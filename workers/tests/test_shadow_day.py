"""Stage 3 of the web shadow (D-279): a Phase B day in steps is the PC's day, byte for byte.

The PC's day is the frozen ``plan_day`` and ``build``, run here on made-up
securities with fakes for Yahoo, JPX and Yanoshin (test_evaluation_phase_b),
under the official cohort's id and seed. The shadow's is ``surge.shadow.day`` on
the same fakes, a few securities to a step; every file both of them write is
compared. The same day through the Workflow SDK is in test_shadow_service.
"""

from __future__ import annotations

import copy
import gzip
import json
import time
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest

import test_evaluation_phase_b as pb
from surge.evaluation import phase_b
from surge.providers.yahoo_finance import JST
from surge.shadow import compare, day
from surge.shadow.artifacts import jsonl_bytes
from surge.shadow.export import _file_rows, _parquet_rows
from surge.storage.local import LocalObjectStore
from test_evaluation import _FakeYahoo

S0, EVENING = pb.S0, pb.EVENING


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """The PC's cohort under the official id and seed, on 40 made-up securities."""

    monkeypatch.setattr(pb, "COHORT", day.OFFICIAL.cohort_id)
    return pb._setup(tmp_path, monkeypatch, seed=day.OFFICIAL.experiment_seed)


def _quiet(_seconds):
    return None


def read_in_steps(env, *, per_step=7, yahoo=None):
    universe = day.read_universe(lambda: (env.issues, "f" * 64))
    window = day.day_window(S0, EVENING)
    now, start = datetime.fromisoformat(window["now"]), date.fromisoformat(window["start"])
    queue, chunks = [i["code"] for i in universe["issues"]], []
    while queue:
        part = day.read_chunk(queue[:per_step], s0=S0, start=start, now=now, yahoo=yahoo or _FakeYahoo(env.charts),
                              sleep=_quiet, monotonic=time.monotonic)
        chunks.append(part)
        queue = part["remaining"] + queue[per_step:]
    return universe, chunks, now, start


def shadow_day(env, *, reread_yahoo=None):
    universe, chunks, now, start = read_in_steps(env)
    planned = day.plan(universe, chunks, s0=S0, now=now, start=start)
    reread = day.reread_selected(planned["selected"], s0=S0, start=start, now=now,
                                 yahoo=reread_yahoo or _FakeYahoo(env.charts), sleep=_quiet, monotonic=time.monotonic)
    return universe, chunks, planned, reread


def test_the_day_in_steps_writes_the_pc_s_plan_and_builds_the_pc_s_requests(env):
    store = pb._planned_and_built(env)  # the PC: plan_day, then build, in one process
    _universe, chunks, planned, reread = shadow_day(env)
    assert len(chunks) == 6  # 40 securities, 7 to a step
    assert not reread["problems"]
    built = day.build(planned, reread, s0=S0, yanoshin=pb._FakeYanoshinRecent())
    selected = planned["summary"]["selected"]
    assert selected["primary"] > 0 and selected["control"] > 0
    assert len(built["requests"].splitlines()) == planned["summary"]["requests"] == built["summary"]["requests"]

    pc, files = store.path, planned["files"]
    assert files["manifest.json"] == (pc / "manifest.json").read_bytes()
    assert files["sessions.json"] == (pc / "sessions.json").read_bytes()
    assert files["population.jsonl"] == (pc / "population.jsonl").read_bytes()
    assert gzip.decompress(files["inputs/universe.json.gz"]) == (pc / "inputs" / "universe.json").read_bytes()
    assert gzip.decompress(files["candidates.jsonl.gz"]) == (pc / "candidates.jsonl").read_bytes()
    assert gzip.decompress(files["screening.jsonl.gz"]) == jsonl_bytes(_parquet_rows(pc / "screening.parquet"))
    # requests.jsonl carries every request body's SHA-256: the same file is the same bodies.
    assert built["requests"] == (pc / "requests.jsonl").read_text(encoding="utf-8")
    assert json.loads(built["tdnet"]) == _file_rows(store, "inputs/tdnet")
    pc_plan, pc_build = store.read_json("stage-plan.json"), store.read_json("stage-build.json")
    assert {k: v for k, v in built["stages"]["build"].items() if k != "finished_at"} == \
           {k: v for k, v in pc_build.items() if k != "finished_at"}
    assert planned["summary"] == {k: v for k, v in pc_plan.items() if k not in ("stage", "finished_at", "files_sha256")}
    assert json.loads(built["cohort"])["frozen_fingerprint"] == day.OFFICIAL.frozen_fingerprint


