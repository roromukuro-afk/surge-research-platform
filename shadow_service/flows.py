"""The shadow's workflows (D-279).

A workflow body is replayed inside the SDK's deterministic sandbox, so this
module imports little at the top; everything with side effects - Blob, the
frozen Phase B code, the network - is imported inside a step, which runs as an
ordinary function.

The exception is two C-extension packages the steps use: curl_cffi (the Yahoo
read) and tiktoken (the build's token counts). From its first run on, the SDK
replaces ``sys.modules`` with a mapping of its own, and a C extension loaded for
the first time after that fails inside a step ("SystemError: ... dictobject.c:
... bad argument to internal function": every Yahoo read of the first cloud
probe, 2026-09-19). So they are imported here, on the host, before any run, and
the sandbox shares them (``passthrough_modules``) instead of loading them again
when it replays this module.

- ``noop``: does nothing. It proves the path the scheduled jobs will take:
  a Vercel Cron request reaches the protected production deployment, starts a
  run, one step executes, and the run completes.
- ``stage2_selftest``: Stage 2 in the cloud with synthetic artifacts only. It
  copies part of the committed synthetic fixture into the private Blob store
  through the write-once store, reads every copied day back against its
  integrity record, and checks that the store refuses an overwrite. No real
  data, no model request.
- ``extension_selftest``: one Yahoo chart read and one o200k token count inside
  a step (offline: the extensions only) - what the day's read and build need.
- ``yahoo_probe``: the read path on a handful of securities (what each read
  returned), or one production-sized read step, before a day is run. Nothing is
  written and no request is built.
- ``shadow_day``: Stage 3 (``surge.shadow.day``). A Phase B day of the PC's
  cohort read, screened and drawn in steps, the requests built by the frozen
  code and none sent; the artifacts go to Blob under ``surge/phase-b-shadow``.
  As a probe, a past business day is read and screened and nothing is written;
  as a rehearsal, a past business day is run whole under ``surge/rehearsal``,
  which is its own cohort and never the evaluation's.
"""

from __future__ import annotations

import curl_cffi.requests  # noqa: F401 - loaded on the host before any run (see above)
import tiktoken  # noqa: F401 - the same
from vercel.workflow import Workflows, get_step_metadata, sandbox, sleep

#: The C-extension packages loaded above, with what they load: shared with the sandbox, never loaded twice.
HOST_EXTENSIONS = frozenset({"curl_cffi", "cffi", "_cffi_backend", "tiktoken", "tiktoken_ext", "regex"})
wf = Workflows(sandbox_policy=sandbox.SandboxPolicy(passthrough_modules=HOST_EXTENSIONS))

JSON = "application/json"
#: surge.shadow.day.CHUNK_SIZE: securities per read step (a workflow body imports nothing of surge).
CHUNK_SIZE = 150


# ----------------------------------------------------------------- noop


@wf.step(max_retries=0)
async def noop_step(trigger: str, requested_at: str) -> dict:
    import platform
    from datetime import UTC, datetime

    from shadow_service import use_workers_src

    use_workers_src()
    from surge.jobs.jev_eval import code_version

    info = get_step_metadata()
    # The steps run from their own bundle: this is the input-building code a day's manifest would name.
    return {"did": "nothing", "trigger": trigger, "requested_at": requested_at, "run_id": info.run_id,
            "step_ran_at": datetime.now(UTC).isoformat(), "attempt": info.attempt,
            "python": platform.python_version(), "input_building_code_sha256": code_version()["code_sha256"]}


@wf.workflow
async def noop(trigger: str, requested_at: str) -> dict:
    return await noop_step(trigger, requested_at)


# ----------------------------------------------------------------- the extensions, inside a step


