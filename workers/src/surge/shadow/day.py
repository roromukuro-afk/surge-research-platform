"""A Phase B day on Vercel, in steps a Function can finish (D-279, Stage 3).

The PC runs a day in one process (``surge.jobs.jev_eval.plan_day``, then
``build``): every domestic common stock read from Yahoo and screened at S0, the
day's sample drawn, the requests built. The read alone takes about half an hour
and a Vercel Function stops at 300 s, so the shadow splits the day - and does
nothing else to it:

- every security is read and screened by the frozen job's own functions
  (``_fetch_one``, ``screen``, ``_screening_row``), in the PC's order and at its
  pace, 150 to a step, and a step that passes its deadline hands the rest on;
- a step keeps what the rest of the day needs and no price: the screening row,
  the route evidence, the dates with a bar (the session rule) and two digests -
  of the chart line as read, and of the as-traded history the screener and the
  state are built from. Prices would fill the Workflow's storage (Hobby: 1 GB
  written a month) with data no later step reads;
- the sample is drawn by the frozen ``select_day``, and the plan's files are
  written with the RunStore's serializers, so the same input gives the PC's bytes;
- the selected securities are read again with the same ``now``. Yahoo does not
  answer twice with the same bytes (key order, recomputed adjusted closes), but
  the as-traded history must be the one screened: a history that changed stops
  the day;
- the requests are built by the frozen ``build`` itself, on a RunStore in a
  temporary directory that holds exactly the plan's files;
- the artifacts are written once, under ``surge/phase-b-shadow``, the integrity
  record last.

Nothing here sends a request to a model: Stage 3 builds the requests and stops.
A probe reads and screens a past business day and writes nothing: it is how the
read is measured on Vercel before a prospective day exists.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import shutil
import statistics
import tempfile
import time
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from surge.evaluation import jpx_calendar, phase_b
from surge.evaluation.method import load_method
from surge.evaluation.population import Screen
from surge.evaluation.selection import ROUTES, SUBGROUPS, SelectionError, route_d_subgroup
from surge.evaluation.universe import ListedIssue
from surge.jobs import jev_eval
from surge.shadow.artifacts import (
    DAY_COMMIT,
    JSON,
    JSONL,
    ArtifactSet,
    cohort_prefix,
    day_prefix,
    exists,
    gzip_bytes,
    json_bytes,
    jsonl_bytes,
    put_run_record,
    read_commit,
)
from surge.shadow.export import REQUEST_BODIES, _file_rows, calendar_record, scrub
from surge.storage.base import ImmutableObjectConflict, ObjectStore, sha256_hex

SHADOW_ROOT = "surge/phase-b-shadow"
SYSTEM = "vercel-shadow"
CHUNK_SIZE = 150
CHUNK_DEADLINE_SECONDS = 180.0
#: How long a day waits for its window when it is started early (the PC's scheduled job: 15 minutes).
MAX_WAIT_SECONDS = jev_eval.MAX_WAIT_FOR_WINDOW_SECONDS
JEV = "not called: Stage 3 builds every request and sends none (D-279)"
#: plan_day's own words for sessions.json.
SESSIONS_RULE = "a date on which at least 30% of the securities read have a bar (D-142: no guessed calendar)"
PRICE_RULE = (
    "lines: the SHA-256 of each security's chart line as read in its step; histories: of the as-traded history "
    "built from it (bars and splits, what the screener and the state read). The selected securities were read "
    "again with the same `now` (reread; their lines are inputs/prices-selected.jsonl.gz): Yahoo does not answer "
    "twice with the same bytes (key order, recomputed adjusted closes), so it is the history that must be the same"
)


@dataclass(frozen=True)
class CohortBinding:
    """The cohort a shadow day reproduces: its selection is keyed by these, its protocol is this fingerprint."""

    cohort_id: str
    experiment_seed: str
    evaluation_version: str
    frozen_fingerprint: str


#: The PC's cohort (D-277; docs/specs/jev-evaluation-design.md), as it was created.
OFFICIAL = CohortBinding(
    cohort_id="jev-phase-b-jp-20260924-v1",
    experiment_seed="surge-phase-b-jp-jev-1.13.0-v1-20260924",
    evaluation_version="jev-eval-1.1.0",
    frozen_fingerprint="73cceb0db1be4e3dcd3a44b5d590bdb31775fa21a712bb473bfea5b725e4bcf6",
)


class DayRefused(RuntimeError):
    """A day that is not run, or not run further: plan_day's refusals, and the shadow's own checks."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind

    def record(self) -> dict:
        return {"kind": self.kind, "message": str(self)[:600]}


