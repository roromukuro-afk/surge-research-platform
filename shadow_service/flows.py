"""The shadow's workflows (D-279).

A workflow body is replayed inside the SDK's deterministic sandbox, so this
module imports only the SDK at the top; everything with side effects - Blob,
the frozen Phase B code, the network - is imported inside a step, which runs as
an ordinary function.

- ``noop``: does nothing. It proves the path the scheduled jobs will take:
  a Vercel Cron request reaches the protected production deployment, starts a
  run, one step executes, and the run completes.
- ``stage2_selftest``: Stage 2 in the cloud with synthetic artifacts only. It
  copies part of the committed synthetic fixture into the private Blob store
  through the write-once store, reads every copied day back against its
  integrity record, and checks that the store refuses an overwrite. No real
  data, no model request.
- ``shadow_day``: Stage 3 (``surge.shadow.day``). A Phase B day of the PC's
  cohort read, screened and drawn in steps, the requests built by the frozen
  code and none sent; the artifacts go to Blob under ``surge/phase-b-shadow``.
  As a probe, a past business day is read and screened and nothing is written.
"""

from __future__ import annotations

from vercel.workflow import Workflows, get_step_metadata, sleep

wf = Workflows()

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
        fingerprint = day.check_binding()
        chosen = date.fromisoformat(s0) if s0 else date.fromisoformat(day.scheduled_s0(now)["s0"])
        window = day.day_window(chosen, now, probe=mode == "probe")
    except day.DayRefused as exc:
        return {"refused": exc.record(), "s0": s0, "checked_at": now.isoformat()}
    return {**window, "fingerprint": fingerprint, "checked_at": now.isoformat()}


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
async def day_plan(universe: dict, chunks: list[dict], s0: str, start: str, now: str) -> dict:
    from datetime import date, datetime

    day = _day()
    began = _stamp()
    try:
        planned = day.plan(universe, chunks, s0=date.fromisoformat(s0), start=date.fromisoformat(start),
                           now=datetime.fromisoformat(now))
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
async def day_build(planned: dict, reread: dict, s0: str) -> dict:
    import time
    from datetime import date

    day = _day()
    began, cpu = _stamp(), time.process_time()
    try:
        built = day.build(planned, reread, s0=date.fromisoformat(s0))
    except day.DayRefused as exc:
        return {"refused": exc.record()}
    built["measure"] = {"began": began, "ended": _stamp(), "cpu_seconds": round(time.process_time() - cpu, 3)}
    return built


def _run_record(opened: dict, status: str, detail: dict, trigger: str, requested_at: str) -> dict:
    day = _day()
    from surge.jobs import jev_eval

    return {"job": "prediction", "started_at": opened.get("checked_at") or requested_at,
            "cohort_id": day.OFFICIAL.cohort_id,
            "code": {"head": None, "code_sha256": jev_eval.code_version()["code_sha256"]},
            "status": status, "exit_code": 0 if status in ("built", "closed_day", "not_yet") else 2,
            "s0": opened.get("s0"), "trigger": trigger,
            "schedule": {k: opened.get(k) for k in ("s0", "opens_at", "closes_at")},
            "detail": detail, "finished_at": _stamp()}


@wf.step(max_retries=2)
async def day_write(planned: dict, reread: dict, built: dict, opened: dict, run: dict) -> dict:
    """Every artifact once, the integrity record last, then the run record."""

    from datetime import date, datetime

    from shadow_service import use_workers_src

    use_workers_src()
    from surge.storage.vercel_blob import VercelBlobObjectStore

    day = _day()
    store = VercelBlobObjectStore.from_env()
    run = {**run, "workflow_run_id": get_step_metadata().run_id}
    written = day.write_day(store, s0=date.fromisoformat(opened["s0"]), planned=planned, reread=reread, built=built,
                            run=run)
    summary = built["summary"]
    record = _run_record(opened, "built", {"requests_built": summary["requests"], "by_variant": summary["by_variant"],
                                           "estimated_usd_total": summary["estimated_usd_total"], "jev": day.JEV,
                                           "workflow_run_id": run["workflow_run_id"]},
                         run["trigger"], run["requested_at"])
    key = day.record_run(store, record, started_at=datetime.fromisoformat(record["started_at"]))
    return {"written": written, "run_record": key, "blob_operations": store.operations.as_dict()}


@wf.step(max_retries=2)
async def day_stop(opened: dict, refused: dict, planned: dict | None, run: dict) -> dict:
    """A day stopped once its window was open: what was planned, and why. A refusal before that: a run record only."""

    from datetime import date, datetime

    from shadow_service import use_workers_src

    use_workers_src()
    from surge.storage.vercel_blob import VercelBlobObjectStore

    day = _day()
    store = VercelBlobObjectStore.from_env()
    run = {**run, "workflow_run_id": get_step_metadata().run_id}
    written = None
    if opened.get("now"):  # the window was open: the day is recorded as stopped, as the PC records it
        written = day.write_stopped_day(store, s0=date.fromisoformat(opened["s0"]), refused=refused, run=run,
                                        planned=planned)
    status = "stopped" if written else refused["kind"]
    record = _run_record(opened, status, {"refused": refused, "workflow_run_id": run["workflow_run_id"]},
                         run["trigger"], run["requested_at"])
    key = day.record_run(store, record, started_at=datetime.fromisoformat(record["started_at"]))
    return {"written": written, "run_record": key, "blob_operations": store.operations.as_dict()}


@wf.workflow
async def shadow_day(s0: str | None, mode: str, trigger: str, requested_at: str) -> dict:
    """``mode``: ``day`` (built and written, nothing sent) or ``probe`` (a past day read and screened, nothing written)."""

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

    planned = await day_plan(universe, chunks, opened["s0"], opened["start"], opened["now"])
    if "refused" in planned:
        return {"mode": mode, "refused": planned["refused"], **await day_stop(opened, planned["refused"], None, run)}
    run["steps"]["plan"] = planned.pop("measure")
    reread = await day_reread(planned["selected"], opened["s0"], opened["start"], opened["now"])
    run["steps"]["reread"] = reread["measure"]
    if reread["problems"]:
        refused = {"kind": "reread", "message": "a selected security's history changed between its two reads",
                   "detail": {"problems": reread["problems"][:20]}}
        return {"mode": mode, "refused": refused, **await day_stop(opened, refused, planned, run)}
    built = await day_build(planned, reread, opened["s0"])
    if "refused" in built:
        return {"mode": mode, "refused": built["refused"], **await day_stop(opened, built["refused"], planned, run)}
    run["steps"]["build"] = built.pop("measure")
    written = await day_write(planned, reread, built, opened, run)
    return {"mode": mode, "opened": opened, "summary": planned["summary"], "requests": built["summary"],
            "steps": run["steps"], **written}
