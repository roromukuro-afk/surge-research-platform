"""The shadow's Vercel service (D-279): its HTTP surface and its workflows, offline.

The workflows run on the SDK's local world, in process: that is what caught a
package import touching the filesystem inside the deterministic sandbox, which
the cloud would have refused the same way. Blob is the fake from test_shadow.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile

import pytest

from surge.evaluation.method import REPO_ROOT

pytest.importorskip("vercel.workflow")
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from shadow_service import app as service  # noqa: E402

OFFICIAL = "73cceb0db1be4e3dcd3a44b5d590bdb31775fa21a712bb473bfea5b725e4bcf6"


def _call(method: str, path: str, query: bytes = b"", headers=()):
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "method": method, "path": path, "query_string": query,
             "headers": [(k.encode(), v.encode()) for k, v in headers]}
    asyncio.run(service.app(scope, receive, send))
    return sent[0]["status"], json.loads(sent[1]["body"])


def test_health_names_the_frozen_protocol_this_code_carries():
    status, body = _call("GET", "/api/shadow/health")
    assert status == 200 and body["frozen_protocol_fingerprint"] == OFFICIAL and body["matches_official_cohort"]
    assert body["repo_root_has_prompts"] is True


def test_unknown_routes_and_an_unconfirmed_selftest_are_refused():
    assert _call("GET", "/api/shadow/nope")[0] == 404
    assert _call("POST", "/api/shadow/selftest/stage2")[0] == 400
    assert _call("GET", "/api/shadow/runs/not-a-run")[0] == 404


def test_the_selftest_writes_commit_records_after_what_they_name():
    keys, prefixes = service._selftest_plan()
    commits = [i for i, key in enumerate(keys) if key.endswith("integrity.json")]
    for prefix in prefixes:
        named = [i for i, key in enumerate(keys) if key.startswith(prefix + "/") and not key.endswith("integrity.json")]
        own = [i for i in commits if keys[i].startswith(prefix + "/")]
        assert own and min(own) > max(named, default=-1)
    assert all(key.startswith("surge/phase-b/synthetic-phase-b-fixture/") for key in keys)  # synthetic only


# The local world keeps its data in WORKFLOW_LOCAL_DATA_DIR, or else in ./.workflow-data. Point it at a
# temporary directory for the whole session, not per test: a delivery the embedded queue makes after a test
# has ended must not land in the working tree either.
os.environ.setdefault("WORKFLOW_LOCAL_DATA_DIR", tempfile.mkdtemp(prefix="surge-workflow-test-"))


@pytest.fixture()
def local_world(monkeypatch):
    # The SDK keeps one world per process, bound to the event loop that first used it; each test runs its own
    # loop, so each gets a fresh world (set_world is the SDK's reset hook), closed in the loop that made it.
    from vercel.workflow._internal.world import set_world

    monkeypatch.setenv("WORKFLOW_TARGET_WORLD", "local")
    set_world(None)
    yield
    set_world(None)


def _run(workflow, *args):
    from vercel.workflow import start
    from vercel.workflow._internal.world import get_world

    async def go():
        run = await start(workflow, *args)
        try:
            return await asyncio.wait_for(run.return_value(), 60)
        finally:
            await get_world().aclose()

    return asyncio.run(go())


def test_the_noop_workflow_runs_to_completion_and_does_nothing(local_world):
    from shadow_service.flows import noop

    output = _run(noop, "test", "2026-09-19T00:00:00+00:00")
    assert output["did"] == "nothing" and output["trigger"] == "test" and output["attempt"] == 1


def test_the_stage2_selftest_copies_verifies_and_is_refused_an_overwrite(local_world, monkeypatch):
    from shadow_service.flows import stage2_selftest

    from surge.storage import vercel_blob
    from test_shadow import FakeBlobClient

    client = FakeBlobClient()
    monkeypatch.setattr(vercel_blob.VercelBlobObjectStore, "from_env", classmethod(lambda cls, env=None: cls(client)))
    keys, prefixes = service._selftest_plan()
    output = _run(stage2_selftest, keys, prefixes, service.PROBE_PREFIX)
    assert output["copied"] == len(keys) == output["created"]
    assert all(isinstance(v, str) and (v.startswith("verified") or v == "absent")
               for v in output["verified"]["checked"].values())
    probe = output["write_once"]
    assert probe["first_created"] and not probe["same_bytes_again_created"]
    assert probe["different_bytes_refused"] and probe["still_the_first_bytes"]