def providers() -> dict:
    """The real sources; tests replace this function."""

    from surge.evaluation.universe import fetch_listed_issues
    from surge.news.sources.yanoshin_tdnet import YanoshinTdnetSource
    from surge.providers.yahoo_finance import YahooSession

    return {"issues": fetch_listed_issues, "yahoo": YahooSession, "yanoshin": YanoshinTdnetSource,
            "sleep": time.sleep, "monotonic": time.monotonic, "now": lambda: datetime.now(UTC)}


def history_digest(history) -> str:
    """The as-traded history as the screener and the state read it: every bar's prices and volume, every split."""

    def text(value) -> str | None:
        return None if value is None else str(value)

    bars = [[b.trade_date.isoformat(), *(text(v) for v in (b.open, b.high, b.low, b.close, b.volume, b.turnover))]
            for b in history.bars]
    actions = [[str(a.action_type), a.ex_date.isoformat(), text(a.split_from), text(a.split_to)]
               for a in history.actions]
    payload = json.dumps({"bars": bars, "actions": actions}, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _measure(values: list[float]) -> dict:
    if not values:
        return {"n": 0}
    ordered = sorted(values)
    return {"n": len(ordered), "p50": round(statistics.median(ordered), 3),
            "p95": round(ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))], 3), "max": round(ordered[-1], 3)}


def _max_rss_mb() -> float | None:
    try:
        import resource  # noqa: PLC0415 - not on Windows
    except ImportError:
        return None
    return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)


# ----------------------------------------------------------------- when


def check_binding(binding: CohortBinding = OFFICIAL) -> str:
    """The frozen protocol recomputed from the code that runs here; a shadow of another protocol is refused."""

    if binding.evaluation_version != jev_eval.EVALUATION_VERSION:
        raise DayRefused("protocol", f"the cohort is {binding.evaluation_version}, this code is "
                                     f"{jev_eval.EVALUATION_VERSION}")
    fingerprint = phase_b.fingerprint(phase_b.frozen_protocol(load_method(),
                                                              evaluation_version=jev_eval.EVALUATION_VERSION))
    if fingerprint != binding.frozen_fingerprint:
        raise DayRefused("protocol", f"this code's frozen protocol is {fingerprint}, cohort {binding.cohort_id}'s is "
                                     f"{binding.frozen_fingerprint}: a changed protocol is a new cohort")
    return fingerprint


def day_window(s0: date, now: datetime, *, probe: bool = False) -> dict:
    """plan_day's refusals in its order, or when to start. A probe reads a past day: the prospective rules do not apply."""

    if probe:
        if s0.weekday() >= 5:
            raise DayRefused("weekend", f"S0 {s0.isoformat()} is a weekend day")
    else:
        try:
            jev_eval.check_s0(s0)
        except SelectionError as exc:
            raise DayRefused("before_start", str(exc)) from exc
    try:
        closed = jpx_calendar.closed_reason(s0)
    except jpx_calendar.CalendarUnknown as exc:
        raise DayRefused("calendar", str(exc)) from exc
    if closed:
        raise DayRefused("closed_day", f"S0 {s0} is not a TSE business day ({closed}; {jpx_calendar.CALENDAR_VERSION})")
    opens, closes = phase_b.close_confirmed_at(s0), phase_b.send_deadline(s0)
    window = {"s0": s0.isoformat(), "opens_at": opens.isoformat(), "closes_at": closes.isoformat()}
    if now < opens:
        if probe or (opens - now).total_seconds() > MAX_WAIT_SECONDS:
            raise DayRefused("not_yet", f"S0 {s0}: its bars are final from {opens.isoformat()}, not before")
        return {**window, "wait_until": opens.isoformat()}
    if not probe and now >= closes:
        raise DayRefused("window_closed", f"S0 {s0}: S1 may have opened ({closes.isoformat()}); the day can no "
                                          "longer be predicted prospectively")
    start = s0 - timedelta(days=phase_b.HISTORY_LOOKBACK_DAYS)
    return {**window, "now": now.isoformat(), "start": start.isoformat()}


