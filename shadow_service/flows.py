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
"""

from __future__ import annotations

from vercel.workflow import Workflows, get_step_metadata

wf = Workflows()

JSON = "application/json"


# ----------------------------------------------------------------- noop


@wf.step(max_retries=0)
async def noop_step(trigger: str, requested_at: str) -> dict:
    import platform
    from datetime import UTC, datetime

    info = get_step_metadata()
    return {"did": "nothing", "trigger": trigger, "requested_at": requested_at, "run_id": info.run_id,
            "step_ran_at": datetime.now(UTC).isoformat(), "attempt": info.attempt,
            "python": platform.python_version()}


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
