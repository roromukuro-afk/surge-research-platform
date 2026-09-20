"""The shadow service's HTTP surface (D-279): a plain ASGI app under ``/api/shadow/``.

Everything here sits behind the project's Deployment Protection (Vercel
Authentication, All Deployments): a request reaches it only as the project's own
cron, a signed-in member of the team, or a caller holding this project's OIDC
token (Trusted Sources). There is no key in this service and none is read.

    GET  /api/shadow/health[?tokenizer=1][&files=1]
                                            versions, the frozen protocol and input-building code this bundle
                                            carries; tokenizer=1: whether o200k_harmony loads here; files=1:
                                            the SHA-256 of each file the input-building code hash covers
    GET  /api/shadow/cron/noop              the no-op workflow (the cron path, D-279)
    POST /api/shadow/selftest/stage2?confirm=stage2-synthetic
                                            Stage 2 in the cloud with synthetic artifacts
    GET  /api/shadow/selftest/extensions   one Yahoo chart read and one o200k count inside a workflow step
    GET  /api/shadow/selftest/yahoo?s0=YYYY-MM-DD&codes=7203[,...]
                                            the read path on a few securities: each read described (detail), or
                                            &mode=chunk[&universe=150]&confirm=yahoo-chunk for one read step of a
                                            day's size. Nothing is written and no request is built
    POST /api/shadow/stage3/probe?s0=YYYY-MM-DD&confirm=stage3-probe
                                            Stage 3: a past business day read and screened; nothing written
    POST /api/shadow/stage3/day?[s0=YYYY-MM-DD&]confirm=stage3-day
                                            Stage 3: a Phase B day built and written to Blob; nothing sent
    POST /api/shadow/stage3/rehearsal?s0=YYYY-MM-DD&confirm=stage3-rehearsal
                                            the whole of a past business day under surge/rehearsal: its own
                                            cohort, never the evaluation's, nothing sent
    GET  /api/shadow/runs/<run id>          a workflow run's status, and its output once completed
    GET  /api/shadow/runs/<run id>/events   how many events the run wrote, by type
"""

from __future__ import annotations

import asyncio
import json
import platform
from datetime import UTC, datetime
from importlib import metadata
from urllib.parse import parse_qs

from shadow_service import use_workers_src

REPO_ROOT = use_workers_src()

OFFICIAL_COHORT_FINGERPRINT = "73cceb0db1be4e3dcd3a44b5d590bdb31775fa21a712bb473bfea5b725e4bcf6"
SYNTHETIC_COHORT = "surge/phase-b/synthetic-phase-b-fixture"
SELFTEST_DAYS = ("2026-09-24", "2026-09-28")
PROBE_PREFIX = "surge/phase-b-selftest/write-once-probes"


def _json(status: int, payload) -> tuple[int, bytes]:
    return status, json.dumps(payload, ensure_ascii=False, default=str, indent=2).encode("utf-8")


def _version(distribution: str) -> str | None:
    try:
        return metadata.version(distribution)
    except metadata.PackageNotFoundError:
        return None


async def health(request: dict) -> tuple[int, bytes]:
    from surge.evaluation import phase_b
    from surge.evaluation.method import load_method
    from surge.jobs.jev_eval import EVALUATION_VERSION, code_version

    try:
        import curl_cffi  # noqa: F401 - whether the Yahoo transport imports here at all

        curl_cffi_imports = True
    except ImportError:
        curl_cffi_imports = False
    fingerprint = phase_b.fingerprint(phase_b.frozen_protocol(load_method(), evaluation_version=EVALUATION_VERSION))
    return _json(200, {
        "service": "surge-shadow",
        "python": platform.python_version(),
        "packages": {name: _version(name) for name in ("vercel", "vercel-workflow", "curl_cffi", "openpyxl",
                                                        "tiktoken")},
        "curl_cffi_imports": curl_cffi_imports,
        "frozen_protocol_fingerprint": fingerprint,
        "matches_official_cohort": fingerprint == OFFICIAL_COHORT_FINGERPRINT,
        "input_building_code_sha256": code_version()["code_sha256"],
        "repo_root_has_prompts": (REPO_ROOT / "docs" / "prompts" / "MANIFEST.md").exists(),
        **(_tokenizer() if request["query"].get("tokenizer") == ["1"] else {}),
        # Each hashed file's SHA-256 (files of this public repository): what to compare when the combined hash differs.
        **({"input_building_files_sha256": code_version()["files_sha256"]}
           if request["query"].get("files") == ["1"] else {}),
    })


def _tokenizer() -> dict:
    """Whether the o200k_harmony encoding loads here: the frozen build counts every request's tokens with it."""

    from surge.analysis.tokenizer import exact_tokens_or_none

    return {"o200k_harmony_tokens_of_a_probe": exact_tokens_or_none("SURGE shadow tokenizer probe",
                                                                     encoding="o200k_harmony")}