def scheduled_s0(now: datetime) -> dict:
    """The day the PC's scheduled job would take at ``now``: the open window's, or the next one's."""

    slot = phase_b.schedule(now)
    return {"s0": slot["s0"] or slot["next_s0"], "window_open": slot["window_open"], "opens_at": slot["opens_at"],
            "closes_at": slot["closes_at"]}


# ----------------------------------------------------------------- read


def read_universe(fetch_issues=None) -> dict:
    """JPX's workbook as plan_day reads it: domestic common stock, by code."""

    issues, workbook_sha256 = (fetch_issues or providers()["issues"])()
    issues = sorted(issues, key=lambda i: i.code)
    return {"workbook_sha256": workbook_sha256, "issues": [vars(i) for i in issues]}


def read_chunk(codes: list[str], *, s0: date, start: date, now: datetime, yahoo=None, sleep=None, monotonic=None,
               deadline_seconds: float = CHUNK_DEADLINE_SECONDS) -> dict:
    """plan_day's loop over ``codes`` until the deadline: each security read, paced and screened as the PC does it."""

    source = providers() if (yahoo is None or sleep is None or monotonic is None) else {}
    yahoo = yahoo or source["yahoo"]()
    sleep = sleep or source["sleep"]
    monotonic = monotonic or source["monotonic"]
    began, cpu = monotonic(), time.process_time()
    rows, evidence, failures, digests, fetch_seconds = [], {}, [], {}, []
    bar_counts: Counter = Counter()
    done = 0
    for code in codes:
        if done and monotonic() - began >= deadline_seconds:
            break
        fetch_began = monotonic()
        chart, history, failure = jev_eval._fetch_one(yahoo, code, start, now, sleep)
        fetch_seconds.append(monotonic() - fetch_began)
        sleep(jev_eval.YAHOO_PAUSE_SECONDS)
        done += 1
        if failure is not None:
            failures.append(failure)
            continue
        bar_counts.update({bar.trade_date for bar in history.bars})
        verdict = jev_eval.screen(history, s0)
        rows.append(jev_eval._screening_row(verdict))
        if verdict.route_evidence:
            evidence[code] = verdict.route_evidence
        digests[code] = {"line": sha256_hex(jev_eval._chart_line(chart)), "history": history_digest(history)}
    # One JSON text, as the RunStore would write its values (Decimal and dates by str): the Workflow carries a string
    # exactly, whatever its own encoding of numbers and keys.
    data = json.dumps({"rows": rows, "evidence": evidence, "failures": failures, "digests": digests,
                       "bar_counts": {d.isoformat(): n for d, n in sorted(bar_counts.items())}},
                      ensure_ascii=False, default=str)
    return {
        "codes": list(codes[:done]),
        "remaining": list(codes[done:]),
        "data": data,
        "measure": {"attempted": done, "read": len(rows), "failed": len(failures),
                    "seconds": round(monotonic() - began, 3), "cpu_seconds": round(time.process_time() - cpu, 3),
                    "fetch_seconds": _measure(fetch_seconds), "max_rss_mb": _max_rss_mb()},
    }


def _chunks(chunks: list[dict]) -> tuple[list[dict], dict, list[dict], dict, Counter]:
    rows, evidence, failures, digests, bar_counts = [], {}, [], {}, Counter()
    for chunk in chunks:
        part = json.loads(chunk["data"])
        rows += part["rows"]
        evidence.update(part["evidence"])
        failures += part["failures"]
        digests.update(part["digests"])
        for day, n in part["bar_counts"].items():
            bar_counts[date.fromisoformat(day)] += n
    return rows, evidence, failures, digests, bar_counts


def _covered(universe: dict, chunks: list[dict]) -> list[ListedIssue]:
    issues = [ListedIssue(**i) for i in universe["issues"]]
    read = [code for chunk in chunks for code in chunk["codes"]]
    if read != [i.code for i in issues]:
        raise DayRefused("chunks", "the steps did not read the universe exactly once and in order")
    return issues


# ----------------------------------------------------------------- the plan