@wf.step(max_retries=0)
async def extension_step(symbol: str, offline: bool) -> dict:
    """What a read step and the build do with the C extensions: a Yahoo chart read and an o200k count."""

    import platform
    from datetime import UTC, date, datetime, timedelta
    from importlib import metadata

    from shadow_service import use_workers_src

    use_workers_src()
    from curl_cffi import requests as curl_requests

    versions = {name: metadata.version(name) for name in ("curl_cffi", "tiktoken")}
    if offline:
        import tiktoken

        curl_requests.Session(impersonate="chrome").close()
        return {"ok": True, "offline": True, "python": platform.python_version(), **versions,
                "encodings": len(tiktoken.list_encoding_names())}
    from surge.analysis.tokenizer import exact_tokens_or_none
    from surge.evaluation.prices import fetch_chart
    from surge.providers.yahoo_finance import YahooSession

    yahoo = YahooSession()
    result = fetch_chart(symbol, date.today() - timedelta(days=30), session=yahoo, now=datetime.now(UTC))
    return {"ok": True, "offline": False, "python": platform.python_version(), **versions,
            "bars": len(result.get("timestamp") or []), "crumb_obtained": bool(yahoo._crumb),
            "o200k_harmony_tokens": exact_tokens_or_none("SURGE shadow tokenizer probe", encoding="o200k_harmony")}


@wf.workflow
async def extension_selftest(symbol: str, offline: bool) -> dict:
    return await extension_step(symbol, offline)


# ----------------------------------------------------------------- Stage 2 self-test


def _content_type(key: str) -> str:
    if key.endswith(".gz"):
        return "application/gzip"
    if key.endswith(".jsonl"):
        return "application/x-ndjson"
    return JSON


@wf.step(max_retries=1)
async def selftest_copy(keys: list[str]) -> dict:
    """Copy fixture objects into Blob byte for byte (a retry lands on the same keys: write-once, no duplicates)."""

    from shadow_service import use_workers_src

    root = use_workers_src()
    from surge.storage.vercel_blob import VercelBlobObjectStore

    fixture = root / "apps" / "web" / "fixtures" / "shadow"
    store = VercelBlobObjectStore.from_env()
    written = {}
    for key in keys:
        stored = store.put_immutable(key, (fixture / key).read_bytes(), _content_type(key))
        written[key] = {"sha256": stored.sha256, "bytes": stored.bytes, "created": stored.created}
    return {"written": written, "operations": store.operations.as_dict()}


@wf.step(max_retries=1)
async def selftest_verify(prefixes: list[str]) -> dict:
    """Every copied day read back from Blob and checked against its commit records."""

    from shadow_service import use_workers_src

    use_workers_src()
    from surge.shadow.artifacts import DAY_COMMIT, OUTCOME_COMMIT, read_commit, verify_committed
    from surge.storage.vercel_blob import VercelBlobObjectStore

    store = VercelBlobObjectStore.from_env()
    checked = {}
    for prefix in prefixes:
        for name in (DAY_COMMIT, OUTCOME_COMMIT):
            commit = read_commit(store, prefix, name)
            if commit is None:
                checked[f"{prefix}/{name}"] = "absent"
                continue
            problems = verify_committed(store, prefix, commit)
            checked[f"{prefix}/{name}"] = problems or f"verified {len(commit['artifacts'])} artifacts"
    return {"checked": checked, "operations": store.operations.as_dict()}


@wf.step(max_retries=0)
async def selftest_write_once(probe_prefix: str) -> dict:
    """The store keeps what it holds: the same bytes again are a re-run, other bytes are refused."""

    from shadow_service import use_workers_src

    use_workers_src()
    from surge.storage.base import ImmutableObjectConflict
    from surge.storage.vercel_blob import VercelBlobObjectStore

    info = get_step_metadata()
    key = f"{probe_prefix}/{info.run_id}.json"
    store = VercelBlobObjectStore.from_env()
    first = store.put_immutable(key, b'{"probe": 1}\n', JSON)
    again = store.put_immutable(key, b'{"probe": 1}\n', JSON)
    try:
        store.put_immutable(key, b'{"probe": 2}\n', JSON)
        refused = False
    except ImmutableObjectConflict:
        refused = True
    return {"key": key, "first_created": first.created, "same_bytes_again_created": again.created,
            "different_bytes_refused": refused, "still_the_first_bytes": store.get(key) == b'{"probe": 1}\n',
            "operations": store.operations.as_dict()}


