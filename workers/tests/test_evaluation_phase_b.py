"""Jev evaluation Phase B (D-272 as decided 2026-09-19): the daily sample, the cohort's guards, offline.

Synthetic data only: the JPX list, the prices, the disclosures and TypeSafe's
answers are made up here. No network, no model request, no database.
"""

from __future__ import annotations

import ast
import copy
import hashlib
import inspect
import json
import random
import socket
import subprocess
import sys
import threading
from collections import Counter
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from surge.evaluation import jpx_calendar, phase_b
from surge.evaluation.cost import Budget, BudgetExceeded, record_credit_snapshot
from surge.evaluation.method import REPO_ROOT
from surge.evaluation.pacing import Pacer
from surge.evaluation.population import Screen, screen
from surge.evaluation.selection import (
    MAX_REQUESTS_PER_DAY,
    PHASE_B_START,
    SelectionError,
    check_s0,
    route_d_subgroup,
    select_day,
    selection_key,
)
from surge.evaluation.store import StoreError
from surge.evaluation.universe import ListedIssue
from surge.jobs import jev_eval
from surge.providers.yahoo_finance import JST
from test_evaluation import (
    FAKE_KEY,
    _direct_answer_body,
    _FakeTime,
    _FakeYahoo,
    _tokens_within_estimate,
    business_days,
    chart,
    history,
    stamp,
    walk,
)

S0 = date(2026, 9, 24)
EVENING = datetime(2026, 9, 24, 17, 0, tzinfo=JST)
VERSION = "jev-eval-1.1.0"
SEED = "phase-b-test-seed"
COHORT = "phase-b-test"
DAY_BUDGET = Budget(Decimal("0.12"), 92)
SECTORS = ("情報・通信業", "サービス業", "小売業", "機械")
ROUTE_PATTERN = (["D"], ["D"], ["A", "D"], ["B"], ["H"], ["D"], ["A"])


# ----------------------------------------------------------------- the daily sample


def _day_screens(passing=300, failing=200, *, s0=S0, routes_for=None):
    screens, issues = [], {}
    for i in range(passing + failing):
        code = str(4000 + i)
        passed = i < passing
        routes = (routes_for or (lambda k: ROUTE_PATTERN[k % len(ROUTE_PATTERN)]))(i) if passed else []
        screens.append(Screen(code, s0, True, passed, Decimal(100 + (i * 53) % 2900), routes=list(routes),
                              route_evidence={r: {"x": 1.0} for r in routes},
                              turnover_avg_20d=float(1000 + (i * 7919) % 100_000)))
        issues[code] = ListedIssue(code, f"銘柄{code}", "PRIME", SECTORS[i % len(SECTORS)], "TOPIX Small 1")
    return screens, issues


def _select(screens, issues, *, s0=S0, version=VERSION, seed=SEED):
    return select_day(screens, issues, s0=s0, evaluation_version=version, experiment_seed=seed)


def _codes(day, cohort="PRIMARY"):
    return [s.code for s in day.samples if s.cohort == cohort]


def test_a_day_is_60_primary_20_control_8_anonymized_4_drift_and_the_same_every_time():
    screens, issues = _day_screens()
    day = _select(screens, issues)
    assert Counter(s.cohort for s in day.samples) == {"PRIMARY": 60, "CONTROL": 20}
    anonymized = {s.sample_id for s in day.samples if s.anonymized_pair}
    drift = {s.sample_id for s in day.samples if s.drift_repeat}
    assert (len(anonymized), len(drift)) == (8, 4) and not anonymized & drift
    assert day.summary["requests"] == 92 == MAX_REQUESTS_PER_DAY
    assert day.summary["selected"] == {"primary": 60, "control": 20, "anonymized": 8, "drift": 4}

    # The same date, version and seed draw the same 80, in whatever order the screens come.
    shuffled = screens[:]
    random.Random(3).shuffle(shuffled)
    again = _select(shuffled, issues)
    assert again.samples == day.samples and again.candidates == day.candidates
    # Another seed, date or version draws another sample.
    assert set(_codes(_select(screens, issues, seed="another-seed"))) != set(_codes(day))
    assert set(_codes(_select(screens, issues, version="jev-eval-9.9.9"))) != set(_codes(day))
    next_day = [replace(s, s0=date(2026, 9, 25)) for s in screens]
    assert set(_codes(_select(next_day, issues, s0=date(2026, 9, 25)))) != set(_codes(day))

    # The key is SHA-256 of version | seed | S0 | purpose | code; the 60 taken are the 60 lowest.
    first = next(c for c in day.candidates if c["code"] == "4000")
    assert first["selection_key"] == hashlib.sha256(f"{VERSION}|{SEED}|2026-09-24|PRIMARY|4000".encode()).hexdigest()
    pool = sorted((c for c in day.candidates if c["pool"] == "PRIMARY_POOL"), key=lambda c: c["selection_key"])
    assert [c["selected"] for c in pool] == [True] * 60 + [False] * 240
    assert [c["selection_rank"] for c in pool] == list(range(1, 301))
    assert all(c["selection_probability"] == 60 / 300 for c in pool)
    assert {c["sample_id"] for c in pool if c["selected"]} == {s.sample_id for s in day.samples
                                                               if s.cohort == "PRIMARY"}
    # Every eligible security is kept, passing or not, with its route membership.
    assert Counter(c["pool"] for c in day.candidates) == {"PRIMARY_POOL": 300, "CONTROL_POOL": 200}
    for c in day.candidates:
        assert [c[f"route_{r}"] for r in "ABCDEFGH"] == [r in c["routes"] for r in "ABCDEFGH"]
        assert c["teacher_admissible"] is False
    assert not any(s.teacher_admissible for s in day.samples)


def test_every_passing_candidate_is_taken_when_60_or_fewer_pass():
    screens, issues = _day_screens(passing=45, failing=100)
    day = _select(screens, issues)
    primary = [s for s in day.samples if s.cohort == "PRIMARY"]
    assert len(primary) == 45 and all(s.selection_probability == 1.0 for s in primary)
    assert sum(s.cohort == "CONTROL" for s in day.samples) == 15  # one for every three Primary
    assert day.summary["selected"] == {"primary": 45, "control": 15, "anonymized": 6, "drift": 3}
    assert day.summary["requests"] == 69


def test_route_d_only_candidates_are_drawn_like_any_other():
    assert [route_d_subgroup(r) for r in (["D"], ["A", "D"], ["D", "H"], ["B"], [])] == [
        "D_ONLY", "D_PLUS_OTHER", "D_PLUS_OTHER", "NON_D", None]
    screens, issues = _day_screens()
    day = _select(screens, issues)
    primary = [s for s in day.samples if s.cohort == "PRIMARY"]
    # The draw reads no route: relabel every candidate and the same securities are drawn.
    for relabel in (["D"], ["A"], ["A", "D"]):
        relabelled = [replace(s, routes=list(relabel)) if s.passed else s for s in screens]
        assert _codes(_select(relabelled, issues)) == _codes(day)
    # D-only candidates are neither excluded nor down-weighted: same probability, drawn, labelled.
    d_only = [c for c in day.candidates if c["d_only"]]
    assert len(d_only) == sum(ROUTE_PATTERN[i % 7] == ["D"] for i in range(300))
    assert {c["selection_probability"] for c in d_only} == {60 / 300}
    assert sum(s.route_d_subgroup == "D_ONLY" for s in primary) == sum(c["selected"] for c in d_only) > 0
    assert {s.route_d_subgroup for s in primary} == {"D_ONLY", "D_PLUS_OTHER", "NON_D"}
    assert day.summary["route_d_subgroups_selected"] == dict(Counter(s.route_d_subgroup for s in primary))
    assert all(s.route_d_subgroup is None for s in day.samples if s.cohort == "CONTROL")