async def _started(run, wait_seconds: float) -> dict:
    try:
        output = await asyncio.wait_for(run.return_value(), wait_seconds)
        return {"run_id": run.run_id, "status": "completed", "output": output}
    except TimeoutError:
        return {"run_id": run.run_id, "status": await run.status(), "output": None}


async def cron_noop(request: dict) -> tuple[int, bytes]:
    from vercel.workflow import start

    from shadow_service.flows import noop

    schedule = request["headers"].get("x-vercel-cron-schedule")
    trigger = f"cron {schedule}" if schedule else "manual"
    run = await start(noop, trigger, datetime.now(UTC).isoformat())
    result = await _started(run, 60)
    print(f"noop workflow {result['run_id']} ({trigger}): {result['status']}", flush=True)
    return _json(200, {"trigger": trigger, **result})


def _selftest_plan() -> tuple[list[str], list[str]]:
    root = REPO_ROOT / "apps" / "web" / "fixtures" / "shadow"
    cohort_files = [f"{SYNTHETIC_COHORT}/{name}" for name in (
        "cohort.json", "calendar.json", "fixture.json", "credit/typesafe-1.json", "reports/report-1.json")]
    day_files, commits, runs = [], [], []
    for day in SELFTEST_DAYS:
        for path in sorted((root / SYNTHETIC_COHORT / day).rglob("*")):
            if path.is_file():
                key = path.relative_to(root).as_posix()
                # Each day's commit records go last, so a reader never sees one before what it names.
                (commits if path.name in ("integrity.json", "outcomes-integrity.json") else day_files).append(key)
        runs += sorted(p.relative_to(root).as_posix() for p in (root / SYNTHETIC_COHORT / "runs" / day).glob("*.json"))
    return cohort_files + day_files + commits + runs, [f"{SYNTHETIC_COHORT}/{day}" for day in SELFTEST_DAYS]


async def selftest_stage2(request: dict) -> tuple[int, bytes]:
    from vercel.workflow import start

    from shadow_service.flows import stage2_selftest

    if request["query"].get("confirm") != ["stage2-synthetic"]:
        return _json(400, {"refused": "add ?confirm=stage2-synthetic: this writes synthetic artifacts to Blob"})
    keys, prefixes = _selftest_plan()
    run = await start(stage2_selftest, keys, prefixes, PROBE_PREFIX)
    result = await _started(run, 240)
    print(f"stage2 selftest {result['run_id']}: {result['status']}", flush=True)
    return _json(200, {"keys": len(keys), **result})


async def selftest_extensions(_request: dict) -> tuple[int, bytes]:
    """The C extensions a day needs, used inside a step: one Yahoo chart (7203.T) and one token count."""

    from vercel.workflow import start

    from shadow_service.flows import extension_selftest

    run = await start(extension_selftest, "7203.T", False)
    result = await _started(run, 90)
    output = result.get("output") or {}
    print(f"extension selftest {result['run_id']}: {result['status']} bars={output.get('bars')} "
          f"crumb={output.get('crumb_obtained')} tokens={output.get('o200k_harmony_tokens')}", flush=True)
    return _json(200, result)


#: A detail probe reads a handful by hand; a chunk probe reads what a day's step reads (surge.shadow.day).
DETAIL_CODES, CHUNK_CODES = 20, 150


async def selftest_yahoo(request: dict) -> tuple[int, bytes]:
    """The read path before a day: the cookie and crumb, the chart, the as-traded history, the splits."""

    from datetime import date

    from vercel.workflow import start

    from shadow_service.flows import yahoo_probe

    query = request["query"]
    mode = (query.get("mode") or ["detail"])[0]
    if mode not in ("detail", "chunk"):
        return _json(400, {"refused": "mode is 'detail' (each read described) or 'chunk' (one read step)"})
    s0 = (query.get("s0") or [None])[0]
    try:
        date.fromisoformat(s0 or "")
    except ValueError:
        return _json(400, {"refused": "a probe names the day whose history it reads: ?s0=YYYY-MM-DD"})
    codes = [code.strip() for code in (query.get("codes") or [""])[0].split(",") if code.strip()]
    universe = int((query.get("universe") or ["0"])[0] or 0)
    if mode == "chunk" and query.get("confirm") != ["yahoo-chunk"]:
        return _json(400, {"refused": f"add ?confirm=yahoo-chunk: this reads up to {CHUNK_CODES} securities"})
    if universe and mode != "chunk":
        return _json(400, {"refused": "codes from the universe are for a chunk probe"})
    if bool(codes) == bool(universe):
        return _json(400, {"refused": "name the securities (?codes=7203,...) or, for a chunk, ?universe=<n>"})
    allowed = CHUNK_CODES if mode == "chunk" else DETAIL_CODES
    if max(len(codes), universe) > allowed:
        return _json(400, {"refused": f"a {mode} probe reads at most {allowed} securities"})
    run = await start(yahoo_probe, codes, mode, s0, universe)
    result = await _started(run, float((query.get("wait") or ["150"])[0]))
    measure = ((result.get("output") or {}).get("measure")) or {}
    print(f"yahoo probe {result['run_id']} ({mode}, {len(codes) or universe} securities): {result['status']} "
          f"read={measure.get('read')} failed={measure.get('failed')} seconds={measure.get('seconds')}", flush=True)
    return _json(200, {"mode": mode, "s0": s0, "codes": codes or f"universe[:{universe}]", **result})


