"""Jev evaluation Phase B (D-272 as decided 2026-09-19): the daily sample, the cohort's guards, offline.

Synthetic data only: the JPX list, the prices, the disclosures and TypeSafe's
answers are made up here. No network, no model request, no database.
"""

from __future__ import annotations

import ast
import hashlib
import inspect
import json
import random
import socket
import subprocess
import sys
from collections import Counter
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from surge.evaluation import phase_b
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


def _universe(n: int, *, until: date = S0):
    codes = [str(3000 + i) for i in range(n)]
    days = business_days(date(2025, 8, 1), until)
    charts = {f"{c}.T": chart(days, walk(len(days), start=300.0 + (i * 37) % 2000, seed=i),
                              volumes=[50_000 + 1000 * (i % 17)] * len(days)) for i, c in enumerate(codes)}
    issues = [ListedIssue(c, f"銘柄{c}株式会社", "PRIME", SECTORS[i % len(SECTORS)], "TOPIX Small 1")
              for i, c in enumerate(codes)]
    return codes, charts, issues


def _setup(tmp_path, monkeypatch, *, n=40, seed=SEED):
    codes, charts, issues = _universe(n)
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


def test_outcomes_after_t_plus_20_and_the_report_by_route_d_subgroup(tmp_path, monkeypatch):
    env = _setup(tmp_path, monkeypatch)
    store = _planned_and_built(env)
    pacer, clock = _paced(EVENING)
    jev_eval.preflight(store, budget=DAY_BUDGET, clock=clock)
    assert jev_eval.run(store, budget=DAY_BUDGET, pacer=pacer, transport=_PinnedTransport(), clock=clock)[
        "stopped"] is None

    # Read again after T+20: the same prices up to S0, and the sessions after it.
    _, later_charts, _ = _universe(len(env.codes), until=date(2026, 10, 30))
    t20 = business_days(date(2026, 9, 25), date(2026, 10, 30))[19]
    with pytest.raises(jev_eval.EvaluationError, match="has not closed"):
        _, short, _ = _universe(len(env.codes), until=date(2026, 10, 15))
        jev_eval.freeze_outcomes(store, clock=lambda: datetime(2026, 10, 15, 17, 0, tzinfo=JST),
                                 yahoo=_FakeYahoo(short), sleep=lambda _s: None)
    with pytest.raises(jev_eval.EvaluationError, match="final from"):
        jev_eval.freeze_outcomes(store, clock=lambda: datetime(t20.year, t20.month, t20.day, 12, 0, tzinfo=JST),
                                 yahoo=_FakeYahoo(later_charts), sleep=lambda _s: None)
    frozen = jev_eval.freeze_outcomes(store, clock=lambda: datetime(t20.year, t20.month, t20.day, 17, 0, tzinfo=JST),
                                      yahoo=_FakeYahoo(later_charts), sleep=lambda _s: None)
    assert frozen["t_plus_20"] == t20.isoformat() and frozen["answers_read"] is False
    outcomes = store.read_jsonl("outcomes.jsonl")
    assert len(outcomes) == len(store.read_jsonl("population.jsonl"))
    assert all(o["teacher_admissible"] is False and o["window_first"] == "2026-09-25" for o in outcomes)

    result = jev_eval.phase_b_report(env.root, COHORT, clock=clock)
    assert result["phase"] == "B" and "not connected to production" in result["banner"]
    assert result["cohort"]["days_included"] == ["2026-09-24"]
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
    status = jev_eval.phase_b_status(env.root, COHORT, clock=clock)
    assert status["days_sent"] == 1 and status["frozen_protocol"] == "holds"
    assert status["answered"]["PRIMARY"] + status["answered"]["CONTROL"] == len(store.read_jsonl("population.jsonl"))


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