def plan_manifest(*, s0: date, now: datetime, start: date, binding: CohortBinding, workbook_sha256: str,
                  issues: int, read: int, failed: int, coverage: float) -> dict:
    """plan_day's manifest, value for value."""

    return {
        "run_id": f"{binding.cohort_id}/days/{s0.isoformat()}",
        "evaluation_version": jev_eval.EVALUATION_VERSION,
        "phase": "B",
        "cohort_id": binding.cohort_id,
        "cohort_fingerprint": binding.frozen_fingerprint,
        "s0": s0.isoformat(),
        "started_at": now.isoformat(),
        "model": phase_b.PINNED_MODEL,
        "provider": jev_eval.TYPESAFE_DIRECT,
        "fallback_provider": None,
        "model_version_metadata": jev_eval._model_metadata(jev_eval.TYPESAFE_DIRECT, pinned=phase_b.PINNED_MODEL),
        "question_schema_hash": jev_eval.question_schema_hash(),
        "method": load_method().manifest_entry(),
        "input_building_code": jev_eval.code_version(),
        "population_definition": {
            "market": "JP",
            "universe": "JPX listed issues workbook as published on S0's evening, domestic common stock in "
                        "PRIME / STANDARD / GROWTH, every security",
            "workbook_sha256": workbook_sha256,
            "universe_read": {"issues": issues, "histories_read": read, "failed": failed,
                              "coverage": coverage, "minimum_coverage": phase_b.MIN_UNIVERSE_COVERAGE},
            "history_start": start.isoformat(),
            "s0": s0.isoformat(),
            "screener": jev_eval.SCREENER_DEFINITION,
            "eligible": "a bar on S0 and the S0 close as traded <= 3,000 yen",
            "kept": "every screening result (screening.parquet) and every eligible candidate with its route "
                    "membership, key, rank, selection and probability (candidates.*), before the sample is drawn",
            "selection": {"version": jev_eval.SELECTION_VERSION, "evaluation_version": binding.evaluation_version,
                          "experiment_seed": binding.experiment_seed, "per_day": phase_b.per_day()},
            "route_d": "unchanged; D-only candidates drawn like any other; subgroups pre-registered in cohort.json",
            "cohorts_pooled": False,
            "limitations": jev_eval.PHASE_B_LIMITATIONS,
        },
        "outcome_definition": jev_eval.OUTCOME_DEFINITION,
        "storage": {"database_writes": "none", "teacher_admissible": False},
    }


def plan(universe: dict, chunks: list[dict], *, s0: date, now: datetime, start: date,
         binding: CohortBinding = OFFICIAL) -> dict:
    """What plan_day computes once every security is read: coverage, sessions, the sample, the plan's files."""

    issues = _covered(universe, chunks)
    rows, evidence, failures, digests, bar_counts = _chunks(chunks)
    read = len(rows)
    coverage = read / len(issues) if issues else 0.0
    if coverage < phase_b.MIN_UNIVERSE_COVERAGE:
        raise DayRefused("coverage", f"S0 {s0}: {read} of {len(issues)} histories read ({coverage:.1%}), below "
                                     f"{phase_b.MIN_UNIVERSE_COVERAGE:.0%}; the day is not drawn from")
    sessions = jev_eval.sessions_from_counts(bar_counts, read)
    if s0 not in sessions:
        raise DayRefused("not_a_session", f"{s0} is not a session in the data: fewer than 30% of the securities "
                                          "have a bar")
    screens = [
        Screen(code=r["code"], s0=date.fromisoformat(r["s0"]), eligible=r["eligible"], passed=r["passed"],
               close_as_traded=None if r["close_as_traded"] is None else Decimal(r["close_as_traded"]),
               routes=list(r["routes"]), route_evidence=evidence.get(r["code"], {}),
               turnover_avg_20d=r["turnover_avg_20d"], reason=r["reason"])
        for r in rows
    ]
    try:
        selection = jev_eval.select_day(screens, {i.code: i for i in issues}, s0=s0,
                                        evaluation_version=binding.evaluation_version,
                                        experiment_seed=binding.experiment_seed)
    except SelectionError as exc:
        raise DayRefused("selection", str(exc)) from exc
    if not selection.samples:
        raise DayRefused("empty", f"S0 {s0}: no eligible security passed the screener; the day has nothing to ask")
    manifest = plan_manifest(s0=s0, now=now, start=start, binding=binding, workbook_sha256=universe["workbook_sha256"],
                             issues=len(issues), read=read, failed=len(failures), coverage=coverage)
    universe_file = json_bytes({"workbook_sha256": universe["workbook_sha256"], "domestic_common_stock": len(issues),
                                "histories_read": read, "coverage": coverage, "failures": failures,
                                "issues": [vars(i) for i in issues]})
    selected = sorted({s.code for s in selection.samples})
    return {
        "files": {
            "manifest.json": json_bytes(manifest),
            "sessions.json": json_bytes({"rule": SESSIONS_RULE, "sessions": [d.isoformat() for d in sessions]}),
            "population.jsonl": jsonl_bytes(s.row() for s in selection.samples),
            # The large ones as the artifacts they become: gzip, byte for byte what put_gzip writes.
            "inputs/universe.json.gz": gzip_bytes(universe_file),
            "screening.jsonl.gz": gzip_bytes(jsonl_bytes(rows)),
            "candidates.jsonl.gz": gzip_bytes(jsonl_bytes(selection.candidates)),
        },
        "summary": {"s0": s0.isoformat(), "issues": len(issues), "histories_read": read, "failed": len(failures),
                    "coverage": coverage, **selection.summary},
        "selected": {code: digests[code] for code in selected},
        "digests": json.dumps(digests, separators=(",", ":")),
        "finished_at": datetime.now(UTC).isoformat(),
    }