class _ReorderedYahoo(_FakeYahoo):
    """Yahoo as it answers a second time: the same bars, keys in another order, the adjusted closes recomputed."""

    def chart(self, symbol, params):
        result = copy.deepcopy(super().chart(symbol, params)["chart"]["result"][0])
        result["indicators"]["adjclose"] = [{"adjclose": [c * 0.999 for c in result["indicators"]["quote"][0]["close"]]}]
        return {"chart": {"result": [dict(reversed(list(result.items())))]}}


class _ChangedYahoo(_FakeYahoo):
    """Yahoo, except that one security's last bar is traded differently on the second read."""

    def __init__(self, charts, symbol):
        changed = copy.deepcopy(charts[symbol])
        changed["indicators"]["quote"][0]["volume"][-1] += 100
        super().__init__({**charts, symbol: changed})


def test_a_selected_security_is_read_again_and_must_have_the_history_that_was_screened(env):
    _universe, _chunks, planned, reread = shadow_day(env, reread_yahoo=None)
    assert all(c["same_line"] and c["same_history"] for c in reread["checked"].values())
    assert set(reread["lines"]) == set(planned["selected"]) == {json.loads(line)["code"] for line in
                                                                 reread["lines"].values()}

    # Another answer's bytes, the same history: the day goes on.
    _u, _c, _p, again = shadow_day(env, reread_yahoo=_ReorderedYahoo(env.charts))
    assert not again["problems"]
    assert all(not c["same_line"] and c["same_history"] for c in again["checked"].values())

    # A history that changed: the day stops.
    code = sorted(planned["selected"])[0]
    _u, _c, _p, changed = shadow_day(env, reread_yahoo=_ChangedYahoo(env.charts, f"{code}.T"))
    assert changed["problems"] == [f"{code}: the history read again is not the one screened"]
    assert changed["checked"][code]["same_history"] is False


def test_a_step_hands_on_what_it_could_not_read_before_its_deadline(env):
    universe = day.read_universe(lambda: (env.issues, "f" * 64))
    codes = [i["code"] for i in universe["issues"]][:10]
    clock = iter(range(0, 100_000, 100))  # every look at the clock is 100 s later
    part = day.read_chunk(codes, s0=S0, start=S0 - timedelta(days=400), now=EVENING, yahoo=_FakeYahoo(env.charts),
                          sleep=_quiet, monotonic=lambda: next(clock), deadline_seconds=180)
    assert part["codes"] == codes[:1] and part["remaining"] == codes[1:]  # never less than one security a step
    assert part["measure"]["attempted"] == 1


def test_the_steps_must_have_read_the_universe_once_and_in_order(env):
    universe, chunks, now, start = read_in_steps(env)
    with pytest.raises(day.DayRefused, match="exactly once"):
        day.plan(universe, list(reversed(chunks)), s0=S0, now=now, start=start)
    with pytest.raises(day.DayRefused, match="exactly once"):
        day.plan(universe, chunks[:-1], s0=S0, now=now, start=start)


def test_a_day_whose_universe_was_not_read_almost_whole_is_refused_as_the_pc_refuses_it(env):
    flaky = pb._FlakyYahoo(env.charts, {"3001.T", "3002.T", "3003.T"})  # 37 of 40: 92.5%
    universe, chunks, now, start = read_in_steps(env, yahoo=flaky)
    with pytest.raises(day.DayRefused, match="below 95%") as refused:
        day.plan(universe, chunks, s0=S0, now=now, start=start)
    assert refused.value.kind == "coverage"


def test_a_probe_counts_what_the_plan_draws_from(env):
    universe, chunks, now, start = read_in_steps(env)
    planned = day.plan(universe, chunks, s0=S0, now=now, start=start)
    probe = day.probe_summary(universe, chunks, s0=S0)
    for key in ("eligible", "passing", "not_passing", "route_membership_among_passing",
                "route_d_subgroups_among_passing"):
        assert probe[key] == planned["summary"][key], key
    assert probe["issues"] == probe["histories_read"] == 40 and probe["s0_is_session"]


