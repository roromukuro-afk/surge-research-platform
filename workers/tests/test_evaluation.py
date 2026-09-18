"""The Jev evaluation (surge.evaluation, surge.jobs.jev_eval), on synthetic data only.

No network, no model request, no database: the JPX list, the prices, the
disclosures and the Gateway's answers are all made up here.
"""

from __future__ import annotations

import hashlib
import io
import json
import random
import subprocess
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from surge.analysis.jev_questions import DECISIONS
from surge.evaluation import cost, leakage, report
from surge.evaluation.cost import Budget, BudgetExceeded, Ledger, estimate_usd, plan_problems
from surge.evaluation.method import MethodError, load_method
from surge.evaluation.outcome import compute_outcome
from surge.evaluation.pacing import Pacer, PacingError
from surge.evaluation.population import (
    Sample,
    Screen,
    choose_s0_dates,
    draw_population,
    liquidity_bands,
    price_band,
    reduce_population,
    screen,
)
from surge.evaluation.prices import PriceHistoryError, history_from_chart, trading_sessions
from surge.evaluation.state import (
    REDACTED_CODE,
    REDACTED_NAME,
    _num,
    build_state,
    disclosure_coverage,
    request_body,
    select_disclosures,
)
from surge.evaluation.store import RunStore, StoreError, default_root
from surge.evaluation.universe import ListedIssue, parse_listed_issues
from surge.jobs import jev_eval
from surge.jobs.jev_smoke import gateway_record
from surge.market.models import CorporateActionType
from surge.market.series import build_comparable_series
from surge.providers.yahoo_finance import JST

NOW = datetime(2026, 9, 18, 9, 0, tzinfo=UTC)


# ----------------------------------------------------------------- builders


def business_days(start: date, end: date) -> list[date]:
    days, day = [], start
    while day <= end:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days


def stamp(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, 9, 0, tzinfo=JST).timestamp())


def chart(days, closes, *, highs=None, lows=None, volumes=None, splits=(), currency="JPY") -> dict:
    """A Yahoo chart result. The prices are whatever Yahoo would show (restated for later splits)."""

    return {
        "meta": {"currency": currency},
        "timestamp": [stamp(d) for d in days],
        "indicators": {"quote": [{
            "open": list(closes),
            "high": list(highs) if highs is not None else list(closes),
            "low": list(lows) if lows is not None else list(closes),
            "close": list(closes),
            "volume": list(volumes) if volumes is not None else [100_000] * len(days),
        }]},
        "events": {"splits": {str(stamp(d)): {"date": stamp(d), "numerator": n, "denominator": m}
                              for d, n, m in splits}},
    }


def yahoo_view(days, closes, highs, lows, volumes, splits):
    """Prices as traded -> the chart Yahoo shows today (every earlier bar restated)."""

    def factor(day):
        f = 1.0
        for ex, n, m in splits:
            if ex > day:
                f *= n / m
        return f

    restate = lambda xs: [round(x / factor(d), 4) for x, d in zip(xs, days, strict=True)]  # noqa: E731
    return chart(days, restate(closes), highs=restate(highs), lows=restate(lows),
                 volumes=[int(v * factor(d)) for v, d in zip(volumes, days, strict=True)], splits=splits)


def walk(n: int, *, start: float = 1000.0, seed: int = 1) -> list[float]:
    rng, price, out = random.Random(seed), start, []
    for _ in range(n):
        price = max(50.0, price * (1 + rng.uniform(-0.02, 0.02)))
        out.append(round(price, 1))
    return out


def history(code="1301", days=None, closes=None, **kwargs):
    days = days or business_days(date(2024, 6, 3), date(2025, 8, 29))
    closes = closes or walk(len(days))
    return history_from_chart(code, f"{code}.T", chart(days, closes, **kwargs), fetched_at=NOW)


def sample_for(verdict: Screen, *, cohort="PRIMARY", name="サンプル水産株式会社", **overrides) -> Sample:
    fields = dict(
        sample_id="s-" + verdict.code, cohort=cohort, code=verdict.code, name=name, segment="PRIME",
        sector33="水産・農林業", size_category="TOPIX Small 2", s0=verdict.s0,
        s0_close_as_traded=verdict.close_as_traded, price_band=price_band(verdict.close_as_traded),
        liquidity_band="Q2", routes=verdict.routes, route_evidence=verdict.route_evidence,
    )
    fields.update(overrides)
    return Sample(**fields)


# ----------------------------------------------------------------- store


def test_run_files_are_write_once_and_hashed(tmp_path):
    store = RunStore(tmp_path, "r1")
    sha = store.write_json("a.json", {"x": 1})
    assert sha == hashlib.sha256((store.path / "a.json").read_bytes()).hexdigest()
    with pytest.raises(StoreError, match="write-once"):
        store.write_json("a.json", {"x": 2})
    store.verify("a.json", sha)
    (store.path / "a.json").write_text("tampered", encoding="utf-8")
    with pytest.raises(StoreError, match="does not match"):
        store.verify("a.json", sha)


def test_parquet_keeps_nested_values_as_json_text(tmp_path):
    import pyarrow.parquet as pq

    store = RunStore(tmp_path, "r1")
    store.write_parquet("p.parquet", [{"a": 1, "b": {"k": [1, 2]}, "c": "x"}])
    assert pq.read_table(store.path / "p.parquet").to_pylist() == [{"a": 1, "b": '{"k": [1, 2]}', "c": "x"}]


def test_the_evaluation_root_comes_from_the_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("SURGE_EVALUATION_ROOT", str(tmp_path))
    assert RunStore(default_root(), "x").path == tmp_path / "evaluation" / "jev" / "x"


# ----------------------------------------------------------------- prices


def test_yahoo_bars_are_restored_to_prices_as_traded():
    # A 2:1 split effective Mar 10 and a 1:5 reverse split effective Mar 13;
    # Yahoo shows every earlier bar on today's share count, all at 100 yen.
    days = business_days(date(2025, 3, 3), date(2025, 3, 14))
    splits = [(date(2025, 3, 10), 2, 1), (date(2025, 3, 13), 1, 5)]
    h = history_from_chart("1301", "1301.T", chart(days, [100.0] * 10, volumes=[1000] * 10, splits=splits),
                           fetched_at=NOW)
    bar = {b.trade_date: b for b in h.bars}
    assert bar[date(2025, 3, 7)].close == Decimal("40.0")  # before both: x2 x1/5
    assert bar[date(2025, 3, 11)].close == Decimal("20.0")  # before the reverse split only
    assert bar[date(2025, 3, 14)].close == Decimal("100.0")
    assert bar[date(2025, 3, 7)].volume == Decimal("2500")
    assert bar[date(2025, 3, 11)].volume == Decimal("5000")
    assert bar[date(2025, 3, 7)].turnover == Decimal("40.0") * Decimal("2500")
    assert [a.action_type for a in h.actions] == [CorporateActionType.SPLIT, CorporateActionType.REVERSE_SPLIT]
    assert all(b.is_ohlc_raw() for b in h.bars)

    # And a series as of a date applies only the splits known by then.
    assert build_comparable_series(h.bars, h.actions, as_of=date(2025, 3, 11)).bars[0].close == Decimal("20.0")
    assert build_comparable_series(h.bars, h.actions, as_of=date(2025, 3, 14)).bars[0].close == Decimal("100.0")