def probe_summary(universe: dict, chunks: list[dict], *, s0: date) -> dict:
    """A past day read and screened: the counts the PC's rehearsal reports, and nothing drawn (S0 is before Phase B)."""

    issues = _covered(universe, chunks)
    rows, _evidence, failures, digests, bar_counts = _chunks(chunks)
    sessions = jev_eval.sessions_from_counts(bar_counts, len(rows))
    listed = {i.code for i in issues}
    eligible = [r for r in rows if r["eligible"] and r["code"] in listed]
    passing = [r for r in eligible if r["passed"]]
    return {
        "s0": s0.isoformat(),
        "workbook_sha256": universe["workbook_sha256"],
        "issues": len(issues),
        "histories_read": len(rows),
        "failed": len(failures),
        "coverage": len(rows) / len(issues) if issues else 0.0,
        "minimum_coverage": phase_b.MIN_UNIVERSE_COVERAGE,
        "s0_is_session": s0 in sessions,
        "screened": len(rows),
        "eligible": len(eligible),
        "ineligible_reasons": dict(Counter(r["reason"] for r in rows if not r["eligible"])),
        "passing": len(passing),
        "not_passing": len(eligible) - len(passing),
        "passing_share": len(passing) / len(eligible) if eligible else None,
        "route_membership_among_passing": {r: sum(r in p["routes"] for p in passing) for r in ROUTES},
        "route_d_subgroups_among_passing": {g: sum(route_d_subgroup(p["routes"]) == g for p in passing)
                                            for g in SUBGROUPS},
        "screening_sha256": sha256_hex(jsonl_bytes(rows)),
        "history_digests_sha256": sha256_hex(json.dumps({c: d["history"] for c, d in sorted(digests.items())},
                                                        separators=(",", ":")).encode()),
        "failures": failures[:20],
    }


# ----------------------------------------------------------------- the selected, again


def reread_selected(selected: dict, *, s0: date, start: date, now: datetime, yahoo=None, sleep=None,
                    monotonic=None) -> dict:
    """Each selected security read again with the plan's ``now``; its history must be the one screened."""

    source = providers() if (yahoo is None or sleep is None or monotonic is None) else {}
    yahoo = yahoo or source["yahoo"]()
    sleep = sleep or source["sleep"]
    monotonic = monotonic or source["monotonic"]
    began = monotonic()
    lines, checked, problems, fetch_seconds = {}, {}, [], []
    for code in sorted(selected):
        fetch_began = monotonic()
        chart, history, failure = jev_eval._fetch_one(yahoo, code, start, now, sleep)
        fetch_seconds.append(monotonic() - fetch_began)
        sleep(jev_eval.YAHOO_PAUSE_SECONDS)
        if failure is not None:
            problems.append(f"{code}: not read again ({failure['error']})")
            continue
        line = jev_eval._chart_line(chart)
        digest = history_digest(history)
        checked[code] = {"line": sha256_hex(line), "history": digest,
                         "same_line": sha256_hex(line) == selected[code]["line"],
                         "same_history": digest == selected[code]["history"]}
        if digest != selected[code]["history"]:
            problems.append(f"{code}: the history read again is not the one screened")
        lines[code] = line.decode("utf-8")
    return {"lines": lines, "checked": checked, "problems": problems,
            "measure": {"read": len(lines), "seconds": round(monotonic() - began, 3),
                        "fetch_seconds": _measure(fetch_seconds)}}