def _at(day_: date, hour: int, minute: int = 0) -> datetime:
    return datetime(day_.year, day_.month, day_.day, hour, minute, tzinfo=JST)


def test_a_day_runs_in_the_pc_s_window_and_a_probe_only_on_a_past_business_day():
    assert day.day_window(S0, _at(S0, 17))["now"] == _at(S0, 17).isoformat()
    early = day.day_window(S0, _at(S0, 16, 0))
    assert early["wait_until"] == phase_b.close_confirmed_at(S0).isoformat() and early["wait_seconds"] == 601.0
    for when, kind in ((_at(S0, 15, 0), "not_yet"), (_at(date(2026, 9, 25), 9, 0), "window_closed")):
        with pytest.raises(day.DayRefused) as refused:
            day.day_window(S0, when)
        assert refused.value.kind == kind
    for s0, kind in ((date(2026, 9, 18), "before_start"), (date(2026, 10, 12), "closed_day")):
        with pytest.raises(day.DayRefused) as refused:
            day.day_window(s0, _at(s0, 20))
        assert refused.value.kind == kind
    probe = day.day_window(date(2026, 9, 18), _at(date(2026, 9, 19), 20), probe=True)
    assert probe["start"] == (date(2026, 9, 18) - timedelta(days=phase_b.HISTORY_LOOKBACK_DAYS)).isoformat()
    with pytest.raises(day.DayRefused) as refused:
        day.day_window(date(2026, 9, 21), _at(date(2026, 9, 21), 20), probe=True)
    assert refused.value.kind == "closed_day"


def test_the_shadow_reproduces_the_official_cohort_s_protocol():
    assert day.check_binding() == day.OFFICIAL.frozen_fingerprint
    with pytest.raises(day.DayRefused, match="a changed protocol is a new cohort"):
        day.check_binding(day.CohortBinding("x-cohort", "seed", day.OFFICIAL.evaluation_version, "0" * 64))


# ----------------------------------------------------------------- the comparison with the PC's day


def shadow_day_in_store(env, target, *, yahoo=None, yanoshin=None):
    """The whole shadow day, written as artifacts to ``target``: what the Workflow does, step by step."""

    universe, chunks, now, start = read_in_steps(env, yahoo=yahoo)
    planned = day.plan(universe, chunks, s0=S0, now=now, start=start)
    reread = day.reread_selected(planned["selected"], s0=S0, start=start, now=now,
                                 yahoo=yahoo or _FakeYahoo(env.charts), sleep=_quiet, monotonic=time.monotonic)
    built = day.build(planned, reread, s0=S0, yanoshin=yanoshin or pb._FakeYanoshinRecent())
    return day.write_day(target, s0=S0, planned=planned, reread=reread, built=built,
                         run={"mode": "day", "trigger": "test", "steps": {}})


def _compare(env, target):
    return compare.compare(compare.pc_day(env.root, day.OFFICIAL.cohort_id, S0),
                           compare.cloud_day(target, day.OFFICIAL.cohort_id, S0), s0=S0)


def _statuses(report) -> dict:
    return {name: item["status"] for name, item in report["items"].items()}


def test_the_same_day_is_an_exact_match_on_every_item_the_user_named(env, tmp_path):
    pb._planned_and_built(env)
    target = LocalObjectStore(tmp_path / "cloud", store_id="test")
    assert shadow_day_in_store(env, target)["written"]
    report = _compare(env, target)
    assert set(_statuses(report).values()) == {compare.EXACT}, compare.render(report)
    assert set(report["items"]) == set(compare.ITEMS)
    assert report["items"]["primary"]["pc"] > 0 and report["items"]["control"]["pc"] > 0
    assert report["items"]["selected_history_digests"]["securities"] > 0
    assert report["cloud_status"] == "built" and report["pc_status"] == "built"
    assert report["stage4"]["may_be_proposed"]