async def stage3_start(request: dict) -> tuple[int, bytes]:
    """Start a Stage 3 run and answer at once: a day takes about half an hour of steps."""

    from datetime import date

    from vercel.workflow import start

    from shadow_service.flows import shadow_day

    mode = request["path"].rsplit("/", 1)[-1]
    if request["query"].get("confirm") != [f"stage3-{mode}"]:
        return _json(400, {"refused": f"add ?confirm=stage3-{mode}: this reads every listed security from Yahoo"
                                      + (" and writes the day to Blob" if mode != "probe" else "")})
    s0 = (request["query"].get("s0") or [None])[0]
    if mode in ("probe", "rehearsal") and not s0:
        return _json(400, {"refused": f"a {mode} names its S0: ?s0=YYYY-MM-DD"})
    if s0:
        try:
            date.fromisoformat(s0)
        except ValueError:
            return _json(400, {"refused": f"not a date: {s0!r}"})
    run = await start(shadow_day, s0, mode, "manual", datetime.now(UTC).isoformat())
    print(f"stage3 {mode} {run.run_id} (S0 {s0 or 'as scheduled'}): started", flush=True)
    return _json(202, {"run_id": run.run_id, "mode": mode, "s0": s0, "status": await run.status()})


async def run_events(request: dict) -> tuple[int, bytes]:
    """The run's events by type: what it counts against the Workflow allowance (50,000 events a month on Hobby)."""

    from collections import Counter

    from vercel.workflow._internal.world import PaginationOptions, get_world

    run_id = request["path"].split("/")[-2]
    if not run_id.startswith("wrun_"):
        return _json(404, {"error": "not a workflow run id"})
    counts: Counter = Counter()
    cursor = None
    while True:
        page = await get_world().events_list(run_id, pagination=PaginationOptions(limit=1000, cursor=cursor))
        counts.update(event.event_type for event in page.data)
        if not page.has_more or not page.cursor:
            break
        cursor = page.cursor
    return _json(200, {"run_id": run_id, "events": sum(counts.values()), "by_type": dict(sorted(counts.items()))})


async def run_status(request: dict) -> tuple[int, bytes]:
    from vercel.workflow import Run

    run_id = request["path"].rsplit("/", 1)[-1]
    if not run_id.startswith("wrun_"):
        return _json(404, {"error": "not a workflow run id"})
    run = Run(run_id)
    status = await run.status()
    output = await run.return_value() if status == "completed" else None
    return _json(200, {"run_id": run_id, "status": status, "output": output})


ROUTES = {
    ("GET", "/api/shadow/health"): health,
    ("GET", "/api/shadow/cron/noop"): cron_noop,
    ("POST", "/api/shadow/selftest/stage2"): selftest_stage2,
    ("GET", "/api/shadow/selftest/extensions"): selftest_extensions,
    ("GET", "/api/shadow/selftest/yahoo"): selftest_yahoo,
    ("POST", "/api/shadow/selftest/yahoo"): selftest_yahoo,
    ("POST", "/api/shadow/stage3/probe"): stage3_start,
    ("POST", "/api/shadow/stage3/day"): stage3_start,
    ("POST", "/api/shadow/stage3/rehearsal"): stage3_start,
}


async def _dispatch(method: str, path: str, request: dict) -> tuple[int, bytes]:
    handler = ROUTES.get((method, path))
    if handler is None and method == "GET" and path.startswith("/api/shadow/runs/"):
        handler = run_events if path.endswith("/events") else run_status
    if handler is None:
        return _json(404, {"error": f"no route {method} {path}"})
    try:
        return await handler(request)
    except Exception as exc:  # noqa: BLE001 - the response says what failed; the log keeps the rest
        print(f"{method} {path} failed: {type(exc).__name__}: {exc}", flush=True)
        return _json(500, {"error": type(exc).__name__, "detail": str(exc)[:500]})


async def app(scope, receive, send) -> None:
    if scope["type"] == "lifespan":
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return
    if scope["type"] != "http":
        return
    request = {
        "path": scope["path"],
        "headers": {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])},
        "query": parse_qs(scope.get("query_string", b"").decode("latin-1")),
    }
    status, body = await _dispatch(scope["method"], scope["path"], request)
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json; charset=utf-8"), (b"cache-control", b"no-store")]})
    await send({"type": "http.response.body", "body": body})