# ----------------------------------------------------------------- the requests


def build(planned: dict, reread: dict, *, s0: date, binding: CohortBinding = OFFICIAL, yanoshin=None) -> dict:
    """The frozen ``build`` on a RunStore that holds the plan's files and the selected prices, and nothing else."""

    root = Path(tempfile.mkdtemp(prefix="surge-shadow-day-"))
    try:
        jev_eval.phase_b_init(root, binding.cohort_id, experiment_seed=binding.experiment_seed)
        cohort = phase_b.cohort_store(root, binding.cohort_id)
        if cohort.read_json("cohort.json")["frozen_fingerprint"] != binding.frozen_fingerprint:
            raise DayRefused("protocol", "the cohort created here does not carry the cohort's frozen protocol")
        store = phase_b.day_store(root, binding.cohort_id, s0)
        files: dict[str, str] = {}
        buffer, archive = jev_eval._archive_writer()
        for code in sorted(reread["lines"]):
            archive.write(reread["lines"][code].encode("utf-8"))
        archive.close()
        files[jev_eval.PRICE_ARCHIVE] = store.write_bytes(jev_eval.PRICE_ARCHIVE, buffer.getvalue())
        plan_files = planned["files"]
        for name, artifact in (("inputs/universe.json", "inputs/universe.json.gz"), ("sessions.json", "sessions.json"),
                               ("candidates.jsonl", "candidates.jsonl.gz"), ("population.jsonl", "population.jsonl"),
                               ("manifest.json", "manifest.json")):
            data = plan_files[artifact]
            files[name] = store.write_bytes(name, gzip.decompress(data) if artifact.endswith(".gz") else data)
        store.write_json("stage-plan.json", {"stage": "plan", "finished_at": planned["finished_at"],
                                             "files_sha256": files, **planned["summary"]})
        yanoshin = yanoshin if yanoshin is not None else providers()["yanoshin"]()
        try:
            summary = jev_eval.build(store, yanoshin=yanoshin)
        except jev_eval.EvaluationError as exc:
            raise DayRefused("build", str(exc)) from exc
        return {
            "requests": (store.path / "requests.jsonl").read_text(encoding="utf-8"),
            "tdnet": json.dumps(_file_rows(store, "inputs/tdnet"), ensure_ascii=False, default=str),
            "stages": {"plan": scrub(store.read_json("stage-plan.json")),
                       "build": scrub(store.read_json("stage-build.json"))},
            "summary": summary,
            "cohort": (cohort.path / "cohort.json").read_text(encoding="utf-8"),
        }
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ----------------------------------------------------------------- the artifacts


def _commit_record(binding: CohortBinding, s0: date, status: str, **extra) -> dict:
    return {"system": SYSTEM, "official": False, "cohort_id": binding.cohort_id, "s0": s0.isoformat(),
            "status": status, **extra, "jev": JEV, "teacher_admissible": False, "database_writes": "none"}


def _plan_parts(parts: ArtifactSet, planned: dict) -> None:
    files = planned["files"]
    parts.put("manifest.json", files["manifest.json"], JSON)
    parts.put("sessions.json", files["sessions.json"], JSON)
    for name in ("inputs/universe.json.gz", "screening.jsonl.gz", "candidates.jsonl.gz"):
        parts.put_gzip(name, gzip.decompress(files[name]))
    parts.put("population.jsonl", files["population.jsonl"], JSONL)


def ensure_cohort_records(store: ObjectStore, binding: CohortBinding, cohort_json: str, *,
                          root: str = SHADOW_ROOT) -> list[str]:
    """The shadow cohort's own cohort.json (its first day's) and the calendar, each written once."""

    base = cohort_prefix(binding.cohort_id, root=root)
    written = []
    for name, data in (("cohort.json", cohort_json.encode("utf-8")), ("calendar.json", json_bytes(calendar_record()))):
        if exists(store, f"{base}/{name}"):
            continue
        try:
            store.put_immutable(f"{base}/{name}", data, JSON)
            written.append(name)
        except ImmutableObjectConflict:
            pass  # written in the meantime by another run: the first one stands
    return written