def test_a_chart_in_another_currency_is_refused():
    with pytest.raises(PriceHistoryError, match="JPY"):
        history(days=business_days(date(2025, 3, 3), date(2025, 3, 7)), closes=[1.0] * 5, currency="USD")


def test_a_session_is_a_day_enough_securities_traded():
    days = business_days(date(2025, 3, 3), date(2025, 3, 7))
    histories = [history(str(1301 + i), days=days[:4], closes=[100.0] * 4) for i in range(9)]
    histories.append(history("1400", days=days, closes=[100.0] * 5))  # one stray bar on Friday
    assert trading_sessions(histories) == days[:4]


# ----------------------------------------------------------------- universe


def _workbook(rows) -> bytes:
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["日付", "コード", "銘柄名", "市場・商品区分", "33業種コード", "33業種区分", "17業種コード",
               "17業種区分", "規模コード", "規模区分"])
    for row in rows:
        ws.append(row)
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def test_the_universe_is_domestic_common_stock_with_sector_and_size():
    issues = parse_listed_issues(_workbook([
        [20260831, 1301, "サンプル水産", "プライム（内国株式）", "50", "水産・農林業", "1", "食品", "7", "TOPIX Small 2"],
        [20260831, "130A", "テストグロース", "グロース（内国株式）", "5250", "情報・通信業", "10", "情報", "-", "-"],
        [20260831, 1305, "テストETF", "ETF・ETN", "-", "-", "-", "-", "-", "-"],
        [20260831, 25935, "テスト第1種優先株式", "スタンダード（内国株式）", "-", "-", "-", "-", "-", "-"],
        [20260831, 9999, "テスト優先株式", "スタンダード（内国株式）", "-", "-", "-", "-", "-", "-"],
    ]))
    assert [(i.code, i.segment, i.sector33) for i in issues] == [
        ("1301", "PRIME", "水産・農林業"), ("130A", "GROWTH", "情報・通信業"),
    ]
    assert issues[0].size_category == "TOPIX Small 2"


# ----------------------------------------------------------------- population


def _traded_and_yahoo(days, ex, *, seed=5):
    """A security that splits 3:1 on ``ex``: its prices as traded and Yahoo's restated chart."""

    rng, k, ks = random.Random(seed), 3334, []
    for _ in days:
        k = max(1700, k + rng.randint(-40, 40))
        ks.append(k)
    traded = [round(k * 0.3, 1) if d < ex else round(k * 0.1, 1) for d, k in zip(days, ks, strict=True)]
    volumes = [100_000 if d < ex else 300_000 for d in days]
    return traded, volumes, yahoo_view(days, traded, traded, traded, volumes, [(ex, 3, 1)])


def test_the_screening_replay_at_s0_uses_nothing_after_s0():
    days = business_days(date(2024, 6, 3), date(2025, 6, 30))
    s0, ex = days[200], days[230]
    traded, volumes, yahoo_today = _traded_and_yahoo(days, ex)
    today = history_from_chart("1301", "1301.T", yahoo_today, fetched_at=NOW)  # read long after, split included
    then = history_from_chart("1301", "1301.T", chart(days[:201], traded[:201], volumes=volumes[:201]),
                              fetched_at=NOW)  # what existed on the evening of S0
    a, b = screen(today, s0), screen(then, s0)
    assert today.bar_on(s0).close == then.bar_on(s0).close == Decimal(str(traded[200]))
    assert (a.eligible, a.passed, a.routes, a.close_as_traded) == (b.eligible, b.passed, b.routes, b.close_as_traded)
    assert a.features == b.features
    assert a.series.as_of == s0 and a.series.bars[-1].trade_date == s0
    assert leakage.screened_as_of_s0(a, s0) == []


def test_a_breakout_setup_passes_route_a_and_expensive_or_absent_bars_are_ineligible():
    days = business_days(date(2025, 1, 6), date(2025, 6, 30))[:120]
    flat = [1000.0] * 120
    verdict = screen(history(days=days, closes=flat, volumes=[100_000] * 119 + [500_000]), days[-1])
    assert verdict.eligible and verdict.passed and "A" in verdict.routes

    expensive = screen(history(days=days, closes=[3000.1] * 120), days[-1])
    assert not expensive.eligible and "3,000" in expensive.reason
    assert screen(history(days=days, closes=[3000.0] * 120), days[-1]).eligible  # 3,000 itself is allowed
    assert screen(history(days=days, closes=flat), date(2025, 7, 1)).reason == "no bar on S0"


def test_price_and_liquidity_bands():
    assert [price_band(Decimal(x)) for x in ("500", "500.1", "3000", "3000.1")] == [
        "0-500", "500-1000", "2000-3000", "above-3000"]
    screens = [Screen(str(i), date(2025, 3, 3), True, False, Decimal(100), turnover_avg_20d=float(i))
               for i in range(1, 9)]
    assert liquidity_bands(screens) == {"1": "Q1", "2": "Q1", "3": "Q2", "4": "Q2", "5": "Q3", "6": "Q3",
                                        "7": "Q4", "8": "Q4"}


def _population_inputs():
    sessions = business_days(date(2025, 1, 1), date(2025, 12, 31))
    s0s = choose_s0_dates(sessions, date(2025, 1, 1), date(2025, 12, 31), 2, random.Random(1))
    closes = {"P1": 700, "P2": 1500, "P3": 2500, "N1": 800, "N2": 1600, "N3": 2600, "N4": 300}
    screens = {
        day: [Screen(code, day, True, code.startswith("P"), Decimal(close),
                     routes=["A"] if code.startswith("P") else [], turnover_avg_20d=float(close) * 1000)
              for code, close in closes.items()]
        for day in s0s
    }
    issues = {c: ListedIssue(c, f"会社{c}", "PRIME", "情報・通信業", "TOPIX Small 1") for c in closes}
    return screens, issues, sessions


def test_the_evaluated_subset_is_seeded_by_cohort_and_id_only():
    screens, issues, sessions = _population_inputs()
    full = draw_population(screens, issues, sessions, primary=12, control=4, anonymized=2, drift=1, seed=9)
    small = reduce_population(full, primary=5, control=2, anonymized=2, drift=2, seed="9/phase-a-evaluated")
    assert (sum(s.cohort == "PRIMARY" for s in small), sum(s.cohort == "CONTROL" for s in small)) == (5, 2)
    by_id = {s.sample_id: s for s in full}
    for s in small:  # the same samples, only the sensitivity flags drawn again
        assert replace(s, anonymized_pair=False, drift_repeat=False) == replace(
            by_id[s.sample_id], anonymized_pair=False, drift_repeat=False)
    anonymized = {s.sample_id for s in small if s.anonymized_pair}
    drift = {s.sample_id for s in small if s.drift_repeat}
    assert len(anonymized) == 2 and len(drift) == 2 and not anonymized & drift
    order = [s.sample_id for s in full]
    assert [s.sample_id for s in small] == [i for i in order if i in {s.sample_id for s in small}]
    assert reduce_population(full, primary=5, control=2, anonymized=2, drift=2,
                             seed="9/phase-a-evaluated") == small  # deterministic
    with pytest.raises(ValueError, match="CONTROL"):
        reduce_population(full, primary=5, control=5, anonymized=0, drift=0, seed="x")