@wf.workflow
async def stage2_selftest(keys: list[str], prefixes: list[str], probe_prefix: str) -> dict:
    copied = await selftest_copy(keys)
    verified = await selftest_verify(prefixes)
    probe = await selftest_write_once(probe_prefix)
    return {"copied": len(copied["written"]),
            "created": sum(1 for w in copied["written"].values() if w["created"]),
            "copy_operations": copied["operations"], "verified": verified, "write_once": probe}


# ----------------------------------------------------------------- Stage 3: a day, and no model request


def _day():
    from shadow_service import use_workers_src

    use_workers_src()
    from surge.shadow import day

    return day


def _stamp() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()


@wf.step(max_retries=0)
async def day_open(s0: str | None, mode: str) -> dict:
    """The cohort's protocol recomputed here, then the day's window: plan_day's refusals, or when it opens."""

    from datetime import date

    day = _day()
    now = day.providers()["now"]()
    try:
        binding, root, system = day.binding_of(mode)
        fingerprint = day.check_binding(binding)
        chosen = date.fromisoformat(s0) if s0 else date.fromisoformat(day.scheduled_s0(now)["s0"])
        # A probe and a rehearsal take a past business day: its bars are final, and no window is waited for.
        window = day.day_window(chosen, now, probe=mode in ("probe", "rehearsal"))
    except day.DayRefused as exc:
        return {"refused": exc.record(), "s0": s0, "checked_at": now.isoformat(), "mode": mode}
    return {**window, "fingerprint": fingerprint, "checked_at": now.isoformat(), "mode": mode,
            "cohort_id": binding.cohort_id, "root": root, "system": system}


@wf.step(max_retries=2)
async def day_universe() -> dict:
    began = _stamp()
    universe = _day().read_universe()
    return {**universe, "measure": {"began": began, "ended": _stamp(), "issues": len(universe["issues"])}}


@wf.step(max_retries=1)
async def day_chunk(codes: list[str], s0: str, start: str, now: str) -> dict:
    from datetime import date, datetime

    began = _stamp()
    part = _day().read_chunk(codes, s0=date.fromisoformat(s0), start=date.fromisoformat(start),
                             now=datetime.fromisoformat(now))
    part["measure"].update(began=began, ended=_stamp(), attempt=get_step_metadata().attempt)
    return part


@wf.step(max_retries=0)
async def day_probe(universe: dict, chunks: list[dict], s0: str) -> dict:
    from datetime import date

    return _day().probe_summary(universe, chunks, s0=date.fromisoformat(s0))


@wf.step(max_retries=0)
async def day_plan(universe: dict, chunks: list[dict], s0: str, start: str, now: str, mode: str) -> dict:
    from datetime import date, datetime

    day = _day()
    binding, _root, _system = day.binding_of(mode)
    began = _stamp()
    try:
        planned = day.plan(universe, chunks, s0=date.fromisoformat(s0), start=date.fromisoformat(start),
                           now=datetime.fromisoformat(now), binding=binding, rehearsal=mode == "rehearsal")
    except day.DayRefused as exc:
        return {"refused": exc.record()}
    planned["measure"] = {"began": began, "ended": _stamp()}
    return planned


@wf.step(max_retries=1)
async def day_reread(selected: dict, s0: str, start: str, now: str) -> dict:
    from datetime import date, datetime

    began = _stamp()
    reread = _day().reread_selected(selected, s0=date.fromisoformat(s0), start=date.fromisoformat(start),
                                    now=datetime.fromisoformat(now))
    reread["measure"].update(began=began, ended=_stamp())
    return reread


@wf.step(max_retries=1)
async def day_build(planned: dict, reread: dict, s0: str, mode: str) -> dict:
    import time
    from datetime import date

    day = _day()
    binding, _root, _system = day.binding_of(mode)
    began, cpu = _stamp(), time.process_time()
    try:
        built = day.build(planned, reread, s0=date.fromisoformat(s0), binding=binding,
                          rehearsal=mode == "rehearsal")
    except day.DayRefused as exc:
        return {"refused": exc.record()}
    built["measure"] = {"began": began, "ended": _stamp(), "cpu_seconds": round(time.process_time() - cpu, 3)}
    return built