def write_day(store: ObjectStore, *, s0: date, planned: dict, reread: dict, built: dict, run: dict,
              binding: CohortBinding = OFFICIAL, root: str = SHADOW_ROOT) -> dict:
    """A built day's artifacts and its integrity record (status ``built``: nothing was sent)."""

    prefix = day_prefix(binding.cohort_id, s0, root=root)
    if read_commit(store, prefix, DAY_COMMIT) is not None:
        return {"written": False, "reason": "already committed", "prefix": prefix}
    parts = ArtifactSet(store, prefix)
    _plan_parts(parts, planned)
    parts.put_gzip("inputs/prices-selected.jsonl.gz",
                   "".join(reread["lines"][code] for code in sorted(reread["lines"])).encode("utf-8"))
    digests = sorted(json.loads(planned["digests"]).items())
    parts.put_json("inputs/price-digests.json", {"rule": PRICE_RULE, "lines": {c: d["line"] for c, d in digests},
                                                 "histories": {c: d["history"] for c, d in digests},
                                                 "reread": reread["checked"]})
    parts.put_gzip("inputs/tdnet.jsonl.gz", jsonl_bytes(json.loads(built["tdnet"])))
    parts.put("requests.jsonl", built["requests"].encode("utf-8"), JSONL)
    parts.put_json("run.json", {"stages": built["stages"], "preflight": [], "run_started": [], "run_stopped": [],
                                "day_stopped": [], "shadow": run})
    source = {"layout": "Vercel Workflow steps (surge.shadow.day)", "workflow_run_id": run.get("workflow_run_id"),
              "files_sha256": {name: sha for stage in built["stages"].values()
                               for name, sha in (stage.get("files_sha256") or {}).items()}}
    commit = parts.commit(DAY_COMMIT, _commit_record(binding, s0, "built", source=source,
                                                     request_bodies=REQUEST_BODIES),
                          committed_at=datetime.now(UTC).isoformat())
    cohort = ensure_cohort_records(store, binding, built["cohort"], root=root)
    return {"written": True, "prefix": prefix, "artifacts": len(parts.artifacts), "integrity_sha256": commit.sha256,
            "cohort_records": cohort}


def write_stopped_day(store: ObjectStore, *, s0: date, refused: dict, run: dict, planned: dict | None = None,
                      binding: CohortBinding = OFFICIAL, root: str = SHADOW_ROOT) -> dict:
    """A day stopped after its window opened: what was planned, if anything, and why it stopped."""

    prefix = day_prefix(binding.cohort_id, s0, root=root)
    if read_commit(store, prefix, DAY_COMMIT) is not None:
        return {"written": False, "reason": "already committed", "prefix": prefix}
    parts = ArtifactSet(store, prefix)
    if planned is not None:
        _plan_parts(parts, planned)
    stopped_at = datetime.now(UTC).isoformat()
    parts.put_json("run.json", {"stages": {}, "preflight": [], "run_started": [], "run_stopped": [],
                                "day_stopped": [{"stopped_at": stopped_at, "reason": refused["message"],
                                                 "detail": {"kind": refused["kind"], **(refused.get("detail") or {})}}],
                                "shadow": run})
    commit = parts.commit(DAY_COMMIT, _commit_record(binding, s0, "stopped", source={
        "layout": "Vercel Workflow steps (surge.shadow.day)", "workflow_run_id": run.get("workflow_run_id")}),
        committed_at=stopped_at)
    return {"written": True, "prefix": prefix, "artifacts": len(parts.artifacts), "integrity_sha256": commit.sha256}


def record_run(store: ObjectStore, record: dict, *, started_at: datetime, binding: CohortBinding = OFFICIAL,
               root: str = SHADOW_ROOT) -> str:
    """One record per invocation, as the PC's scheduler keeps them (job ``prediction``)."""

    return put_run_record(store, binding.cohort_id, "prediction", {**record, "system": SYSTEM},
                          started_at=started_at, root=root)


__all__ = ["CHUNK_DEADLINE_SECONDS", "CHUNK_SIZE", "JEV", "OFFICIAL", "SHADOW_ROOT", "SYSTEM", "CohortBinding",
           "DayRefused", "build", "check_binding", "day_window", "ensure_cohort_records", "history_digest", "plan",
           "plan_manifest", "probe_summary", "providers", "read_chunk", "read_universe", "record_run",
           "reread_selected", "scheduled_s0", "write_day", "write_stopped_day"]