def test_control_is_matched_on_price_and_liquidity_then_sector_and_ties_go_to_the_lowest_key():
    screens, issues = _day_screens()
    day = _select(screens, issues)
    by_id = {s.sample_id: s for s in day.samples}
    failing = {s.code for s in screens if not s.passed}
    controls = [s for s in day.samples if s.cohort == "CONTROL"]
    assert len({c.code for c in controls}) == 20 and {c.code for c in controls} <= failing
    for c in controls:
        anchor = by_id[c.matched_to]
        assert anchor.cohort == "PRIMARY" and c.routes == []
        if c.match_tier.startswith("PRICE+LIQUIDITY"):
            assert (c.price_band, c.liquidity_band) == (anchor.price_band, anchor.liquidity_band)
        if c.match_tier == "PRICE+LIQUIDITY+SECTOR":
            assert c.sector33 == anchor.sector33
    assert Counter(c.match_tier for c in controls)["PRICE+LIQUIDITY+SECTOR"] > 0

    # A sector no Primary shares costs no sample: the match falls back to price and liquidity.
    elsewhere = {code: replace(issue, sector33="鉱業") if code in failing else issue for code, issue in issues.items()}
    moved = [s for s in _select(screens, elsewhere).samples if s.cohort == "CONTROL"]
    assert len(moved) == 20 and "PRICE+LIQUIDITY+SECTOR" not in {s.match_tier for s in moved}

    # Equals (same price band, liquidity band and sector): the lowest key is taken.
    tiny = [Screen(str(5000 + i), S0, True, i < 3, Decimal(800), routes=["A"] if i < 3 else [],
                   turnover_avg_20d=5000.0) for i in range(6)]
    tiny_issues = {s.code: ListedIssue(s.code, f"銘柄{s.code}", "PRIME", "機械", "TOPIX Small 1") for s in tiny}
    (control,) = [s for s in _select(tiny, tiny_issues).samples if s.cohort == "CONTROL"]
    lowest = min(("5003", "5004", "5005"), key=lambda code: selection_key(VERSION, SEED, S0, "CONTROL", code))
    assert control.code == lowest and control.match_tier == "PRICE+LIQUIDITY+SECTOR"


def test_the_draw_reads_nothing_but_the_day_s_screening():
    assert list(inspect.signature(select_day).parameters) == ["screens", "issues", "s0", "evaluation_version",
                                                              "experiment_seed"]
    # Two securities that are the same up to S0 and part ways after it screen - and are drawn - alike.
    days = business_days(date(2025, 8, 1), date(2026, 10, 30))
    s0_index = days.index(S0)
    base = walk(len(days), seed=4)
    later = base[:s0_index + 1] + [x * 3 for x in base[s0_index + 1:]]
    a, b = screen(history("6001", days=days, closes=base), S0), screen(history("6001", days=days, closes=later), S0)
    assert (a.eligible, a.passed, a.routes, a.close_as_traded) == (b.eligible, b.passed, b.routes, b.close_as_traded)


def test_dates_before_the_prospective_start_are_refused(tmp_path, monkeypatch):
    for day, reason in ((date(2026, 9, 18), "prospective start"), (date(2026, 9, 23), "prospective start"),
                        (date(2026, 9, 26), "weekend")):
        with pytest.raises(SelectionError, match=reason):
            check_s0(day)
    screens, issues = _day_screens(passing=10, failing=10, s0=date(2026, 9, 23))
    with pytest.raises(SelectionError, match="2026-09-24"):
        _select(screens, issues, s0=date(2026, 9, 23))
    assert PHASE_B_START == date(2026, 9, 24)
    assert phase_b.schedule(datetime(2026, 9, 19, 12, 0, tzinfo=UTC)) == {
        "s0": None, "window_open": False, "next_s0": "2026-09-24", "opens_at": "2026-09-24T07:10:00+00:00",
        "closes_at": "2026-09-25T00:00:00+00:00"}

    env = _setup(tmp_path, monkeypatch, n=8)
    with pytest.raises(jev_eval.EvaluationError, match="prospective start"):
        _plan(env, date(2026, 9, 23), clock=lambda: datetime(2026, 9, 23, 17, 0, tzinfo=JST))
    with pytest.raises(jev_eval.EvaluationError, match="final from"):  # 16:10 JST, not before
        _plan(env, S0, clock=lambda: datetime(2026, 9, 24, 16, 5, tzinfo=JST))
    with pytest.raises(jev_eval.SendWindowClosed, match="S1 may have opened"):
        _plan(env, S0, clock=lambda: datetime(2026, 9, 25, 9, 0, tzinfo=JST))
    assert not (phase_b.day_store(env.root, COHORT, S0).path / "manifest.json").exists()


# ----------------------------------------------------------------- a day, end to end (offline)


class _FakeYanoshinRecent:
    """One title in S0's window that names the company, one outside it, and another issuer's."""

    limit = 300

    def fetch_for_codes(self, codes):
        (code,) = codes
        items = [SimpleNamespace(yanoshin_id=1, pubdate=datetime(2026, 9, 10, 15, 0, tzinfo=JST),
                                 title=f"銘柄{code}株式会社 月次報告", code=SimpleNamespace(normalised=code)),
                 SimpleNamespace(yanoshin_id=2, pubdate=datetime(2026, 5, 1, 15, 0, tzinfo=JST),
                                 title="old title", code=SimpleNamespace(normalised=code)),
                 SimpleNamespace(yanoshin_id=3, pubdate=datetime(2026, 9, 11, 15, 0, tzinfo=JST),
                                 title="another issuer's title", code=SimpleNamespace(normalised="9999"))]
        return SimpleNamespace(items=items, fetched_at=EVENING, endpoint=f"https://example.invalid/{code}.json",
                               response_sha256="0" * 64, total_count=len(items))


class _PinnedTransport:
    """TypeSafe's API: answers every request, served by ``served(n)`` for the n-th (1-based)."""

    def __init__(self, served=lambda n: "jev-1.13.0"):
        self.served = served
        self.models, self.auth = [], set()

    def __call__(self, url, data, headers, timeout):
        self.models.append(json.loads(data)["model"])
        self.auth.add(headers.get("Authorization"))
        n = len(self.models)
        body = _direct_answer_body(model=self.served(n), tokens=_tokens_within_estimate(data))
        return 200, {"x-typesafe-request-id": f"req_{n}"}, json.dumps(body).encode("utf-8")


def _universe(n: int, *, until: date = S0, holidays: bool = False):
    codes = [str(3000 + i) for i in range(n)]
    days = business_days(date(2025, 8, 1), until)
    if holidays:  # no bars on JPX's closed days, as the real market (the calendar covers 2026 on)
        days = [d for d in days if d.year < 2026 or jpx_calendar.is_business_day(d)]
    charts = {f"{c}.T": chart(days, walk(len(days), start=300.0 + (i * 37) % 2000, seed=i),
                              volumes=[50_000 + 1000 * (i % 17)] * len(days)) for i, c in enumerate(codes)}
    issues = [ListedIssue(c, f"銘柄{c}株式会社", "PRIME", SECTORS[i % len(SECTORS)], "TOPIX Small 1")
              for i, c in enumerate(codes)]
    return codes, charts, issues