def test_the_population_is_primary_and_matched_control_spaced_and_seeded():
    screens, issues, sessions = _population_inputs()
    samples = draw_population(screens, issues, sessions, primary=12, control=4, anonymized=2, drift=1, seed=9)
    primary = [s for s in samples if s.cohort == "PRIMARY"]
    control = [s for s in samples if s.cohort == "CONTROL"]
    assert (len(primary), len(control)) == (12, 4)
    assert all(s.code.startswith("P") for s in primary) and all(s.code.startswith("N") for s in control)
    by_id = {s.sample_id: s for s in primary}
    for c in control:
        anchor = by_id[c.matched_to]
        assert c.s0 == anchor.s0 and c.price_band == anchor.price_band
    index = {d: i for i, d in enumerate(sessions)}
    for code in {s.code for s in samples}:
        positions = sorted(index[s.s0] for s in samples if s.code == code)
        assert all(b - a >= 20 for a, b in zip(positions, positions[1:], strict=False))
    assert len({(s.s0.year, s.s0.month) for s in primary}) >= 6  # spread over the months
    anonymized = {s.sample_id for s in samples if s.anonymized_pair}
    drift = {s.sample_id for s in samples if s.drift_repeat}
    assert len(anonymized) == 2 and len(drift) == 1 and not anonymized & drift
    assert not any(s.teacher_admissible for s in samples)
    assert draw_population(screens, issues, sessions, primary=12, control=4, anonymized=2, drift=1, seed=9) == samples


# ----------------------------------------------------------------- state


def _real_screen():
    days = business_days(date(2024, 6, 3), date(2025, 6, 30))
    verdict = screen(history(days=days, closes=walk(len(days), seed=11)), days[200])
    assert verdict.eligible
    return verdict


def test_numbers_are_written_plainly():
    assert _num(Decimal("3000")) == "3000"
    assert _num(Decimal("1234.50")) == "1234.5"
    assert _num(0.1234567) == 0.123457
    assert _num(None) is None


def test_the_state_holds_s0_facts_and_nothing_later():
    verdict = _real_screen()
    sample = sample_for(verdict)
    disclosures = [{"published_at": datetime(2025, 3, 3, 15, 0, tzinfo=JST).isoformat(),
                    "title": "サンプル水産株式会社 業績予想の修正に関するお知らせ"}]
    state = build_state(sample, verdict, disclosures, canonical="CANON", addenda=["ADD1", "ADD2"])
    assert state["s0"]["confirmed_close_jpy"] == _num(verdict.close_as_traded)
    assert Decimal(state["given_by_code_not_to_be_judged"]["target_price_jpy"]) == (
        verdict.close_as_traded * Decimal("1.20")).quantize(Decimal("0.01"))
    assert len(state["daily_bars_up_to_s0"]) == 20 and state["daily_bars_up_to_s0"][-1]["date"] == sample.s0.isoformat()
    assert "native_symbol" not in state["features_at_s0"]
    assert leakage.bars_not_after_s0(state, sample.s0) == []
    assert leakage.no_date_after_cutoff(state, sample.s0) == []
    assert leakage.anonymized_has_no_identity(state, sample)  # the plain state does identify it

    body = request_body(state)
    assert body["model"] == "typesafe-ai/jev"
    assert body["questions"]["reaches_target"]["type"] == "boolean"
    json.dumps(body, ensure_ascii=False)  # every value serializes


def test_the_anonymized_state_differs_only_in_identity():
    verdict = _real_screen()
    sample = sample_for(verdict)
    titles = [{"published_at": datetime(2025, 3, 3, 15, 0, tzinfo=JST).isoformat(),
               "title": "サンプル水産株式会社 業績予想の修正に関するお知らせ（コード1301）"}]
    plain = build_state(sample, verdict, titles, canonical="C", addenda=[])
    anon = build_state(sample, verdict, titles, canonical="C", addenda=[], anonymized=True)
    assert anon["security"]["code"] == REDACTED_CODE and anon["security"]["name"] == REDACTED_NAME
    assert anon["tdnet_disclosure_titles_up_to_cutoff"][0]["title"] == (
        f"{REDACTED_NAME} 業績予想の修正に関するお知らせ（コード{REDACTED_CODE}）")
    assert leakage.anonymized_has_no_identity(anon, sample) == []
    kept = ("s0", "given_by_code_not_to_be_judged", "daily_bars_up_to_s0", "features_at_s0", "screening_at_s0",
            "method", "data_cutoff")
    assert all(plain[k] == anon[k] for k in kept)
    assert anon["security"]["segment"] == plain["security"]["segment"] == "PRIME"
    assert anon["security"]["sector33"] == plain["security"]["sector33"]


def test_a_full_width_name_is_also_found_half_width():
    # JPX writes Latin letters full-width; a title may not.
    verdict = _real_screen()
    sample = sample_for(verdict, name="ＸＹテックホールディングス")
    titles = [{"published_at": datetime(2025, 3, 3, 15, 0, tzinfo=JST).isoformat(), "title": "XYテックHD 配当予想の修正"}]
    anon = build_state(sample, verdict, titles, canonical="C", addenda=[], anonymized=True)
    assert anon["tdnet_disclosure_titles_up_to_cutoff"][0]["title"] == f"{REDACTED_NAME}HD 配当予想の修正"
    assert leakage.anonymized_has_no_identity(anon, sample) == []
    plain = build_state(sample, verdict, titles, canonical="C", addenda=[])
    plain["security"].update(code=REDACTED_CODE, name=REDACTED_NAME)
    assert leakage.anonymized_has_no_identity(plain, sample)  # the half-width name in the title is caught


def test_disclosures_are_the_60_days_before_the_cutoff_less_the_margin():
    s0 = date(2025, 3, 14)
    items = [SimpleNamespace(pubdate=datetime(2025, 3, 14, 23, 29, tzinfo=JST), title="in"),
             SimpleNamespace(pubdate=datetime(2025, 3, 14, 23, 31, tzinfo=JST), title="inside the margin"),
             SimpleNamespace(pubdate=datetime(2025, 3, 15, 8, 0, tzinfo=JST), title="after the cutoff"),
             SimpleNamespace(pubdate=datetime(2025, 1, 1, 15, 0, tzinfo=JST), title="before the window")]
    assert [t["title"] for t in select_disclosures(items, s0)] == ["in"]
    assert disclosure_coverage(s0, returned=120, limit=300, oldest_returned=None) == "COMPLETE"
    early, late = datetime(2024, 12, 1, tzinfo=JST), datetime(2025, 2, 1, tzinfo=JST)
    assert disclosure_coverage(s0, returned=300, limit=300, oldest_returned=early) == "COMPLETE"
    assert disclosure_coverage(s0, returned=300, limit=300, oldest_returned=late) == "INCOMPLETE"


# ----------------------------------------------------------------- outcome