class _LaterTitle(pb._FakeYanoshinRecent):
    """The index read later: one security has a title published on S0's evening, after the PC read it."""

    def __init__(self, code, *, published=datetime(2026, 9, 24, 18, 30, tzinfo=JST)):
        self.late, self.published = code, published

    def fetch_for_codes(self, codes):
        result = super().fetch_for_codes(codes)
        if codes == [self.late]:
            result.items.append(SimpleNamespace(yanoshin_id=7, pubdate=self.published,
                                                title="業績予想の修正に関するお知らせ",
                                                code=SimpleNamespace(normalised=self.late)))
        return result


def test_a_title_published_after_the_pc_read_the_index_is_an_expected_timing_difference(env, tmp_path):
    store = pb._planned_and_built(env)  # the PC read the index at 17:00 JST
    late = store.read_jsonl("population.jsonl")[0]["code"]
    target = LocalObjectStore(tmp_path / "cloud", store_id="test")
    shadow_day_in_store(env, target, yanoshin=_LaterTitle(late))  # a title published at 18:30 JST
    report = _compare(env, target)
    statuses = _statuses(report)
    assert statuses["disclosure_titles"] == statuses["request_hashes_independent_input"] == compare.TIMING
    assert statuses["request_hashes_same_input"] == compare.EXACT  # the same input builds the same bytes
    titles = report["items"]["disclosure_titles"]
    assert [e["code"] for e in titles["examples"]] == [late]
    assert titles["examples"][0]["titles_only_cloud"][0]["title"] == "業績予想の修正に関するお知らせ"
    requests = report["items"]["request_hashes_independent_input"]
    assert {e["code"] for e in requests["examples"]} == {late} and requests["by_status"][compare.UNEXPLAINED] == 0
    for name in ("universe", "population_at_or_below_3000_yen", "screening_pass", "route_memberships", "primary",
                 "control", "selected_history_digests"):
        assert statuses[name] == compare.EXACT, name
    assert report["stage4"]["may_be_proposed"]  # a timing difference does not block


def test_a_title_the_cloud_saw_but_published_before_the_pc_read_is_unexplained(env, tmp_path):
    store = pb._planned_and_built(env)
    late = store.read_jsonl("population.jsonl")[0]["code"]
    target = LocalObjectStore(tmp_path / "cloud", store_id="test")
    # Published at 16:00 JST, before the PC read the index at 17:00: the PC should have seen it too.
    shadow_day_in_store(env, target, yanoshin=_LaterTitle(late, published=datetime(2026, 9, 24, 16, 0, tzinfo=JST)))
    report = _compare(env, target)
    assert _statuses(report)["disclosure_titles"] == compare.UNEXPLAINED
    assert report["items"]["request_hashes_independent_input"]["examples"][0]["cause"] == "disclosure titles differ"
    assert not report["stage4"]["may_be_proposed"]
    assert report["stage4"]["unexplained_by_group"]["request_build"] == ["request_hashes_independent_input"]


class _RevisedBar(_FakeYahoo):
    """Yahoo with one security's bar five sessions before S0 revised, on every read of the cloud's."""

    def __init__(self, charts, symbol):
        revised = copy.deepcopy(charts[symbol])
        revised["indicators"]["quote"][0]["close"][-6] += 1.0
        super().__init__({**charts, symbol: revised})


def test_a_history_read_differently_is_an_unexplained_mismatch_traced_to_the_prices(env, tmp_path):
    store = pb._planned_and_built(env)
    code = store.read_jsonl("population.jsonl")[0]["code"]
    target = LocalObjectStore(tmp_path / "cloud", store_id="test")
    shadow_day_in_store(env, target, yahoo=_RevisedBar(env.charts, f"{code}.T"))
    report = _compare(env, target)
    statuses = _statuses(report)
    assert statuses["request_hashes_same_input"] == compare.EXACT
    assert statuses["selected_history_digests"] == compare.UNEXPLAINED
    assert report["items"]["selected_history_digests"]["examples"] == [code]
    causes = {e["cause"] for e in report["items"]["request_hashes_independent_input"]["examples"]}
    assert causes == {"price history differs"}
    blocking = report["stage4"]["unexplained_by_group"]
    assert not report["stage4"]["may_be_proposed"]
    assert blocking["price_history_inputs"] == ["selected_history_digests"]
    assert blocking["request_build"] == ["request_hashes_independent_input"]