def _setup(tmp_path, monkeypatch, *, n=40, seed=SEED, until=S0, holidays=False):
    codes, charts, issues = _universe(n, until=until, holidays=holidays)
    real_screen = jev_eval.screen

    def screen_with_known_routes(h, s0):
        # The plumbing is under test here, not the routes: every other eligible security passes.
        verdict = real_screen(h, s0)
        if not verdict.eligible:
            return verdict
        k = int(h.code)
        if k % 2:
            return replace(verdict, passed=False, routes=[], route_evidence={})
        routes = ROUTE_PATTERN[k // 2 % len(ROUTE_PATTERN)]
        return replace(verdict, passed=True, routes=list(routes), route_evidence={r: {"x": 1.0} for r in routes})

    monkeypatch.setattr(jev_eval, "screen", screen_with_known_routes)
    monkeypatch.setattr(jev_eval, "_o200k", lambda text: len(text) // 3)
    monkeypatch.setenv("TYPESAFE_API_KEY", FAKE_KEY)
    jev_eval.phase_b_init(tmp_path, COHORT, experiment_seed=seed)
    record_credit_snapshot(tmp_path, balance_usd=Decimal("5.00"), confirmed_at=datetime(2026, 9, 19, 8, 43, 13,
                                                                                        tzinfo=UTC),
                           expires_at=datetime(2026, 10, 19, tzinfo=UTC), displayed_expiry="Oct 19, 2026",
                           source="test", now=datetime(2026, 9, 19, 9, 0, tzinfo=UTC))
    return SimpleNamespace(root=tmp_path, codes=codes, charts=charts, issues=issues)


def _plan(env, s0=S0, *, clock=lambda: EVENING):
    return jev_eval.plan_day(env.root, COHORT, s0, clock=clock, yahoo=_FakeYahoo(env.charts),
                             fetch_issues=lambda: (env.issues, "f" * 64), sleep=lambda _s: None)


def _planned_and_built(env, s0=S0):
    _plan(env, s0)
    store = phase_b.day_store(env.root, COHORT, s0)
    jev_eval.build(store, yanoshin=_FakeYanoshinRecent())
    return store


def _paced(start: datetime):
    """A pacer and a wall clock that move together, only when something sleeps."""

    fake = _FakeTime()
    pacer = Pacer(min_interval=5.0, window=60.0, max_in_window=12, clock=fake.clock, sleep=fake.sleep)
    return pacer, (lambda: start + timedelta(seconds=fake.now - 1000.0))


def _records(store):
    return sorted((json.loads(p.read_text(encoding="utf-8")) for p in (store.path / "responses").glob("*.json")
                   if ".raw" not in p.name), key=lambda r: r["pacing"]["send_index"])


@pytest.fixture()
def no_network(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise AssertionError("the evaluation tried to open a network connection")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)


def _repo_status() -> set[str]:
    out = subprocess.run(["git", "status", "--porcelain", "--untracked-files=all"], cwd=REPO_ROOT,
                         capture_output=True, text=True, check=False).stdout
    return set(out.splitlines())


def test_a_day_sends_92_requests_pinned_to_jev_1_13_0_and_writes_no_database(tmp_path, monkeypatch, no_network):
    repo_before = _repo_status()
    env = _setup(tmp_path, monkeypatch, n=170)
    pacer, clock = _paced(EVENING)
    transport = _PinnedTransport()
    result = jev_eval.phase_b_day(env.root, COHORT, S0, send=True, clock=clock, yahoo=_FakeYahoo(env.charts),
                                  fetch_issues=lambda: (env.issues, "f" * 64), yanoshin=_FakeYanoshinRecent(),
                                  transport=transport, pacer=pacer, sleep=lambda _s: None)
    plan = result["plan"]
    assert plan["selected"] == {"primary": 60, "control": 20, "anonymized": 8, "drift": 4}
    assert plan["requests"] == 92 and plan["coverage"] == 1.0
    assert result["build"]["by_variant"] == {"main": 80, "anonymized": 8, "drift": 4}
    checked = result["preflight"]
    assert checked["ready_to_send"] and checked["violations"] == 0 and checked["requests_total"] == 92
    assert checked["samples"] == {"PRIMARY": 60, "CONTROL": 20}
    run = result["run"]
    assert run["stopped"] is None and run["requests_answered"] == 92 and run["served_model"] == "jev-1.13.0"

    # Pinned: every request asked for jev-1.13.0 and every answer came from it; one every 5 seconds.
    assert transport.models == ["jev-1.13.0"] * 92 and transport.auth == {f"Bearer {FAKE_KEY}"}
    store = phase_b.day_store(env.root, COHORT, S0)
    records = _records(store)
    assert [r["model"] for r in records] == ["jev-1.13.0"] * 92
    assert {r["requested_model"] for r in records} == {"jev-1.13.0"}
    assert [r["pacing"]["waited_seconds"] for r in records] == [0.0] + [5.0] * 91
    assert all(Decimal(r["ledger_usd"]) == Decimal(r["cost_usd"]) for r in records)

    # Everything kept before the draw: every screening result and every candidate with its route membership.
    candidates = store.read_jsonl("candidates.jsonl")
    assert len(candidates) == plan["eligible"] and sum(c["selected"] for c in candidates) == 80
    assert {c["pool"] for c in candidates if c["passed"]} == {"PRIMARY_POOL"}
    assert store.exists("screening.parquet") and store.exists("inputs/prices.jsonl.gz")
    manifest = store.read_json("manifest.json")
    assert manifest["phase"] == "B" and manifest["model"] == "jev-1.13.0" and manifest["fallback_provider"] is None
    assert manifest["storage"] == {"database_writes": "none", "teacher_admissible": False}

    # The anonymized state names neither the company nor its code; the main state keeps both.
    anonymized = next(r for r in store.read_jsonl("requests.jsonl") if r["variant"] == "anonymized")
    body = json.loads((store.path / anonymized["file"]).read_bytes())
    code = next(s for s in store.read_jsonl("population.jsonl") if s["sample_id"] == anonymized["sample_id"])["code"]
    assert body["state"]["security"]["code"] == "[CODE]"
    assert [t["title"] for t in body["state"]["tdnet_disclosure_titles_up_to_cutoff"]] == ["[COMPANY] 月次報告"]
    assert code not in json.dumps(body["state"]["tdnet_disclosure_titles_up_to_cutoff"], ensure_ascii=False)

    # No teacher data, no database, nothing outside the evaluation root.
    for name in ("population.jsonl", "candidates.jsonl", "predictions.jsonl"):
        assert all(row["teacher_admissible"] is False for row in store.read_jsonl(name))
    assert _repo_status() == repo_before
    written = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert all((tmp_path / "evaluation" / "jev") in p.parents for p in written)
    everything = "".join(p.read_text(encoding="utf-8", errors="ignore") for p in written
                         if p.suffix == ".json" and "requests" not in p.parts)
    assert FAKE_KEY not in everything


class _FlakyYahoo(_FakeYahoo):
    """Yahoo, except that some symbols never come back: the transport itself fails for them."""

    def __init__(self, charts, failing):
        super().__init__(charts)
        self.failing = failing

    def chart(self, symbol, params):
        if symbol in self.failing:
            self.calls += 1
            raise OSError("connection reset by peer")
        return super().chart(symbol, params)


def test_a_security_that_cannot_be_read_is_recorded_and_the_day_goes_on(tmp_path, monkeypatch):
    env = _setup(tmp_path, monkeypatch)
    yahoo = _FlakyYahoo(env.charts, {"3007.T"})
    summary = jev_eval.plan_day(env.root, COHORT, S0, clock=lambda: EVENING, yahoo=yahoo,
                                fetch_issues=lambda: (env.issues, "f" * 64), sleep=lambda _s: None)
    assert summary["failed"] == 1 and summary["coverage"] == 39 / 40
    universe = phase_b.day_store(env.root, COHORT, S0).read_json("inputs/universe.json")
    assert universe["failures"] == [{"code": "3007", "error": "OSError: connection reset by peer"}]
    # Too much of the universe unread: the day is not drawn from, and nothing is written.
    other = _setup(tmp_path / "short", monkeypatch)
    with pytest.raises(jev_eval.EvaluationError, match="below 95%"):
        jev_eval.plan_day(other.root, COHORT, S0, clock=lambda: EVENING,
                          yahoo=_FlakyYahoo(other.charts, {"3001.T", "3002.T", "3003.T"}),
                          fetch_issues=lambda: (other.issues, "f" * 64), sleep=lambda _s: None)
    assert not phase_b.day_store(other.root, COHORT, S0).path.exists()


def test_no_day_is_planned_once_the_cohort_has_its_25_business_days(tmp_path, monkeypatch):
    env = _setup(tmp_path, monkeypatch, n=8)
    days = business_days(date(2026, 9, 24), date(2026, 12, 31))
    stopped, sent, after = days[0], days[1:26], days[26]
    phase_b.day_store(env.root, COHORT, stopped).write_json("run-stopped-1.json", {"stopped": "x"})
    for day in sent:
        phase_b.day_store(env.root, COHORT, day).write_json("stage-run.json", {"stage": "run"})
    # Only days sent in full count; a stopped day does not.
    assert phase_b.completed_days(env.root, COHORT) == sent
    with pytest.raises(jev_eval.EvaluationError, match="has its 25 business days"):
        _plan(env, after, clock=lambda: datetime(after.year, after.month, after.day, 17, 0, tzinfo=JST))


def test_the_scheduler_sends_nothing_unless_asked(tmp_path, monkeypatch, no_network):
    env = _setup(tmp_path, monkeypatch)

    def never(*_args):
        raise AssertionError("a request was sent")

    result = jev_eval.phase_b_day(env.root, COHORT, None, send=False, clock=lambda: EVENING,
                                  yahoo=_FakeYahoo(env.charts), fetch_issues=lambda: (env.issues, "f" * 64),
                                  yanoshin=_FakeYanoshinRecent(), transport=never)
    assert result["s0"] == "2026-09-24" and result["preflight"]["ready_to_send"] and "run" not in result
    closed = jev_eval.phase_b_day(env.root, COHORT, None, send=True,
                                  clock=lambda: datetime(2026, 9, 25, 12, 0, tzinfo=JST), transport=never)
    assert closed["done"].startswith("nothing") and closed["scheduled"]["next_s0"] == "2026-09-25"
    # The same day planned again is refused; its files are written once.
    with pytest.raises(StoreError, match="already been planned"):
        _plan(env)


def test_a_day_draws_the_same_sample_every_time_and_preflight_draws_it_again(tmp_path, monkeypatch):
    first = _setup(tmp_path / "a", monkeypatch)
    second = _setup(tmp_path / "b", monkeypatch)
    _plan(first)
    _plan(second)
    stores = [phase_b.day_store(env.root, COHORT, S0) for env in (first, second)]
    assert stores[0].read_jsonl("population.jsonl") == stores[1].read_jsonl("population.jsonl")
    assert stores[0].read_jsonl("candidates.jsonl") == stores[1].read_jsonl("candidates.jsonl")

    jev_eval.build(stores[0], yanoshin=_FakeYanoshinRecent())
    checked = jev_eval.preflight(stores[0], budget=DAY_BUDGET, clock=lambda: EVENING)
    assert checked["selection_reproduced"] and checked["ready_to_send"]
    # A draw that does not come out the same (here: another rule) is caught before anything is sent.
    real = jev_eval.select_day
    monkeypatch.setattr(jev_eval, "select_day", lambda *a, **k: real(*a, **{**k, "experiment_seed": "other"}))
    tampered = jev_eval.preflight(stores[0], budget=DAY_BUDGET, clock=lambda: EVENING)
    assert not tampered["selection_reproduced"] and not tampered["ready_to_send"]


def test_a_changed_served_version_stops_the_day_and_the_cohort(tmp_path, monkeypatch):
    env = _setup(tmp_path, monkeypatch)
    store = _planned_and_built(env)
    pacer, clock = _paced(EVENING)
    assert jev_eval.preflight(store, budget=DAY_BUDGET, clock=clock)["ready_to_send"]
    transport = _PinnedTransport(served=lambda n: "jev-1.13.0" if n < 4 else "jev-1.14.0")
    summary = jev_eval.run(store, budget=DAY_BUDGET, pacer=pacer, transport=transport, clock=clock)
    assert "jev-1.13.0 to jev-1.14.0" in summary["stopped"] and summary["requests_answered"] == 4
    assert len(transport.models) == 4  # nothing more was sent
    stop = store.read_json("run-stopped-1.json")
    assert stop["last_request"]["cohort_stop"] == "cohort-stopped-1.json"
    (cohort_stop,) = phase_b.cohort_stops(env.root, COHORT)
    assert cohort_stop["reason"] == "served model is not the pinned one" and "jev-1.14.0" in cohort_stop["detail"]
    # No day of this cohort runs any more, and the day itself is not resumed.
    with pytest.raises(jev_eval.EvaluationError, match="stopped"):
        _plan(env, date(2026, 9, 25), clock=lambda: datetime(2026, 9, 25, 17, 0, tzinfo=JST))
    with pytest.raises(jev_eval.EvaluationError, match="stopped"):
        jev_eval.preflight(store, budget=DAY_BUDGET, clock=clock)
    with pytest.raises(jev_eval.EvaluationError, match="stopped"):
        jev_eval.run(store, budget=DAY_BUDGET, pacer=pacer, transport=transport, clock=clock)
    assert jev_eval.phase_b_status(env.root, COHORT, clock=clock)["stops"] == [cohort_stop]


def test_the_gateway_cannot_join_a_phase_b_cohort(tmp_path, monkeypatch):
    env = _setup(tmp_path, monkeypatch)
    store = _planned_and_built(env)
    with pytest.raises(jev_eval.EvaluationError, match="carry no version"):
        jev_eval.preflight(store, budget=DAY_BUDGET, provider="vercel-ai-gateway", clock=lambda: EVENING)
    assert jev_eval.preflight(store, budget=DAY_BUDGET, clock=lambda: EVENING)["ready_to_send"]
    with pytest.raises(jev_eval.EvaluationError, match="preflight checked typesafe-direct"):
        jev_eval.run(store, budget=DAY_BUDGET, provider="vercel-ai-gateway", clock=lambda: EVENING)


def _spent_elsewhere(env, usd: str, day: date = date(2026, 9, 30)) -> None:
    """A record of the cohort's spend on another day, as a run would have left it."""

    responses = phase_b.day_store(env.root, COHORT, day).path / "responses"
    responses.mkdir(parents=True, exist_ok=True)
    (responses / f"x{len(list(responses.glob('*')))}.main.json").write_text(json.dumps({
        "label": "x.main", "via": "typesafe-direct", "sent_at": "2026-09-30T08:00:00+00:00", "cost_usd": usd,
        "ledger_usd": usd}), encoding="utf-8")


def test_the_cohort_never_spends_past_2_50(tmp_path, monkeypatch):
    phase_b.check_cap(Decimal("2.499"), Decimal("0.001"))  # exactly $2.50 is allowed
    with pytest.raises(BudgetExceeded, match=r"\$2.50 cap"):
        phase_b.check_cap(Decimal("2.4995"), Decimal("0.001"))

    env = _setup(tmp_path, monkeypatch)
    store = _planned_and_built(env)
    day_estimate = sum(Decimal(r["estimated_usd"]) for r in store.read_jsonl("requests.jsonl"))
    _spent_elsewhere(env, str(Decimal("2.50") - day_estimate + Decimal("0.000001")))
    checked = jev_eval.preflight(store, budget=DAY_BUDGET, clock=lambda: EVENING)
    assert not checked["ready_to_send"]
    assert any("$2.50 cap" in p for p in checked["budget_problems"])
    assert Decimal(checked["global_cap"]["after_this_day_estimated_usd"]) > Decimal("2.50")
    with pytest.raises(jev_eval.EvaluationError, match="not ready"):
        jev_eval.run(store, budget=DAY_BUDGET, transport=_PinnedTransport(), clock=lambda: EVENING)

    # Room for the day after all: it is sent, and the cohort total stays inside the cap.
    other = _setup(tmp_path / "room", monkeypatch)
    room_store = _planned_and_built(other)
    day_estimate = sum(Decimal(r["estimated_usd"]) for r in room_store.read_jsonl("requests.jsonl"))
    _spent_elsewhere(other, str(Decimal("2.50") - day_estimate - Decimal("0.001")))
    pacer, clock = _paced(EVENING)
    assert jev_eval.preflight(room_store, budget=DAY_BUDGET, clock=clock)["ready_to_send"]
    # Spend recorded after the preflight (another process, say) is caught before the first request.
    _spent_elsewhere(other, "0.01")
    with pytest.raises(jev_eval.EvaluationError, match="cap"):
        jev_eval.run(room_store, budget=DAY_BUDGET, pacer=pacer, transport=_PinnedTransport(), clock=clock)
    assert phase_b.cohort_spend(other.root, COHORT) <= Decimal("2.50")


def test_nothing_is_sent_once_s1_may_have_opened(tmp_path, monkeypatch):
    env = _setup(tmp_path, monkeypatch)
    store = _planned_and_built(env)
    pacer, clock = _paced(datetime(2026, 9, 25, 8, 59, 40, tzinfo=JST))
    assert jev_eval.preflight(store, budget=DAY_BUDGET, clock=clock)["ready_to_send"]
    transport = _PinnedTransport()
    summary = jev_eval.run(store, budget=DAY_BUDGET, pacer=pacer, transport=transport, clock=clock)
    assert len(transport.models) == 4 and "S1 may have opened" in summary["stopped"]  # :40 :45 :50 :55
    assert max(r["pacing"]["requested_at"] for r in _records(store)) < "2026-09-25T09:00:00+09:00"
    assert not store.exists("stage-run.json")
    late = jev_eval.preflight(store, budget=DAY_BUDGET, clock=lambda: datetime(2026, 9, 25, 9, 0, tzinfo=JST))
    assert "the send window has closed: S1 may have opened" in late["window_problems"]
    assert any("not resumed" in p for p in late["limit_problems"])
    # The scheduler reports a stopped day instead of sending the rest of it.
    again = jev_eval.phase_b_day(env.root, COHORT, S0, send=True, transport=transport, clock=clock)
    assert "S1 may have opened" in again["stopped"]["stopped"] and len(transport.models) == 4


def test_an_expired_credit_stops_the_run_until_the_new_balance_is_confirmed(tmp_path, monkeypatch):
    env = _setup(tmp_path, monkeypatch)
    store = _planned_and_built(env)
    record_credit_snapshot(env.root, balance_usd=Decimal("4.99"), confirmed_at=datetime(2026, 9, 20, tzinfo=UTC),
                           expires_at=datetime(2026, 9, 24, 10, 0, tzinfo=UTC), displayed_expiry="test",
                           source="test", now=datetime(2026, 9, 24, 9, 0, tzinfo=UTC))
    pacer, clock = _paced(datetime(2026, 9, 24, 18, 59, 45, tzinfo=JST))
    assert jev_eval.preflight(store, budget=DAY_BUDGET, clock=clock)["ready_to_send"]
    transport = _PinnedTransport()
    summary = jev_eval.run(store, budget=DAY_BUDGET, pacer=pacer, transport=transport, clock=clock)
    assert len(transport.models) == 3 and "credit expired" in summary["stopped"]
    expired = jev_eval.preflight(store, budget=DAY_BUDGET, clock=lambda: datetime(2026, 9, 24, 19, 1, tzinfo=JST))
    assert not expired["ready_to_send"] and "confirm the new console balance once" in expired["budget_problems"][0]


def test_phase_b_is_frozen_once_its_cohort_exists(tmp_path, monkeypatch):
    env = _setup(tmp_path, monkeypatch, n=8)
    cohort = env.root / "evaluation" / "jev" / COHORT / "cohort.json"
    frozen = json.loads(cohort.read_text(encoding="utf-8"))
    assert frozen["requested_model"] == frozen["pinned_served_model"] == "jev-1.13.0"
    assert frozen["per_day"] == {"primary": 60, "control": 20, "anonymized": 8, "drift": 4, "max_requests": 92}
    assert frozen["budget"]["global_hard_cap_usd"] == "2.50" and frozen["prospective_start"] == "2026-09-24"
    assert set(frozen["preregistered"]["route_d_subgroups"]) == {"D_ONLY", "D_PLUS_OTHER", "NON_D"}
    assert "a probability threshold" in frozen["not_introduced"]
    assert set(frozen["frozen"]) >= {"method", "question_schema_hash", "state_version", "screener", "selection",
                                     "outcome", "decision_interpretation", "files_sha256"}
    with pytest.raises(StoreError):
        jev_eval.phase_b_init(env.root, COHORT, experiment_seed="another")

    monkeypatch.setattr(phase_b, "question_schema_hash", lambda: "0" * 64)
    with pytest.raises(jev_eval.EvaluationError, match=r"frozen.*question_schema_hash"):
        _plan(env)
    monkeypatch.undo()
    env = _setup(tmp_path / "files", monkeypatch, n=8)
    real = phase_b._sha256
    monkeypatch.setattr(phase_b, "_sha256", lambda path: "0" * 64 if path.name == "engine.py" and
                        path.parent.name == "routes" else real(path))
    with pytest.raises(jev_eval.EvaluationError, match="routes/engine.py"):
        _plan(env)


def _evening(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, 17, 0, tzinfo=JST)


def _send_day(env, s0: date = S0):
    """Plan, build and send one day on its evening, every answer from jev-1.13.0."""

    jev_eval.plan_day(env.root, COHORT, s0, clock=lambda: _evening(s0), yahoo=_FakeYahoo(env.charts),
                      fetch_issues=lambda: (env.issues, "f" * 64), sleep=lambda _s: None)
    store = phase_b.day_store(env.root, COHORT, s0)
    jev_eval.build(store, yanoshin=_FakeYanoshinRecent())
    pacer, clock = _paced(_evening(s0))
    assert jev_eval.preflight(store, budget=DAY_BUDGET, clock=clock)["ready_to_send"]
    assert jev_eval.run(store, budget=DAY_BUDGET, pacer=pacer, transport=_PinnedTransport(), clock=clock)[
        "stopped"] is None
    return store


def test_outcomes_after_t_plus_20_and_the_report_by_route_d_subgroup(tmp_path, monkeypatch):
    env = _setup(tmp_path, monkeypatch, until=date(2026, 11, 30), holidays=True)
    store = _send_day(env)

    # T+20 of 2026-09-24 by JPX's calendar is 10-23 (10-12 is closed); not before its close is final.
    for when in (datetime(2026, 10, 22, 17, 0, tzinfo=JST), datetime(2026, 10, 23, 12, 0, tzinfo=JST)):
        with pytest.raises(jev_eval.OutcomeNotDue, match="by JPX's calendar"):
            jev_eval.freeze_outcomes(store, clock=lambda w=when: w, yahoo=_FakeYahoo(env.charts),
                                     sleep=lambda _s: None)
    # Due by the calendar, but the prices read lack a session (an unscheduled closure): not frozen either.
    _, short, _ = _universe(len(env.codes), until=date(2026, 10, 22), holidays=True)
    with pytest.raises(jev_eval.OutcomeNotDue, match="in the prices read"):
        jev_eval.freeze_outcomes(store, clock=lambda: datetime(2026, 10, 23, 17, 0, tzinfo=JST),
                                 yahoo=_FakeYahoo(short), sleep=lambda _s: None)
    assert not (store.path / "outcome-attempts").exists()
    frozen = jev_eval.freeze_outcomes(store, clock=lambda: datetime(2026, 10, 23, 17, 0, tzinfo=JST),
                                      yahoo=_FakeYahoo(env.charts), sleep=lambda _s: None)
    assert frozen["t_plus_20"] == "2026-10-23" and frozen["answers_read"] is False and frozen["attempt"] == 1
    outcomes = store.read_jsonl(frozen["outcomes_file"])
    assert frozen["outcomes_file"] == "outcome-attempts/1/outcomes.jsonl"
    assert len(outcomes) == len(store.read_jsonl("population.jsonl"))
    for o in outcomes:
        assert o["teacher_admissible"] is False and o["window_first"] == "2026-09-25"
        assert o["window_last"] == "2026-10-23" and "success_label" not in o
        assert {f"ret_t{n}" for n in (1, 3, 5, 10, 20)} | {"hit_20_high", "hit_20_close", "max_upside_high",
                                                            "max_upside_close", "max_drawdown_low",
                                                            "max_drawdown_close"} <= set(o)

    result = jev_eval.phase_b_report(env.root, COHORT, clock=lambda: datetime(2026, 10, 23, 18, 0, tzinfo=JST))
    assert result["phase"] == "B" and "not connected to production" in result["banner"]
    assert result["status"] == "partial" and result["progress"]["days"]["with_outcomes"] == ["2026-09-24"]
    groups = result["route_d_subgroups"]
    primary = [s for s in store.read_jsonl("population.jsonl") if s["cohort"] == "PRIMARY"]
    joined = result["pipeline"]["outcome_join"]["by_cohort"]["PRIMARY"]
    assert sum(groups[g]["n"] for g in ("D_ONLY", "D_PLUS_OTHER", "NON_D")) == joined
    assert {g: groups[g]["n"] for g in ("D_ONLY", "D_PLUS_OTHER", "NON_D")} == dict(
        Counter(s["route_d_subgroup"] for s in primary if s["sample_id"] in {o["sample_id"] for o in outcomes
                                                                              if o["resolution"] == "RESOLVED"}))
    d_only = groups["D_ONLY"]
    assert set(d_only) >= {"hit_20_high", "hit_20_close", "mean_reaches_target", "decision_counts", "upside_score",
                           "max_upside", "max_drawdown"}
    assert "reaches_target_calibration" in d_only["hit_20_high"]
    assert result["teacher_admissible"] is False
    assert (env.root / "evaluation" / "jev" / COHORT / "report-1.md").exists()
    status = jev_eval.phase_b_status(env.root, COHORT, clock=lambda: datetime(2026, 10, 23, 18, 0, tzinfo=JST))
    assert status["days_sent"] == 1 and status["frozen_protocol"] == "holds"
    assert status["answered"]["PRIMARY"] + status["answered"]["CONTROL"] == len(store.read_jsonl("population.jsonl"))
    assert status["latest_report"]["status"] == "partial"


def test_nothing_in_the_evaluation_can_write_to_a_database():
    forbidden = ("psycopg", "psycopg2", "asyncpg", "sqlalchemy", "supabase", "surge.market.db", "surge.entry.db",
                 "surge.runtime")
    sources = sorted((REPO_ROOT / "workers" / "src" / "surge" / "evaluation").glob("*.py"))
    sources.append(REPO_ROOT / "workers" / "src" / "surge" / "jobs" / "jev_eval.py")
    for path in sources:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = ([a.name for a in node.names] if isinstance(node, ast.Import)
                     else [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
            for name in names:
                assert not name.startswith(forbidden) and not name.endswith(".db"), f"{path.name} imports {name}"
    # Nor anything they import in turn.
    probe = ("import sys, surge.jobs.jev_eval; print([m for m in sys.modules if m.split('.')[0] in "
             "{'psycopg', 'psycopg2', 'asyncpg', 'sqlalchemy', 'supabase'} or m.endswith('.db')])")
    out = subprocess.run([sys.executable, "-c", probe], cwd=REPO_ROOT / "workers", capture_output=True, text=True,
                         check=True, env={**__import__("os").environ, "PYTHONPATH": str(REPO_ROOT / "workers" / "src")})
    assert out.stdout.strip() == "[]"


# ----------------------------------------------------------------- the JPX calendar and the scheduled job (D-277)


def _calendar_page(closed, *, stray_quote_on=None):
    rows = []
    for day, name in sorted(closed.items()):
        weekday = "月火水木金土日"[day.weekday()]
        cell_end = '</td">' if day == stray_quote_on else "</td>"
        rows.append(f'<tr><td class="a-center">{day:%Y/%m/%d}（{weekday}）{cell_end}<td class="a-center">{name}</td></tr>')
    return "<html><body><h2>休業日一覧</h2><table>" + "\r\n".join(rows) + "</table></body></html>"


def test_the_jpx_calendar_is_weekends_and_jpx_s_closed_days_and_nothing_it_does_not_cover():
    from surge.evaluation import jpx_calendar

    assert [jpx_calendar.closed_reason(d) for d in (date(2026, 9, 21), date(2026, 9, 22), date(2026, 9, 23))] == [
        "敬老の日", "休日※", "秋分の日"]
    assert jpx_calendar.closed_reason(date(2026, 10, 12)) == "スポーツの日"
    assert jpx_calendar.closed_reason(date(2026, 9, 26)) == "weekend"
    assert jpx_calendar.is_business_day(date(2026, 9, 24)) and jpx_calendar.is_business_day(date(2026, 10, 13))
    with pytest.raises(jpx_calendar.CalendarUnknown, match="2026, 2027"):
        jpx_calendar.closed_reason(date(2028, 1, 4))

    # The page as JPX publishes it, stray quote in the 2026/12/31 row included, reads back exactly.
    page = _calendar_page(jpx_calendar.CLOSED_DAYS, stray_quote_on=date(2026, 12, 31))
    assert jpx_calendar.parse_closed_days(page) == jpx_calendar.CLOSED_DAYS
    assert jpx_calendar.diff_against_page(page) == {"added_on_page": [], "removed_from_page": [], "renamed": [],
                                                    "years_on_page": [2026, 2027]}
    changed = {**{d: n for d, n in jpx_calendar.CLOSED_DAYS.items() if d != date(2026, 11, 23)},
               date(2026, 12, 30): "休業日", date(2026, 10, 12): "改称"}
    assert jpx_calendar.diff_against_page(_calendar_page(changed)) == {
        "added_on_page": ["2026-12-30"], "removed_from_page": ["2026-11-23"], "renamed": ["2026-10-12"],
        "years_on_page": [2026, 2027]}

    fetched = SimpleNamespace(status=200, body=page.encode("utf-8"), sha256="0" * 64)
    result = jev_eval.check_calendar(fetch=lambda url: fetched)
    assert result["matches"] and not result["same_as_read_on"]
    assert not jev_eval.check_calendar(fetch=lambda url: SimpleNamespace(
        status=200, body=_calendar_page(changed).encode("utf-8"), sha256="1" * 64))["matches"]


def test_a_jpx_closed_day_is_never_planned(tmp_path, monkeypatch):
    env = _setup(tmp_path, monkeypatch, n=8)
    yahoo = _FakeYahoo(env.charts)
    with pytest.raises(jev_eval.ClosedDay, match="スポーツの日"):
        jev_eval.plan_day(env.root, COHORT, date(2026, 10, 12), clock=lambda: datetime(2026, 10, 12, 17, 0, tzinfo=JST),
                          yahoo=yahoo, fetch_issues=lambda: (env.issues, "f" * 64), sleep=lambda _s: None)
    assert yahoo.calls == 0


def _scheduled(env, start, *, transport=None, yahoo=None, **kwargs):
    pacer, clock = _paced(start)
    return jev_eval.phase_b_scheduled(env.root, COHORT, clock=clock, sleep=pacer.sleep,
                                      yahoo=yahoo or _FakeYahoo(env.charts),
                                      fetch_issues=lambda: (env.issues, "f" * 64), yanoshin=_FakeYanoshinRecent(),
                                      transport=transport if transport is not None else _PinnedTransport(),
                                      pacer=pacer, **kwargs)


def _scheduler_records(env):
    runs = env.root / "evaluation" / "jev" / COHORT / "scheduler" / "runs"
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(runs.glob("*.json"))]


def test_the_scheduled_job_sends_nothing_before_2026_09_24_nor_on_a_closed_day(tmp_path, monkeypatch, no_network):
    env = _setup(tmp_path, monkeypatch)
    yahoo, transport = _FakeYahoo(env.charts), _PinnedTransport()
    for start, closed in ((datetime(2026, 9, 19, 16, 10, tzinfo=JST), "weekend"),
                          (datetime(2026, 9, 21, 16, 10, 5, tzinfo=JST), "敬老の日"),
                          (datetime(2026, 9, 22, 16, 10, tzinfo=JST), "休日※"),
                          (datetime(2026, 9, 23, 16, 10, tzinfo=JST), "秋分の日")):
        record = _scheduled(env, start, yahoo=yahoo, transport=transport)
        assert (record["status"], record["exit_code"], record["closed"]) == ("closed_day", 0, closed)
        assert record["schedule"]["next_s0"] == "2026-09-24"
    # A business day before the window opens (more than 15 minutes early): nothing either.
    early = _scheduled(env, datetime(2026, 9, 24, 12, 0, tzinfo=JST), yahoo=yahoo, transport=transport)
    assert (early["status"], early["exit_code"]) == ("no_window", 0)
    assert yahoo.calls == 0 and transport.models == []
    assert [r["status"] for r in _scheduler_records(env)] == ["closed_day"] * 4 + ["no_window"]
    assert not (env.root / "evaluation" / "jev" / COHORT / "days").exists()


def test_the_scheduled_job_waits_for_the_window_sends_the_day_once_and_then_leaves_it_alone(tmp_path, monkeypatch):
    env = _setup(tmp_path, monkeypatch)
    transport = _PinnedTransport()
    first = _scheduled(env, datetime(2026, 9, 24, 16, 9, 30, tzinfo=JST), transport=transport)  # 30 s early
    assert (first["status"], first["exit_code"], first["s0"]) == ("sent", 0, "2026-09-24")
    assert first["detail"]["requests_answered"] == 30 and first["detail"]["served_model"] == "jev-1.13.0"
    assert len(transport.models) == 30
    store = phase_b.day_store(env.root, COHORT, S0)
    assert store.exists("stage-run.json")
    # Started again the same evening, or woken the next morning before 09:00: nothing is sent twice.
    for start in (datetime(2026, 9, 24, 20, 0, tzinfo=JST), datetime(2026, 9, 25, 8, 30, tzinfo=JST)):
        again = _scheduled(env, start, transport=transport)
        assert (again["status"], again["exit_code"]) == ("already_sent", 0)
    assert len(transport.models) == 30


def test_a_late_start_inside_the_window_runs_and_outside_it_sends_nothing(tmp_path, monkeypatch):
    inside = _setup(tmp_path / "inside", monkeypatch)
    woke = _scheduled(inside, datetime(2026, 9, 25, 2, 0, tzinfo=JST))  # the PC slept through 16:10
    assert (woke["status"], woke["s0"], woke["exit_code"]) == ("sent", "2026-09-24", 0)

    outside = _setup(tmp_path / "outside", monkeypatch)
    yahoo, transport = _FakeYahoo(outside.charts), _PinnedTransport()
    late = _scheduled(outside, datetime(2026, 9, 25, 9, 5, tzinfo=JST), yahoo=yahoo, transport=transport)
    assert (late["status"], late["exit_code"]) == ("no_window", 0)
    assert late["schedule"]["next_s0"] == "2026-09-25" and yahoo.calls == 0 and transport.models == []


def test_a_second_instance_sends_nothing_while_the_first_holds_the_lock(tmp_path, monkeypatch):
    from surge.evaluation.joblock import JobLock, JobLockHeld

    env = _setup(tmp_path, monkeypatch)
    yahoo, transport = _FakeYahoo(env.charts), _PinnedTransport()
    with JobLock(phase_b.job_lock_path(env.root, COHORT)):
        record = _scheduled(env, EVENING, yahoo=yahoo, transport=transport)
        assert (record["status"], record["exit_code"]) == ("locked", 0)
        with pytest.raises(jev_eval.EvaluationError, match="held by another process"):
            jev_eval.phase_b_day(env.root, COHORT, S0, send=True, clock=lambda: EVENING, yahoo=yahoo,
                                 fetch_issues=lambda: (env.issues, "f" * 64), transport=transport)
        with pytest.raises(JobLockHeld):
            JobLock(phase_b.job_lock_path(env.root, COHORT)).acquire()
    assert yahoo.calls == 0 and transport.models == []
    # Released, the next start goes ahead.
    assert _scheduled(env, EVENING, transport=transport)["status"] == "sent"


def test_a_guard_stops_the_day_and_the_job_does_not_try_it_again(tmp_path, monkeypatch):
    env = _setup(tmp_path, monkeypatch)
    flaky = _FlakyYahoo(env.charts, {"3001.T", "3002.T", "3003.T"})
    stopped = _scheduled(env, EVENING, yahoo=flaky)
    assert (stopped["status"], stopped["exit_code"]) == ("stopped", 2) and "below 95%" in stopped["detail"]
    calls = flaky.calls
    again = _scheduled(env, datetime(2026, 9, 24, 18, 0, tzinfo=JST), yahoo=flaky)
    assert (again["status"], again["exit_code"]) == ("day_stopped_earlier", 2) and flaky.calls == calls
    # A manual run of the day leaves it alone too.
    manual = jev_eval.phase_b_day(env.root, COHORT, S0, send=True, clock=lambda: EVENING, yahoo=flaky,
                                  fetch_issues=lambda: (env.issues, "f" * 64))
    assert "below 95%" in manual["stopped"]["reason"] and flaky.calls == calls


def test_an_expired_credit_stops_the_scheduled_day_until_a_new_balance_is_recorded(tmp_path, monkeypatch):
    env = _setup(tmp_path, monkeypatch)
    record_credit_snapshot(env.root, balance_usd=Decimal("4.99"), confirmed_at=datetime(2026, 9, 20, tzinfo=UTC),
                           expires_at=datetime(2026, 9, 24, 6, 0, tzinfo=UTC), displayed_expiry="test",
                           source="test", now=datetime(2026, 9, 24, 5, 0, tzinfo=UTC))
    transport = _PinnedTransport()
    record = _scheduled(env, EVENING, transport=transport)
    assert (record["status"], record["exit_code"]) == ("stopped", 2) and transport.models == []
    assert any("confirm the new console balance once" in p for p in record["detail"]["budget_problems"])
    assert _scheduled(env, datetime(2026, 9, 24, 19, 0, tzinfo=JST), transport=transport)["status"] == \
        "day_stopped_earlier"


def test_a_changed_version_stops_the_cohort_for_every_later_scheduled_day(tmp_path, monkeypatch):
    env = _setup(tmp_path, monkeypatch)
    transport = _PinnedTransport(served=lambda n: "jev-1.13.0" if n < 4 else "jev-1.14.0")
    record = _scheduled(env, EVENING, transport=transport)
    assert (record["status"], record["exit_code"]) == ("stopped", 2) and len(transport.models) == 4
    later = _scheduled(env, datetime(2026, 9, 25, 16, 20, tzinfo=JST), transport=transport)
    assert (later["status"], later["exit_code"]) == ("cohort_refused", 2) and "stopped" in later["detail"]
    assert len(transport.models) == 4


def test_an_interrupted_run_is_not_resumed(tmp_path, monkeypatch):
    env = _setup(tmp_path, monkeypatch)
    pinned = _PinnedTransport()

    def dies_on_the_third(url, data, headers, timeout):
        if len(pinned.models) == 2:
            raise RuntimeError("the process was killed here")
        return pinned(url, data, headers, timeout)

    crashed = _scheduled(env, EVENING, transport=dies_on_the_third)
    assert (crashed["status"], crashed["exit_code"]) == ("error", 1) and "killed" in crashed["detail"]
    store = phase_b.day_store(env.root, COHORT, S0)
    assert store.exists("run-started-1.json") and not store.exists("stage-run.json")
    resumed = _scheduled(env, datetime(2026, 9, 24, 17, 30, tzinfo=JST), transport=pinned)
    assert (resumed["status"], resumed["exit_code"]) == ("stopped", 2) and "interrupted" in resumed["detail"]
    assert len(pinned.models) == 2 and store.exists("run-stopped-1.json")


def test_without_the_typesafe_key_the_day_is_not_ready_and_nothing_starts(tmp_path, monkeypatch):
    env = _setup(tmp_path, monkeypatch)
    store = _planned_and_built(env)
    monkeypatch.delenv("TYPESAFE_API_KEY")
    checked = jev_eval.preflight(store, budget=DAY_BUDGET, clock=lambda: EVENING)
    assert not checked["ready_to_send"] and any("TYPESAFE_API_KEY" in p for p in checked["window_problems"])
    assert not store.exists("run-started-1.json")


def test_the_scheduled_job_runs_only_from_the_worktree_it_was_registered_for(tmp_path, monkeypatch):
    env = _setup(tmp_path, monkeypatch, n=8)
    elsewhere = _scheduled(env, EVENING, expect_repo=tmp_path)
    assert (elsewhere["status"], elsewhere["exit_code"]) == ("not_the_frozen_worktree", 2)
    other_commit = _scheduled(env, EVENING, expect_repo=REPO_ROOT, expect_commit="0" * 40)
    assert (other_commit["status"], other_commit["exit_code"]) == ("not_the_frozen_worktree", 2)
    assert jev_eval._frozen_worktree_problem(REPO_ROOT, None) is None


def test_the_scheduled_job_ends_quietly_once_the_cohort_has_its_25_days(tmp_path, monkeypatch):
    env = _setup(tmp_path, monkeypatch, n=8)
    for day in business_days(date(2026, 9, 24), date(2026, 12, 31))[:25]:
        phase_b.day_store(env.root, COHORT, day).write_json("stage-run.json", {"stage": "run"})
    record = _scheduled(env, datetime(2026, 11, 2, 16, 20, tzinfo=JST))
    assert (record["status"], record["exit_code"]) == ("cohort_complete", 0)


# ----------------------------------------------------------------- the outcome job and the reports (D-278)


def _outcome_run(env, when: datetime, *, yahoo=None):
    return jev_eval.phase_b_outcome_scheduled(env.root, COHORT, clock=lambda: when, sleep=lambda _s: None,
                                              yahoo=yahoo or _FakeYahoo(env.charts))


def _at(month: int, day: int, hour: int = 18, minute: int = 0) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=JST)


def _without_bar(result: dict, day: date) -> dict:
    """A Yahoo chart with one session's bar missing."""

    changed = copy.deepcopy(result)
    i = changed["timestamp"].index(stamp(day))
    del changed["timestamp"][i]
    for values in changed["indicators"]["quote"][0].values():
        del values[i]
    return changed


def _refuse_jev(monkeypatch):
    from surge.evaluation import providers

    def never(*_args, **_kwargs):
        raise AssertionError("the outcome job called Jev")

    monkeypatch.setattr(providers.TypeSafeDirect, "call", never)
    monkeypatch.setattr(providers, "urllib_transport", never)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)


def test_the_outcome_job_freezes_a_day_once_when_its_t_plus_20_has_closed(tmp_path, monkeypatch):
    env = _setup(tmp_path, monkeypatch, until=date(2026, 11, 30), holidays=True)
    store = _send_day(env)
    _refuse_jev(monkeypatch)  # from here on nothing may reach Jev, and there is no key to reach it with
    cohort_dir = env.root / "evaluation" / "jev" / COHORT

    # Nothing due - the evening of S0, the day before T+20, T+20 before 16:10: no-ops that write nothing.
    for when in (_at(9, 24), _at(10, 22), _at(10, 23, 12)):
        record = _outcome_run(env, when)
        assert (record["status"], record["exit_code"], record["due"]) == ("no_targets", 0, [])
    assert not (store.path / "outcome-attempts").exists() and not list(cohort_dir.glob("report-*"))

    frozen = _outcome_run(env, _at(10, 23))
    assert (frozen["status"], frozen["exit_code"]) == ("outcomes_frozen", 0)
    assert frozen["outcomes"]["2026-09-24"]["status"] == "frozen" and frozen["outcomes"]["2026-09-24"]["attempt"] == 1
    assert frozen["report"]["file"] == "report-1.json" and frozen["report"]["status"] == "partial"
    stage = store.read_json("stage-outcomes.json")
    first = store.read_jsonl(stage["outcomes_file"])

    # The same day again, and again later: no second outcome, no second report.
    for when in (_at(10, 23, 18, 5), _at(10, 26)):
        again = _outcome_run(env, when)
        assert (again["status"], again["exit_code"], again["due"], again["report"]) == ("no_targets", 0, [], None)
    assert sorted(p.name for p in (store.path / "outcome-attempts").iterdir()) == ["1"]
    assert sorted(p.name for p in cohort_dir.glob("report-*.json")) == ["report-1.json"]
    assert store.read_jsonl(stage["outcomes_file"]) == first

    # Write-once: a frozen outcome is never frozen or written again.
    with pytest.raises(StoreError, match="already frozen"):
        jev_eval.freeze_outcomes(store, clock=lambda: _at(10, 27), yahoo=_FakeYahoo(env.charts),
                                 sleep=lambda _s: None)
    with pytest.raises(StoreError, match="write-once"):
        store.write_jsonl(stage["outcomes_file"], [])
    assert [r["job"] for r in _scheduler_records(env)][-1] == "outcome"


def test_reports_are_partial_until_every_outcome_is_frozen_and_final_only_then(tmp_path, monkeypatch):
    monkeypatch.setattr(phase_b, "TARGET_BUSINESS_DAYS", 2)  # a two-day cohort, to reach the end quickly
    env = _setup(tmp_path, monkeypatch, until=date(2026, 11, 30), holidays=True)
    first, second = _send_day(env, S0), _send_day(env, date(2026, 9, 25))
    # One Primary security has no bar on 2026-10-05, inside the first day's window: its outcome is unresolvable.
    outcome_charts = {**env.charts, "3000.T": _without_bar(env.charts["3000.T"], date(2026, 10, 5))}
    missing = next(s["sample_id"] for s in first.read_jsonl("population.jsonl")
                   if s["code"] == "3000" and s["cohort"] == "PRIMARY")
    _refuse_jev(monkeypatch)
    cohort_dir = env.root / "evaluation" / "jev" / COHORT
    main = {day: len([r for r in store.read_jsonl("requests.jsonl") if r["variant"] == "main"])
            for day, store in (("first", first), ("second", second))}

    partial = _outcome_run(env, _at(10, 23), yahoo=_FakeYahoo(outcome_charts))
    assert partial["status"] == "outcomes_frozen" and partial["report"]["status"] == "partial"
    report_ = json.loads((cohort_dir / "report-1.json").read_text(encoding="utf-8"))
    progress = report_["progress"]
    assert report_["status"] == progress["status"] == "partial"
    assert progress["predictions"]["sent"]["total"] == main["first"] + main["second"]
    assert progress["predictions"]["resolved"]["total"] == main["first"] - 1
    assert progress["predictions"]["unresolved"]["missing_data"] == {"PRIMARY": 1, "CONTROL": 0, "total": 1}
    assert progress["predictions"]["unresolved"]["awaiting_t_plus_20"]["total"] == main["second"]
    assert progress["predictions"]["unresolved"]["total"]["total"] == main["second"] + 1
    assert (progress["collection_rate"], progress["cohort_completion_rate"]) == (1.0, 0.5)
    assert progress["days"]["awaiting_t_plus_20"] == [{"s0": "2026-09-25", "t_plus_20_by_calendar": "2026-10-26"}]
    # Unresolved predictions are neither failures nor in a denominator: Primary's n is its resolved ones only.
    primary_resolved = progress["predictions"]["resolved"]["PRIMARY"]
    assert report_["primary"]["n"] == primary_resolved == report_["pipeline"]["outcome_join"]["by_cohort"]["PRIMARY"]
    assert report_["pipeline"]["outcome_join"]["outcome_not_resolved"] == [missing]
    text = (cohort_dir / "report-1.md").read_text(encoding="utf-8")
    assert "**status = partial**" in text and "unresolved predictions: " in text and "cohort completion: 50.0%" in text
    assert not (cohort_dir / "report-final.json").exists()

    # The missing 2026-10-05 bar is inside the second day's window too, where 3000 is Primary again.
    final = _outcome_run(env, _at(10, 26), yahoo=_FakeYahoo(outcome_charts))
    assert final["status"] == "outcomes_frozen" and final["report"] == {
        "file": "report-final.json", "status": "final", "resolved": main["first"] + main["second"] - 2,
        "unresolved": 2, "cohort_completion_rate": 1.0}
    closing = json.loads((cohort_dir / "report-final.json").read_text(encoding="utf-8"))
    assert closing["status"] == "final" and closing["progress"]["predictions"]["unresolved"]["awaiting_t_plus_20"][
        "total"] == 0
    assert closing["progress"]["predictions"]["unresolved"]["missing_data"] == {"PRIMARY": 2, "CONTROL": 0, "total": 2}
    assert closing["primary"]["n"] == closing["progress"]["predictions"]["resolved"]["PRIMARY"]
    assert "**status = final**" in (cohort_dir / "report-final.md").read_text(encoding="utf-8")
    after = _outcome_run(env, _at(10, 27), yahoo=_FakeYahoo(outcome_charts))
    assert (after["status"], after["exit_code"]) == ("cohort_final", 0)
    assert sorted(p.name for p in cohort_dir.glob("report-*.json")) == ["report-1.json", "report-final.json"]
    with pytest.raises(StoreError, match="final report"):
        jev_eval.phase_b_report(env.root, COHORT, clock=lambda: _at(10, 27))


def test_the_prediction_and_outcome_jobs_never_run_at_once(tmp_path, monkeypatch):
    env = _setup(tmp_path, monkeypatch, until=date(2026, 11, 30), holidays=True)
    _send_day(env, S0)
    entered, release, results = threading.Event(), threading.Event(), {}
    pinned = _PinnedTransport()

    def holds_the_first_answer(url, data, headers, timeout):
        entered.set()
        assert release.wait(60)
        return pinned(url, data, headers, timeout)

    def predict():
        results["prediction"] = _scheduled(env, _evening(date(2026, 9, 25)), transport=holds_the_first_answer)

    worker = threading.Thread(target=predict)
    worker.start()
    try:
        assert entered.wait(120)  # the prediction job is sending, under the cohort's lock
        during = _outcome_run(env, _at(10, 23))
        assert (during["status"], during["exit_code"]) == ("locked", 0)
        assert "held by another process" in during["detail"]
        assert not phase_b.day_store(env.root, COHORT, S0).exists("stage-outcomes.json")
    finally:
        release.set()
        worker.join(120)
    assert results["prediction"]["status"] == "sent"
    assert _outcome_run(env, _at(10, 23))["status"] == "outcomes_frozen"


def test_the_outcome_job_is_a_no_op_on_closed_days_and_stops_on_missing_prices_or_bad_artifacts(tmp_path,
                                                                                               monkeypatch):
    from surge.evaluation.joblock import JobLock

    env = _setup(tmp_path, monkeypatch, until=date(2026, 11, 30), holidays=True)
    store = _send_day(env)
    closed = _outcome_run(env, _at(10, 12))
    assert (closed["status"], closed["exit_code"], closed["closed"]) == ("closed_day", 0, "スポーツの日")
    with JobLock(phase_b.job_lock_path(env.root, COHORT)):
        assert _outcome_run(env, _at(10, 23))["status"] == "locked"

    # Prices that cannot all be read: nothing frozen, and tried again on the next business day.
    short = _outcome_run(env, _at(10, 23), yahoo=_FlakyYahoo(env.charts, {"3000.T"}))
    assert (short["status"], short["exit_code"]) == ("stopped", 2)
    assert short["outcomes"]["2026-09-24"]["kind"] == "coverage"
    assert not store.exists("stage-outcomes.json") and not (store.path / "outcome-attempts").exists()
    assert _outcome_run(env, _at(10, 26))["status"] == "outcomes_frozen"

    # A frozen outcome changed on disk: the job stops, and keeps stopping, until it is put right.
    frozen_file = store.path / store.read_json("stage-outcomes.json")["outcomes_file"]
    frozen_file.write_text(frozen_file.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    broken = _outcome_run(env, _at(10, 27))
    assert (broken["status"], broken["exit_code"]) == ("stopped", 2) and "2026-09-24" in broken["integrity"]

    # A changed frozen protocol: refused.
    monkeypatch.setattr(phase_b, "question_schema_hash", lambda: "0" * 64)
    refused = _outcome_run(env, _at(10, 28))
    assert (refused["status"], refused["exit_code"]) == ("cohort_refused", 2)
    wrong = jev_eval.phase_b_outcome_scheduled(env.root, COHORT, expect_repo=tmp_path, clock=lambda: _at(10, 28))
    assert (wrong["status"], wrong["exit_code"]) == ("not_the_frozen_worktree", 2)