def _outcome_case(rows, *, splits=(), drop=None, sessions_after=25):
    """S0 at 1,000 yen as traded, then ``rows`` of (high, low, close) as traded, flat after."""

    days = business_days(date(2025, 1, 6), date(2025, 12, 31))[:60 + sessions_after + 1]
    s0 = days[60]
    traded = [(1010.0, 990.0, 1000.0)] * 61 + list(rows) + [(1010.0, 990.0, 1000.0)] * (sessions_after - len(rows))

    def as_traded(values):
        # After a 2:1 split the same value trades at half the price.
        return [v / 2 if any(ex <= d for ex, _, _ in splits) else v for v, d in zip(values, days, strict=True)]

    highs, lows, closes = (as_traded([t[i] for t in traded]) for i in range(3))
    view = yahoo_view(days, closes, highs, lows, [100_000] * len(days), list(splits))
    if drop is not None:
        keep = [i for i, d in enumerate(days) if d != drop]
        quote = view["indicators"]["quote"][0]
        view["timestamp"] = [view["timestamp"][i] for i in keep]
        for key in quote:
            quote[key] = [quote[key][i] for i in keep]
    h = history_from_chart("1301", "1301.T", view, fetched_at=NOW)
    sample = sample_for(Screen("1301", s0, True, True, Decimal("1000.0")), routes=[], route_evidence={})
    return sample, h, days, s0


def test_the_outcome_measures_plus_20_percent_both_ways():
    rows = [(1010.0, 990.0, 1000.0), (1010.0, 950.0, 990.0), (1205.0, 1000.0, 1100.0)]
    sample, h, days, s0 = _outcome_case(rows)
    outcome = compute_outcome(sample, h, days)
    v = outcome.values
    assert outcome.resolution == "RESOLVED" and outcome.window[0] == days[61].isoformat()
    assert (v["hit_20_high"], v["first_hit_session_high"]) == (True, 3)
    assert (v["hit_20_close"], v["first_hit_session_close"]) == (False, None)
    assert v["ret_t1"] == pytest.approx(0.0) and v["ret_t3"] == pytest.approx(0.10)
    assert v["max_upside_high"] == pytest.approx(0.205) and v["max_drawdown_low"] == pytest.approx(-0.05)
    assert outcome.row()["teacher_admissible"] is False


def test_a_split_inside_the_window_is_not_a_crash():
    t5 = business_days(date(2025, 1, 6), date(2025, 12, 31))[65]
    sample, h, days, s0 = _outcome_case([], splits=[(t5, 2, 1)])
    v = compute_outcome(sample, h, days).values
    assert h.bar_on(days[66]).close == Decimal("500.0")  # as traded, after the split
    assert v["ret_t20"] == pytest.approx(0.0) and v["max_drawdown_close"] == pytest.approx(0.0)
    assert v["max_drawdown_low"] == pytest.approx(-0.01)


def test_a_missing_session_leaves_the_outcome_unresolved_and_a_short_window_pending():
    all_days = business_days(date(2025, 1, 6), date(2025, 12, 31))
    sample, h, days, s0 = _outcome_case([], drop=all_days[64])
    outcome = compute_outcome(sample, h, days)
    assert outcome.resolution == "UNRESOLVED_MISSING_DATA"
    assert outcome.values["missing_sessions"] == [all_days[64].isoformat()]

    sample, h, days, s0 = _outcome_case([], sessions_after=10)
    assert compute_outcome(sample, h, days).resolution == "PENDING"

    with pytest.raises(ValueError, match="does not match"):
        compute_outcome(replace(sample, s0_close_as_traded=Decimal("999")), h, days[:61] + all_days[61:90])


# ----------------------------------------------------------------- leakage


def _clean_case():
    days = business_days(date(2024, 6, 3), date(2025, 12, 31))
    h = history(days=days, closes=walk(len(days), seed=11))
    s0 = days[200]
    verdict = screen(h, s0)
    sample = sample_for(verdict)
    method_c, method_a = "CANON", ["ADD1"]
    state = build_state(sample, verdict, [], canonical=method_c, addenda=method_a)
    anon = build_state(sample, verdict, [], canonical=method_c, addenda=method_a, anonymized=True)
    outcome = compute_outcome(sample, h, days)
    hashes = dict(canonical_sha256=hashlib.sha256(b"CANON").hexdigest(),
                  addenda_sha256=[hashlib.sha256(b"ADD1").hexdigest()])
    return sample, verdict, state, anon, outcome, hashes


def test_a_clean_sample_passes_every_leakage_check():
    sample, verdict, state, anon, outcome, hashes = _clean_case()
    assert leakage.check_sample(sample, verdict, state, outcome, anonymized_state=anon, **hashes) == []


def test_each_leak_is_caught():
    sample, verdict, state, anon, outcome, hashes = _clean_case()
    s0 = sample.s0

    future_bar = json.loads(json.dumps(state))
    future_bar["daily_bars_up_to_s0"].append({**future_bar["daily_bars_up_to_s0"][-1], "date": outcome.window[0]})
    assert leakage.bars_not_after_s0(future_bar, s0)
    assert leakage.outcome_window_outside_state(future_bar, outcome, s0)

    late_title = json.loads(json.dumps(state))
    next_morning = datetime(s0.year, s0.month, s0.day, 8, 0, tzinfo=JST) + timedelta(days=1)
    late_title["tdnet_disclosure_titles_up_to_cutoff"] = [{"published_at": next_morning.isoformat(), "title": "x"}]
    assert leakage.disclosures_before_cutoff(late_title, s0)

    stray_date = json.loads(json.dumps(state))
    stray_date["screening_at_s0"]["route_evidence"] = {"A": {"note": f"high on {s0 + timedelta(days=3)}"}}
    assert leakage.no_date_after_cutoff(stray_date, s0)

    assert leakage.screened_as_of_s0(verdict, s0) == []
    assert leakage.screened_as_of_s0(verdict, s0 - timedelta(days=1))  # screened as of another day

    assert leakage.base_matches(state, replace(outcome, base=outcome.base + 1))
    assert leakage.method_matches_manifest(state, "0" * 64, hashes["addenda_sha256"])
    assert leakage.method_matches_manifest(state, hashes["canonical_sha256"], [])

    named = json.loads(json.dumps(anon))
    named["tdnet_disclosure_titles_up_to_cutoff"] = [{"published_at": "2025-01-06T15:00:00+09:00",
                                                      "title": "サンプル水産の新製品"}]
    assert leakage.anonymized_has_no_identity(named, sample)

    assert leakage.eligible_and_not_teacher_data(replace(sample, s0_close_as_traded=Decimal("3000.1")))
    assert leakage.eligible_and_not_teacher_data(replace(sample, teacher_admissible=True))


# ----------------------------------------------------------------- cost


def test_the_estimate_is_the_o200k_count_times_the_safety_factor():
    assert estimate_usd(10_000) == Decimal(13_500) * Decimal("0.042") / Decimal(1_000_000)
    assert cost.estimated_jev_tokens(17_751) == 23_964  # ceil(17,751 x 1.35)