def _run_record(opened: dict, status: str, detail: dict, trigger: str, requested_at: str) -> dict:
    day = _day()
    from surge.jobs import jev_eval

    binding, _root, _system = day.binding_of(opened.get("mode") or "day")
    return {"job": "prediction", "started_at": opened.get("checked_at") or requested_at,
            "cohort_id": binding.cohort_id,
            "code": {"head": None, "code_sha256": jev_eval.code_version()["code_sha256"]},
            "status": status, "exit_code": 0 if status in ("built", "closed_day", "not_yet") else 2,
            "s0": opened.get("s0"), "trigger": trigger,
            "schedule": {k: opened.get(k) for k in ("s0", "opens_at", "closes_at")},
            "detail": detail, "finished_at": _stamp()}


@wf.step(max_retries=2)
async def day_write(planned: dict, reread: dict, built: dict, opened: dict, run: dict) -> dict:
    """Every artifact once, the integrity record last, then the run record. A rehearsal writes under its own
    root, never the cohort's."""

    from datetime import date, datetime

    from shadow_service import use_workers_src

    use_workers_src()
    from surge.storage.vercel_blob import VercelBlobObjectStore

    day = _day()
    binding, root, system = day.binding_of(run["mode"])
    store = VercelBlobObjectStore.from_env()
    run = {**run, "workflow_run_id": get_step_metadata().run_id}
    written = day.write_day(store, s0=date.fromisoformat(opened["s0"]), planned=planned, reread=reread, built=built,
                            run=run, binding=binding, root=root, system=system)
    summary = built["summary"]
    record = _run_record(opened, "built", {"requests_built": summary["requests"], "by_variant": summary["by_variant"],
                                           "estimated_usd_total": summary["estimated_usd_total"], "jev": day.JEV,
                                           "workflow_run_id": run["workflow_run_id"]},
                         run["trigger"], run["requested_at"])
    key = day.record_run(store, record, started_at=datetime.fromisoformat(record["started_at"]),
                         binding=binding, root=root, system=system)
    return {"written": written, "run_record": key, "blob_operations": store.operations.as_dict()}


@wf.step(max_retries=2)
async def day_stop(opened: dict, refused: dict, planned: dict | None, run: dict) -> dict:
    """A day stopped once its window was open: what was planned, and why. A refusal before that: a run record only."""

    from datetime import date, datetime

    from shadow_service import use_workers_src

    use_workers_src()
    from surge.storage.vercel_blob import VercelBlobObjectStore

    day = _day()
    binding, root, system = day.binding_of(run["mode"])
    store = VercelBlobObjectStore.from_env()
    run = {**run, "workflow_run_id": get_step_metadata().run_id}
    written = None
    if opened.get("now"):  # the window was open: the day is recorded as stopped, as the PC records it
        written = day.write_stopped_day(store, s0=date.fromisoformat(opened["s0"]), refused=refused, run=run,
                                        planned=planned, binding=binding, root=root, system=system)
    status = "stopped" if written else refused["kind"]
    record = _run_record(opened, status, {"refused": refused, "workflow_run_id": run["workflow_run_id"]},
                         run["trigger"], run["requested_at"])
    key = day.record_run(store, record, started_at=datetime.fromisoformat(record["started_at"]),
                         binding=binding, root=root, system=system)
    return {"written": written, "run_record": key, "blob_operations": store.operations.as_dict()}


# ----------------------------------------------------------------- the read path, before a day


@wf.step(max_retries=0)
async def probe_universe(limit: int) -> dict:
    """The universe's first ``limit`` codes: a production-shaped read step reads production's securities."""

    universe = _day().read_universe()
    return {"workbook_sha256": universe["workbook_sha256"], "issues": len(universe["issues"]),
            "codes": [issue["code"] for issue in universe["issues"]][:limit]}