def test_a_budget_is_positive_and_within_the_free_credit():
    with pytest.raises(ValueError, match="free credit"):
        Budget(Decimal("5.01"), 10)
    with pytest.raises(ValueError):
        Budget(Decimal(0), 10)


def test_the_ledger_stops_before_the_money_or_the_count_runs_out():
    ledger = Ledger(Budget(Decimal("0.002"), 3))
    ledger.record({"label": "a", "gateway_cost_usd": "0.0009"}, estimate=Decimal("0.001"))
    ledger.check_next(Decimal("0.0009"))
    ledger.record({"label": "b", "gateway_cost_usd": None}, estimate=Decimal("0.001"))  # unreported: the estimate
    assert ledger.spent_usd == Decimal("0.0019") and ledger.estimated_charges == ["b"]
    with pytest.raises(BudgetExceeded, match="would pass"):
        ledger.check_next(Decimal("0.0002"))
    counted = Ledger(Budget(Decimal(1), 1))
    counted.record({"gateway_cost_usd": "0"}, estimate=Decimal(0))
    with pytest.raises(BudgetExceeded, match="requests"):
        counted.check_next(Decimal(0))


def test_the_plan_must_fit_the_budget_and_the_credit_balance():
    budget = Budget(Decimal("0.20"), 3)
    estimates = [Decimal("0.001")] * 3
    ok = {"balance": "4.99", "total_used": "0.01"}
    assert plan_problems(budget, estimates, ok) == []
    assert plan_problems(budget, estimates * 2, ok) == ["6 requests planned, the budget allows 3"]
    assert "above the budget" in plan_problems(Budget(Decimal("0.002"), 3), estimates, ok)[0]
    assert "not been read" in plan_problems(budget, estimates, None)[0]
    assert "could not be read" in plan_problems(budget, estimates, {"error": "401"})[0]
    assert "above the credit balance" in plan_problems(budget, estimates, {"balance": "0.10", "total_used": "4.9"})[0]


def test_the_credit_balance_is_read_through_the_runner_into_a_new_file(tmp_path, monkeypatch):
    def fake_run(args, **kwargs):
        Path(args[3]).write_text(json.dumps({"balance": "4.98", "totalUsed": "0.02",
                                             "checkedAt": "2026-09-18T09:00:00Z"}), encoding="utf-8")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(cost.subprocess, "run", fake_run)
    out = tmp_path / "credits.json"
    assert cost.read_credits(Path("runner.mjs"), out) == {"balance": "4.98", "total_used": "0.02",
                                                           "checked_at": "2026-09-18T09:00:00Z"}
    with pytest.raises(FileExistsError):
        cost.read_credits(Path("runner.mjs"), out)


# ----------------------------------------------------------------- report


def test_average_precision_brier_calibration_and_spearman():
    assert report.average_precision([0.9, 0.8, 0.7, 0.6], [True, False, True, False]) == pytest.approx(
        0.5 * 1 + 0.5 * 2 / 3)
    assert report.average_precision([0.5, 0.5], [True, False]) == pytest.approx(0.5)  # ties: one threshold
    assert report.average_precision([0.5], [False]) is None
    assert report.brier([1.0, 0.0], [True, False]) == 0.0 and report.brier([0.5], [True]) == 0.25
    table = report.calibration([0.05, 0.15, 1.0], [False, True, True])
    assert [(b["bin"], b["n"], b["observed_rate"]) for b in table["bins"]] == [
        ("0.0-0.1", 1, 0.0), ("0.1-0.2", 1, 1.0), ("0.9-1.0", 1, 1.0)]
    assert report.spearman([1, 2, 3], [10, 20, 30]) == pytest.approx(1.0)
    assert report.spearman([1, 2, 3], [3, 2, 1]) == pytest.approx(-1.0)
    assert report.spearman([1, 1, 1], [1, 2, 3]) is None and report.spearman([1, 2], [1, 2]) is None


def _gateway_raw(choice="REJECT", p=0.1, score=1.0, cost_usd="0.0009"):
    options = list(DECISIONS)
    probabilities = {k: (0.6 if k == choice else 0.1) for k in options}
    basis = {f"basis_{k}": {"type": "boolean", "probability": 0.3} for k in (
        "current_material", "supply_structure", "volume_structure", "support_resistance", "volatility_range",
        "sector_move")}
    return {
        "latencyMs": 950,
        "answers": {
            "decision": {"type": "choice", "choice": choice, "probabilities": probabilities},
            "reaches_target": {"type": "boolean", "probability": p},
            "upside_band": {"type": "score", "score": score,
                            "probabilities": {"0": 0.2, "1": 0.4, "2": 0.2, "3": 0.1, "4": 0.1}},
            **basis,
        },
        "usage": {"inputTokens": 22_000, "outputTokens": 211},
        "providerMetadata": {"gateway": {"cost": cost_usd, "routing": {"finalProvider": "typesafe-ai"}},
                             "typesafe": {"confidence": {"decision": 0.7, "upside_band": 0.4}}},
        "response": {"modelId": "typesafe-ai/jev"},
    }


def _row(sample_id, cohort, variant, raw, request_sha="r1"):
    from surge.analysis.jev_questions import questions

    record = gateway_record(raw, asked=questions(), label=sample_id, request_sha256=request_sha)
    return report.prediction_row(
        record, {"sample_id": sample_id, "cohort": cohort, "code": "1301", "s0": "2025-03-14"}, variant=variant,
        request={"file": f"requests/{sample_id}.{variant}.json", "sha256": request_sha, "o200k_tokens": 17_000,
                 "estimated_usd": "0.00096"},
        evaluation_version="test", question_schema_hash="h",
    )


def test_a_prediction_row_flattens_one_gateway_answer():
    row = _row("s1", "PRIMARY", "main", _gateway_raw(choice="ENTRY", p=0.42))
    assert row["complete"] and row["decision"] == "ENTRY" and row["reaches_target"] == 0.42
    assert row["decision_confidence"] == 0.7 and row["upside_score"] == 1.0
    assert row["gateway_cost_usd"] == 0.0009 and row["input_tokens"] == 22_000
    assert row["teacher_admissible"] is False and row["basis_sector_move"] == 0.3


def _outcome_row(sample_id, hit_high, hit_close, resolution="RESOLVED", upside=0.1):
    return {"sample_id": sample_id, "resolution": resolution, "hit_20_high": hit_high, "hit_20_close": hit_close,
            "ret_t20": upside / 2, "max_upside_high": upside, "max_upside_close": upside * 0.8,
            "max_drawdown_low": -0.05}


def test_undefined_statistics_stay_null_with_few_samples():
    predictions = [_row("p1", "PRIMARY", "main", _gateway_raw("REJECT", 0.1, 0.5)),
                   _row("p2", "PRIMARY", "main", _gateway_raw("WATCH_BREAKOUT", 0.3, 1.0)),
                   _row("c1", "CONTROL", "main", _gateway_raw("REJECT", 0.2, 0.8))]
    outcomes = [_outcome_row("p1", False, False), _outcome_row("p2", False, False), _outcome_row("c1", False, False)]
    result = report.build_report({"run_id": "t", "phase": "A", "model": "typesafe-ai/jev"}, predictions, outcomes,
                                 planned=3, budget={"max_usd": "0.05", "max_requests": 3})
    high = result["primary"]["hit_20_high"]
    assert high["positives"] == 0 and high["pr_auc_average_precision"] is None and high["brier_skill"] is None
    assert result["primary"]["score_vs_future_return_spearman"]["upside_score_vs_ret_t20"] is None  # n < 3
    assert result["primary"]["by_decision"]["ENTRY"] == {
        "n": 0, "hit_20_high_rate": None, "hit_20_close_rate": None, "mean_ret_t20": None,
        "median_max_upside_high": None, "median_max_drawdown_low": None}
    assert result["anonymized_sensitivity"]["n"] == 0 and result["anonymized_sensitivity"]["decision_change_rate"] is None
    assert result["pipeline"]["outcome_join"]["joined_to_resolved_outcome"] == 3
    json.dumps(result)
    assert "| hit_20_high | 0 | 0.000 |" in report.render_markdown(result)


def test_the_report_keeps_the_cohorts_apart_and_says_phase_a_is_not_for_adoption():
    predictions = [
        _row("p1", "PRIMARY", "main", _gateway_raw("ENTRY", 0.8, 3.0)),
        _row("p2", "PRIMARY", "main", _gateway_raw("REJECT", 0.1, 0.5)),
        _row("p3", "PRIMARY", "main", _gateway_raw("WATCH_BREAKOUT", 0.4, 2.0)),
        _row("p4", "PRIMARY", "main", _gateway_raw("REJECT", 0.2, 1.0)),
        _row("c1", "CONTROL", "main", _gateway_raw("ENTRY", 0.7, 2.5)),
        _row("p1", "PRIMARY", "anonymized", _gateway_raw("REJECT", 0.5, 2.0), request_sha="r2"),
        _row("p2", "PRIMARY", "drift", _gateway_raw("REJECT", 0.12, 0.5)),
    ]
    outcomes = [_outcome_row("p1", True, True, upside=0.3), _outcome_row("p2", False, False, upside=0.02),
                _outcome_row("p3", True, False, upside=0.22), _outcome_row("p4", False, False, "PENDING"),
                _outcome_row("c1", False, False, upside=0.05)]
    manifest = {"run_id": "t", "phase": "A", "model": "typesafe-ai/jev"}
    result = report.build_report(manifest, predictions, outcomes, planned=7,
                                 budget={"max_usd": "0.2", "max_requests": 7})
    assert result["banner"] == report.PHASE_A_BANNER
    assert result["primary"]["n"] == 3  # p4 is pending; the control is not pooled
    assert result["primary"]["hit_20_high"]["base_rate"] == pytest.approx(2 / 3)
    assert result["primary"]["by_decision"]["ENTRY"]["n"] == 1
    assert set(result["control_benchmark"]) == {"note", "n", "decision_counts", "mean_reaches_target",
                                                "pipeline_value"}  # no Brier, no calibration
    assert result["control_benchmark"]["n"] == 1
    assert result["anonymized_sensitivity"]["decision_changed"] == 1
    assert result["anonymized_sensitivity"]["reaches_target_abs_diff_max"] == pytest.approx(0.3)
    assert result["drift"]["comparable"] == 1 and result["drift"]["decision_changed"] == 0
    assert result["pipeline"]["requests"] == {"planned": 7, "sent": 7, "ok": 7, "errors": 0, "incomplete": 0,
                                              "by_variant": {"main": 5, "anonymized": 1, "drift": 1}}
    assert result["pipeline"]["cost"]["gateway_cost_usd_total"] == pytest.approx(0.0063)
    assert result["teacher_admissible"] is False
    text = report.render_markdown(result)
    assert report.PHASE_A_BANNER in text and "Control (benchmark only)" in text
    json.dumps(result)


# ----------------------------------------------------------------- method


def test_the_method_is_the_manifest_s_canonical_and_addenda_in_registration_order():
    method = load_method()
    assert method.canonical.path == "docs/prompts/short-surge-v5.1.original.md"
    assert hashlib.sha256(method.canonical.text.encode("utf-8")).hexdigest() == method.canonical.sha256
    paths = [a.path for a in method.addenda]
    assert paths[0] == "docs/prompts/addenda/v5.1-addendum-2026-09-15.md"  # registered first, amended later
    assert paths.index("docs/prompts/addenda/v5.1-addendum-2026-09-18-eod-prediction.md") > paths.index(
        "docs/prompts/addenda/v5.1-addendum-2026-09-15-phase0.2-final-patch.md")


def test_an_unregistered_or_altered_method_file_is_refused(tmp_path):
    (tmp_path / "docs/prompts/addenda").mkdir(parents=True)
    canonical, addendum = b"canonical text\n", b"addendum text\n"
    (tmp_path / "docs/prompts/short-surge-v5.1.original.md").write_bytes(canonical)
    (tmp_path / "docs/prompts/addenda/a.md").write_bytes(addendum)
    manifest = (
        f"| `docs/prompts/short-surge-v5.1.original.md` | c | `{hashlib.sha256(canonical).hexdigest()}` | 1 |\n"
        f"| `docs/prompts/addenda/a.md` | a | `{hashlib.sha256(addendum).hexdigest()}` | - |\n"
    )
    (tmp_path / "docs/prompts/MANIFEST.md").write_text(manifest, encoding="utf-8")
    assert [a.text for a in load_method(tmp_path).addenda] == ["addendum text\n"]
    (tmp_path / "docs/prompts/addenda/b.md").write_bytes(b"new\n")
    with pytest.raises(MethodError, match="not registered"):
        load_method(tmp_path)
    (tmp_path / "docs/prompts/addenda/b.md").unlink()
    (tmp_path / "docs/prompts/addenda/a.md").write_bytes(b"edited\n")
    with pytest.raises(MethodError, match="MANIFEST records"):
        load_method(tmp_path)


# ----------------------------------------------------------------- the job, end to end (offline)


class _FakeYahoo:
    def __init__(self, charts):
        self.charts = charts
        self.calls = 0

    def chart(self, symbol, params):
        self.calls += 1
        assert params["events"] == "split"
        return {"chart": {"result": [self.charts[symbol]]}}

    def reset(self):
        pass


class _FakeYanoshin:
    limit = 300

    def fetch_for_codes(self, codes):
        (code,) = codes
        items = [SimpleNamespace(yanoshin_id=i, pubdate=datetime(2025, m, 10, 15, 0, tzinfo=JST),
                                 title=f"会社{code}株式会社 月次報告 {m}月", code=SimpleNamespace(normalised=code))
                 for i, m in enumerate(range(1, 9))]
        items.append(SimpleNamespace(yanoshin_id=99, pubdate=datetime(2025, 3, 1, 15, 0, tzinfo=JST),
                                     title="another issuer's title", code=SimpleNamespace(normalised="9999")))
        return SimpleNamespace(items=items, fetched_at=NOW, endpoint=f"https://example.invalid/{code}.json",
                               response_sha256="0" * 64, total_count=len(items))