@wf.step(max_retries=0)
async def probe_read(codes: list[str], mode: str, s0: str) -> dict:
    """``mode`` ``detail``: each security's read described; ``chunk``: one read step exactly as a day reads one."""

    import json
    from datetime import UTC, date, datetime, timedelta

    from surge.evaluation import phase_b

    day = _day()
    began, s0_date = _stamp(), date.fromisoformat(s0)
    start, now = s0_date - timedelta(days=phase_b.HISTORY_LOOKBACK_DAYS), datetime.now(UTC)
    if mode == "chunk":
        part = day.read_chunk(codes, s0=s0_date, start=start, now=now)
        read = json.loads(part["data"])
        result = {"mode": mode, "read": len(read["rows"]), "not_read": len(part["remaining"]),
                  "failures": read["failures"][:10],
                  "history_digests": dict(sorted((code, digest["history"])
                                                 for code, digest in read["digests"].items())[:5]),
                  "measure": part["measure"]}
    else:
        result = {"mode": mode, **day.probe_codes(codes, start=start, now=now)}
    result["measure"].update(began=began, ended=_stamp(), s0=s0, start=start.isoformat(), codes=len(codes),
                             attempt=get_step_metadata().attempt)
    return result


@wf.workflow
async def yahoo_probe(codes: list[str], mode: str, s0: str, universe: int) -> dict:
    """``universe``: read that many of the listed securities' codes first, as a day's read step would get them."""

    picked = await probe_universe(universe) if universe else None
    read = await probe_read(picked["codes"] if picked else codes, mode, s0)
    return {"universe": None if picked is None else {"workbook_sha256": picked["workbook_sha256"],
                                                     "issues": picked["issues"]}, **read}


# ----------------------------------------------------------------- Stage 3: the day itself


@wf.workflow
async def shadow_day(s0: str | None, mode: str, trigger: str, requested_at: str) -> dict:
    """``mode``: ``day`` (the cohort's day, built and written, nothing sent), ``probe`` (a past day read and
    screened, nothing written) or ``rehearsal`` (a past day run whole under ``surge/rehearsal``)."""

    opened = await day_open(s0, mode)
    for _ in range(3):  # started early: wait for the window (a step computes how long; the body only counts)
        if "wait_seconds" not in opened:
            break
        await sleep(opened["wait_seconds"])
        opened = await day_open(opened["s0"], mode)
    if "wait_seconds" in opened:
        opened = {**opened, "refused": {"kind": "not_yet", "message": f"S0 {opened['s0']}: still not open after "
                                                                      "three waits"}}
    run = {"mode": mode, "trigger": trigger, "requested_at": requested_at, "opened": opened, "steps": {}}
    if "refused" in opened:
        if mode == "probe":
            return {"mode": mode, "refused": opened["refused"]}
        return {"mode": mode, "refused": opened["refused"], **await day_stop(opened, opened["refused"], None, run)}

    universe = await day_universe()
    run["steps"]["universe"] = universe.pop("measure")
    queue = [issue["code"] for issue in universe["issues"]]
    chunks, measures = [], []
    while queue:
        part = await day_chunk(queue[:CHUNK_SIZE], opened["s0"], opened["start"], opened["now"])
        if not part["codes"]:
            raise RuntimeError("a step read nothing: the day cannot move on")
        measures.append(part.pop("measure"))
        chunks.append(part)
        queue = part["remaining"] + queue[CHUNK_SIZE:]
    run["steps"]["chunks"] = measures
    if mode == "probe":
        return {"mode": mode, "opened": opened, "summary": await day_probe(universe, chunks, opened["s0"]),
                "steps": run["steps"]}

    planned = await day_plan(universe, chunks, opened["s0"], opened["start"], opened["now"], mode)
    if "refused" in planned:
        return {"mode": mode, "refused": planned["refused"], **await day_stop(opened, planned["refused"], None, run)}
    run["steps"]["plan"] = planned.pop("measure")
    reread = await day_reread(planned["selected"], opened["s0"], opened["start"], opened["now"])
    run["steps"]["reread"] = reread["measure"]
    if reread["problems"]:
        refused = {"kind": "reread", "message": "a selected security's history changed between its two reads",
                   "detail": {"problems": reread["problems"][:20]}}
        return {"mode": mode, "refused": refused, **await day_stop(opened, refused, planned, run)}
    built = await day_build(planned, reread, opened["s0"], mode)
    if "refused" in built:
        return {"mode": mode, "refused": built["refused"], **await day_stop(opened, built["refused"], planned, run)}
    run["steps"]["build"] = built.pop("measure")
    written = await day_write(planned, reread, built, opened, run)
    return {"mode": mode, "opened": opened, "summary": planned["summary"], "requests": built["summary"],
            "steps": run["steps"], **written}