@pytest.fixture()
def planned_run(tmp_path, monkeypatch):
    codes = [str(2000 + i) for i in range(8)]
    days = business_days(date(2024, 6, 3), date(2026, 8, 31))
    charts = {f"{c}.T": chart(days, walk(len(days), start=600.0 + 250 * i, seed=i),
                              volumes=[100_000 + 1000 * i] * len(days)) for i, c in enumerate(codes)}
    issues = [ListedIssue(c, f"会社{c}株式会社", "PRIME", "情報・通信業", "TOPIX Small 1") for c in codes]
    real_screen = jev_eval.screen

    def screen_with_known_passes(h, s0):
        # The plumbing is under test here, not the routes: half the securities pass.
        verdict = real_screen(h, s0)
        if verdict.eligible and int(h.code) % 2 == 0:
            return replace(verdict, passed=True, routes=["A"], route_evidence={"A": {"close": 1.0}})
        return replace(verdict, passed=False, routes=[], route_evidence={})

    monkeypatch.setattr(jev_eval, "screen", screen_with_known_passes)
    # An official population of 8, of which a seeded 4 are evaluated (the Phase A shape, D-274).
    monkeypatch.setitem(jev_eval.PHASES, "A", {**jev_eval.PHASES["A"], "primary": 6, "control": 2,
                                                "anonymized": 1, "drift": 1,
                                                "evaluated": {"primary": 3, "control": 1, "anonymized": 1,
                                                              "drift": 1}})
    monkeypatch.setattr(jev_eval, "_o200k", lambda text: len(text) // 3)
    store = RunStore(tmp_path, "t1")
    summary = jev_eval.plan(store, seed=7, symbols=8, now=NOW, yahoo=_FakeYahoo(charts),
                            fetch_issues=lambda: (issues, "f" * 64), sleep=lambda _s: None)
    assert summary["official_samples"] == {"PRIMARY": 6, "CONTROL": 2}
    assert summary["samples"] == {"PRIMARY": 3, "CONTROL": 1}
    return store


def test_plan_build_freeze_and_preflight_offline(planned_run):
    store = planned_run
    manifest = store.read_json("manifest.json")
    for key in ("run_id", "evaluation_version", "model", "provider", "model_version_metadata",
                "question_schema_hash", "method", "input_building_code", "population_definition",
                "outcome_definition", "started_at"):
        assert manifest[key], key
    assert manifest["storage"] == {"database_writes": "none", "teacher_admissible": False}
    assert manifest["population_definition"]["cohorts_pooled"] is False
    full = store.read_jsonl("population_full.jsonl")
    evaluated = store.read_jsonl("population.jsonl")
    assert (len(full), len(evaluated)) == (8, 4)
    assert {r["sample_id"] for r in evaluated} <= {r["sample_id"] for r in full}
    assert manifest["population_definition"]["evaluated"]["seed"] == "7/phase-a-evaluated"

    built = jev_eval.build(store, yanoshin=_FakeYanoshin())
    assert built["by_variant"] == {"main": 4, "anonymized": 1, "drift": 1}
    requests = store.read_jsonl("requests.jsonl")
    for row in requests:
        body = json.loads((store.path / row["file"]).read_bytes())
        titles = [t["title"] for t in body["state"]["tdnet_disclosure_titles_up_to_cutoff"]]
        assert "another issuer's title" not in titles
    drift = next(r for r in requests if r["variant"] == "drift")
    assert drift["sha256"] == next(r["sha256"] for r in requests
                                   if r["sample_id"] == drift["sample_id"] and r["variant"] == "main")

    frozen = jev_eval.freeze_outcomes(store)
    assert frozen["resolution"]["RESOLVED"] == 4
    assert all(row["teacher_admissible"] is False for row in store.read_jsonl("outcomes.jsonl"))
    with pytest.raises(StoreError):
        jev_eval.freeze_outcomes(store)

    checked = jev_eval.preflight(store, budget=Budget(Decimal("0.05"), 10), runner=None)
    assert checked["leakage_and_integrity"]["violations"] == {}
    assert checked["requests_total"] == 6
    assert checked["budget_problems"] == ["the Gateway credit balance has not been read"]
    assert checked["ready_to_send"] is False
    with pytest.raises(jev_eval.EvaluationError, match="not ready"):
        jev_eval.run(store, budget=Budget(Decimal("0.05"), 10), runner=Path("runner.mjs"))


BUDGET = Budget(Decimal("0.05"), 6)


class _FakeTime:
    """A clock that moves only when something sleeps, so pacing is exact and instant."""

    def __init__(self):
        self.now = 1000.0
        self.slept = []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


def _pacer(**kwargs):
    fake = _FakeTime()
    return Pacer(clock=fake.clock, sleep=fake.sleep, **kwargs)


def test_the_pacer_keeps_15_seconds_apart_and_4_per_rolling_minute():
    pacer = _pacer()
    waits, counts = [], []
    for _ in range(10):
        waits.append(pacer.wait())
        counts.append(pacer.mark_sent())
    assert waits == [0.0] + [300.0] * 9  # one request every 5 minutes
    assert max(counts) == 4 and counts[:4] == [1, 2, 3, 4]

    # The rolling window holds even when the interval alone would not.
    burst = _pacer(min_interval=0.0)
    for _ in range(4):
        assert burst.wait() == 0.0
        burst.mark_sent()
    assert burst.delay() == 1200.0  # the 5th waits until the 1st is 20 minutes old
    burst.wait()
    assert burst.mark_sent() == 1  # the burst sent at one instant is now 20 minutes old

    unguarded = Pacer(min_interval=0.0, clock=lambda: 0.0, sleep=lambda s: None)
    for _ in range(4):
        unguarded.mark_sent()
    with pytest.raises(PacingError, match="5 requests"):
        unguarded.mark_sent()  # sent without waiting: refused loudly


def test_every_answer_records_its_pacing_and_http_facts(ready_run, monkeypatch):
    store = ready_run
    _fake_gateway(monkeypatch)
    jev_eval.run(store, budget=BUDGET, pacer=_pacer())
    records = sorted((store.read_json(f"responses/{p.name}") for p in (store.path / "responses").glob("*.json")
                      if not p.name.endswith(".raw.json")), key=lambda r: r["pacing"]["requested_at"])
    assert len(records) == 6
    assert [r["pacing"]["waited_seconds"] for r in records] == [0.0] + [300.0] * 5
    assert max(r["pacing"]["requests_in_policy_window"] for r in records) == 4
    assert all(r["pacing"]["requests_in_rolling_60s"] == 1 for r in records)
    assert records[0]["pacing"]["previous_success_at"] is None
    assert records[1]["pacing"]["previous_success_at"] == records[0]["pacing"]["requested_at"]
    assert all(r["http"]["status"] == 200 and r["http"]["retry_after"] is None for r in records)
    assert store.read_json("stage-run.json")["max_requests_in_policy_window"] == 4


def test_a_429_is_recorded_with_its_headers_and_stops_the_run(ready_run, monkeypatch):
    store = ready_run

    def rate_limited(raw, n):
        if n < 3:
            return raw
        return {"startedAt": "2026-09-18T10:00:30.000Z", "latencyMs": 900, "error": {
            "name": "GatewayRateLimitError", "type": "rate_limit_exceeded", "statusCode": 429,
            "message": "Free tier requests on this model are rate-limited.",
            "responseHeaders": {"Retry-After": "30", "x-vercel-id": "hnd1::test"}}}

    sent = _fake_gateway(monkeypatch, answer=rate_limited)
    summary = jev_eval.run(store, budget=BUDGET, pacer=_pacer())
    assert len(sent) == 3 and "Gateway error" in summary["stopped"]
    stop = store.read_json("run-stopped-1.json")
    last = stop["last_request"]
    assert last["http"] == {"status": 429, "error_name": "GatewayRateLimitError", "error_type": "rate_limit_exceeded",
                            "retry_after": "30", "response_headers": {"Retry-After": "30", "x-vercel-id": "hnd1::test"}}
    assert last["pacing"]["requests_in_policy_window"] == 3
    assert last["pacing"]["requests_in_rolling_60s"] == 1
    assert last["pacing"]["previous_success_at"] is not None
    assert last["pacing"]["runner_started_at"] == "2026-09-18T10:00:30.000Z"
    assert "REQUEST" not in json.dumps(stop)  # nothing of the request body


def test_a_429_without_retry_after_records_null():
    facts = jev_eval.http_facts({"error": {"statusCode": 429, "type": "rate_limit_exceeded",
                                           "responseHeaders": {"x-vercel-id": "x"}}})
    assert facts["status"] == 429 and facts["retry_after"] is None
    assert jev_eval.http_facts({"error": {"statusCode": None, "responseHeaders": None}})["response_headers"] is None


@pytest.fixture()
def ready_run(planned_run, monkeypatch):
    """Built, frozen and preflighted with a (fake) credit balance: ready to send."""

    jev_eval.build(planned_run, yanoshin=_FakeYanoshin())
    jev_eval.freeze_outcomes(planned_run)
    monkeypatch.setattr(jev_eval, "read_credits", lambda runner, out: {"balance": "4.99", "total_used": "0.01"})
    assert jev_eval.preflight(planned_run, budget=BUDGET)["ready_to_send"] is True
    return planned_run


def _fake_gateway(monkeypatch, answer=None):
    """Answer each request as the Gateway would; ``answer(raw, n)`` may alter the n-th (1-based)."""

    sent = []
    real_run = subprocess.run

    def fake_runner(args, **kwargs):
        if args[0] != "node":  # git, for the code version
            return real_run(args, **kwargs)
        text = Path(args[2]).read_text(encoding="utf-8")
        sent.append(args[2])
        tokens = int(len(text) // 3 * 1.2)  # the o200k stand-in is len // 3; Jev counts 1.2x that
        raw = _gateway_raw(cost_usd=str(Decimal(tokens) * Decimal("0.042") / Decimal(1_000_000)))
        raw["usage"]["inputTokens"] = tokens
        if answer is not None:
            raw = answer(raw, len(sent))
        Path(args[3]).write_text(json.dumps(raw), encoding="utf-8")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(jev_eval.subprocess, "run", fake_runner)
    return sent


def test_run_and_report_offline_inside_the_budget(ready_run, monkeypatch):
    store = ready_run
    with pytest.raises(jev_eval.EvaluationError, match="differs"):
        jev_eval.run(store, budget=Budget(Decimal("0.06"), 10))

    sent = _fake_gateway(monkeypatch)
    summary = jev_eval.run(store, budget=BUDGET, pacer=_pacer())
    assert summary["stopped"] is None and summary["requests_answered"] == 6 and len(sent) == 6
    assert summary["credits_after"] == {"balance": "4.99", "total_used": "0.01"}
    assert store.exists("predictions.parquet") and store.exists("paired_anonymized.parquet")
    assert store.exists("drift.parquet")
    assert len(store.read_jsonl("predictions.jsonl")) == 4

    result = jev_eval.report(store)
    assert result["banner"] == report.PHASE_A_BANNER
    assert result["pipeline"]["requests"]["sent"] == 6
    assert result["pipeline"]["tokens"]["input_p50"] is not None
    assert result["primary"]["n"] == 3 and result["control_benchmark"]["n"] == 1
    assert result["pipeline"]["outcome_join"]["joined_to_resolved_outcome"] == 4
    assert result["answers"]["PRIMARY"]["n"] == 3 and result["answers"]["CONTROL"]["n"] == 1
    assert result["report_code"]["code_sha256"]
    assert store.exists("report.md")
    for row in store.read_jsonl("predictions.jsonl"):
        assert row["teacher_admissible"] is False

    with pytest.raises(StoreError):  # a report is written once
        jev_eval.report(store)
    with pytest.raises(jev_eval.EvaluationError, match="already sent"):
        jev_eval.run(store, budget=BUDGET)


def test_a_stage_refuses_code_that_changed_since_the_plan(planned_run, monkeypatch):
    real = jev_eval.code_version
    monkeypatch.setattr(jev_eval, "code_version", lambda: {**real(), "code_sha256": "0" * 64})
    with pytest.raises(jev_eval.EvaluationError, match="code changed"):
        jev_eval.build(planned_run, yanoshin=_FakeYanoshin())


def _incomplete(raw):
    del raw["answers"]["basis_sector_move"]
    return raw


@pytest.mark.parametrize(("alter", "reason"), [
    (lambda raw: {"latencyMs": 10, "error": {"message": "HTTP 429"}}, "429"),
    (_incomplete, "schema incomplete"),
    (lambda raw: {**raw, "response": {"modelId": "someone-else/model"}}, "unexpected model"),
    (lambda raw: {**raw, "providerMetadata": {**raw["providerMetadata"], "gateway": {
        **raw["providerMetadata"]["gateway"], "routing": {"finalProvider": "other"}}}}, "unexpected provider"),
    (lambda raw: {**raw, "providerMetadata": {**raw["providerMetadata"], "gateway": {
        **raw["providerMetadata"]["gateway"], "cost": "0.5"}}}, "above the request's estimate"),
    (lambda raw: {**raw, "usage": {**raw["usage"], "inputTokens": 10_000_000}}, "outside the estimate"),
])
def test_one_bad_answer_stops_the_run_and_the_run_is_not_resumed(ready_run, monkeypatch, alter, reason):
    store = ready_run
    sent = _fake_gateway(monkeypatch, answer=lambda raw, n: alter(raw) if n == 2 else raw)
    summary = jev_eval.run(store, budget=BUDGET, pacer=_pacer())
    assert len(sent) == 2 and summary["requests_answered"] == 2 and reason in summary["stopped"]
    assert store.exists("run-stopped-1.json")
    assert not store.exists("stage-run.json") and not store.exists("predictions.parquet")
    with pytest.raises(jev_eval.EvaluationError, match="stopped earlier"):
        jev_eval.run(store, budget=BUDGET)


def test_a_request_file_changed_after_preflight_is_never_sent(ready_run, monkeypatch):
    store = ready_run
    request = store.read_jsonl("requests.jsonl")[0]
    (store.path / request["file"]).write_bytes(b"{}")
    sent = _fake_gateway(monkeypatch)
    with pytest.raises(StoreError, match="does not match"):
        jev_eval.run(store, budget=BUDGET, pacer=_pacer())
    assert sent == []
