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
import time
from pathlib import Path

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
    from surge.jobs.jev_eval import code_version

    assert body["input_building_code_sha256"] == code_version()["code_sha256"]  # the PC's manifests name this


def test_unknown_routes_and_an_unconfirmed_selftest_are_refused():
    assert _call("GET", "/api/shadow/nope")[0] == 404
    assert _call("POST", "/api/shadow/selftest/stage2")[0] == 400
    assert _call("GET", "/api/shadow/runs/not-a-run")[0] == 404
    assert _call("GET", "/api/shadow/runs/not-a-run/events")[0] == 404
    # Stage 3 reads every listed security from Yahoo: nothing starts without the word for it.
    assert _call("POST", "/api/shadow/stage3/probe")[0] == 400
    assert _call("POST", "/api/shadow/stage3/probe", b"confirm=stage3-probe")[0] == 400  # a probe names its S0
    assert _call("POST", "/api/shadow/stage3/day", b"confirm=stage3-probe")[0] == 400  # the other mode's word
    assert _call("POST", "/api/shadow/stage3/day", b"s0=someday&confirm=stage3-day")[0] == 400


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
def local_world(monkeypatch, tmp_path):
    # The SDK keeps one world per process, bound to the event loop that first used it; each test runs its own
    # loop, so each gets a fresh world (set_world is the SDK's reset hook), closed in the loop that made it, and
    # its own data: what one test's queue left behind is not another test's to deliver.
    from vercel.workflow._internal.world import set_world

    monkeypatch.setenv("WORKFLOW_TARGET_WORLD", "local")
    monkeypatch.setenv("WORKFLOW_LOCAL_DATA_DIR", str(tmp_path / "workflow-data"))
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
    from surge.jobs.jev_eval import code_version

    assert output["input_building_code_sha256"] == code_version()["code_sha256"]


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


# ----------------------------------------------------------------- Stage 3: a day through the workflow


@pytest.fixture()
def pc_cohort(tmp_path, monkeypatch):
    """The PC's cohort under the official id and seed, on 160 made-up securities: two read steps of 150."""

    import test_evaluation_phase_b as pb
    from surge.shadow import day

    monkeypatch.setattr(pb, "COHORT", day.OFFICIAL.cohort_id)
    return pb._setup(tmp_path, monkeypatch, n=160, seed=day.OFFICIAL.experiment_seed)


def _stage3_fakes(monkeypatch, env, client, *, yahoo=None):
    import test_evaluation_phase_b as pb
    from surge.shadow import day
    from surge.storage import vercel_blob
    from test_evaluation import _FakeYahoo

    fakes = {"issues": lambda: (env.issues, "f" * 64), "yahoo": yahoo or (lambda: _FakeYahoo(env.charts)),
             "yanoshin": pb._FakeYanoshinRecent, "sleep": lambda _s: None, "monotonic": time.monotonic,
             "now": lambda: pb.EVENING}
    monkeypatch.setattr(day, "providers", lambda: fakes)
    monkeypatch.setattr(vercel_blob.VercelBlobObjectStore, "from_env", classmethod(lambda cls, env=None: cls(client)))
    return vercel_blob.VercelBlobObjectStore(client)


def test_the_workflow_reads_150_securities_a_step():
    from shadow_service import flows

    from surge.shadow import day

    assert flows.CHUNK_SIZE == day.CHUNK_SIZE == 150


def test_the_read_path_probe_describes_each_security_and_writes_nothing(local_world, pc_cohort, monkeypatch):
    """The check before a day: the read path on securities named by hand, in a step."""

    from shadow_service.flows import yahoo_probe

    from test_shadow import FakeBlobClient

    client = FakeBlobClient()
    _stage3_fakes(monkeypatch, pc_cohort, client)
    codes = [issue.code for issue in pc_cohort.issues][:3]
    detail = _run(yahoo_probe, codes, "detail", "2026-09-24", 0)
    assert detail["universe"] is None and [r["code"] for r in detail["read"]] == codes
    assert detail["measure"]["read"] == 3 and not detail["failures"]
    assert all(r["bars"] > 0 and r["history_digest"] for r in detail["read"])
    assert client.objects == {}  # a probe writes nothing, and builds no request


def test_the_read_path_probe_reads_a_step_s_worth_of_the_universe(local_world, pc_cohort, monkeypatch):
    """The same check at a day's size: the universe's own codes, read by the step a day uses."""

    from shadow_service.flows import yahoo_probe

    from test_shadow import FakeBlobClient

    client = FakeBlobClient()
    _stage3_fakes(monkeypatch, pc_cohort, client)
    chunk = _run(yahoo_probe, [], "chunk", "2026-09-24", 5)
    assert chunk["universe"]["issues"] == 160 and chunk["read"] == 5 and chunk["not_read"] == 0
    assert chunk["measure"]["attempted"] == 5 and len(chunk["history_digests"]) == 5
    assert client.objects == {}


def test_stage3_the_day_through_the_workflow_is_the_pc_s_day_committed_in_blob(local_world, pc_cohort, monkeypatch):
    from shadow_service.flows import shadow_day

    import test_evaluation_phase_b as pb
    from surge.shadow import day
    from surge.shadow.artifacts import DAY_COMMIT, jsonl_bytes, read_commit, read_verified, verify_committed
    from surge.shadow.export import _parquet_rows
    from test_shadow import FakeBlobClient

    client = FakeBlobClient()
    blob = _stage3_fakes(monkeypatch, pc_cohort, client)
    pc = pb._planned_and_built(pc_cohort).path  # the PC's day, in one process
    output = _run(shadow_day, "2026-09-24", "day", "test", "2026-09-24T08:00:00+00:00")

    assert output["written"]["written"] and output["summary"]["selected"]["primary"] == 60
    assert len(output["steps"]["chunks"]) == 2  # 160 securities: 150, then 10
    prefix = f"surge/phase-b-shadow/{day.OFFICIAL.cohort_id}/2026-09-24"
    commit = read_commit(blob, prefix, DAY_COMMIT)
    assert (commit["status"], commit["system"], commit["official"]) == ("built", "vercel-shadow", False)
    assert "not called" in commit["jev"] and verify_committed(blob, prefix, commit) == []
    for artifact, pc_file in (("manifest.json", "manifest.json"), ("sessions.json", "sessions.json"),
                              ("inputs/universe.json.gz", "inputs/universe.json"),
                              ("candidates.jsonl.gz", "candidates.jsonl"), ("population.jsonl", "population.jsonl"),
                              ("requests.jsonl", "requests.jsonl")):
        assert read_verified(blob, prefix, commit, artifact) == (pc / pc_file).read_bytes(), artifact
    screening = read_verified(blob, prefix, commit, "screening.jsonl.gz")
    assert screening == jsonl_bytes(_parquet_rows(pc / "screening.parquet"))
    run = json.loads(read_verified(blob, prefix, commit, "run.json"))
    assert [c["attempted"] for c in run["shadow"]["steps"]["chunks"]] == [150, 10]
    assert set(run["stages"]) == {"plan", "build"}
    base = f"surge/phase-b-shadow/{day.OFFICIAL.cohort_id}"
    assert json.loads(client.objects[f"{base}/cohort.json"])["frozen_fingerprint"] == day.OFFICIAL.frozen_fingerprint
    record = json.loads(client.objects[f"{base}/runs/2026-09-24/prediction-1.json"])
    assert (record["status"], record["system"], record["s0"]) == ("built", "vercel-shadow", "2026-09-24")
    assert not any("/requests/" in key for key in client.objects)  # request bodies are never stored


def test_stage3_a_probe_reads_and_screens_and_writes_nothing(local_world, pc_cohort, monkeypatch):
    from shadow_service.flows import shadow_day

    from test_shadow import FakeBlobClient

    client = FakeBlobClient()
    _stage3_fakes(monkeypatch, pc_cohort, client)
    output = _run(shadow_day, "2026-09-24", "probe", "test", "2026-09-24T08:00:00+00:00")
    summary = output["summary"]
    assert summary["issues"] == summary["histories_read"] == 160 and summary["s0_is_session"]
    assert summary["passing"] > 0 and len(output["steps"]["chunks"]) == 2
    assert client.objects == {}


def test_stage3_a_universe_not_read_almost_whole_stops_the_day_as_the_pc_stops_it(local_world, pc_cohort,
                                                                                  monkeypatch):
    from shadow_service.flows import shadow_day

    import test_evaluation_phase_b as pb
    from surge.shadow import day
    from surge.shadow.artifacts import DAY_COMMIT, read_commit, read_verified
    from test_shadow import FakeBlobClient

    failing = {f"{3000 + i}.T" for i in range(9)}  # 151 of 160: 94.4%
    client = FakeBlobClient()
    blob = _stage3_fakes(monkeypatch, pc_cohort, client, yahoo=lambda: pb._FlakyYahoo(pc_cohort.charts, failing))
    output = _run(shadow_day, "2026-09-24", "day", "test", "2026-09-24T08:00:00+00:00")
    assert output["refused"]["kind"] == "coverage"
    prefix = f"surge/phase-b-shadow/{day.OFFICIAL.cohort_id}/2026-09-24"
    commit = read_commit(blob, prefix, DAY_COMMIT)
    assert commit["status"] == "stopped" and set(commit["artifacts"]) == {"run.json"}
    stopped = json.loads(read_verified(blob, prefix, commit, "run.json"))["day_stopped"]
    assert "below 95%" in stopped[0]["reason"] and stopped[0]["detail"]["kind"] == "coverage"


def test_stage3_a_day_started_just_before_its_window_waits_for_it(tmp_path):
    # In a process of its own (tests/shadow_wait_child.py): the local world keeps process-wide state, and a second
    # event loop in one process does not get a workflow's sleep back. On Vercel every invocation is its own process.
    import subprocess
    from datetime import datetime

    child = Path(__file__).with_name("shadow_wait_child.py")
    done = subprocess.run([sys.executable, str(child), str(tmp_path)], capture_output=True, text=True, timeout=300,
                          check=False)
    assert done.returncode == 0, done.stderr[-3000:]
    result = json.loads(done.stdout.strip().splitlines()[-1])
    assert datetime.fromisoformat(result["opened_now"]) >= datetime.fromisoformat(result["opens"])
    assert result["written"] and result["elapsed"] >= 2.5  # it slept until the window opened, then ran the day


def test_the_c_extensions_a_day_needs_load_inside_a_step(tmp_path):
    # In a process of its own (tests/shadow_extensions_child.py), which imports only shadow_service.flows: from
    # the SDK's first run on, a C extension loaded for the first time fails inside a step (every Yahoo read of the
    # first cloud probe, 2026-09-19), so flows.py loads curl_cffi and tiktoken on the host and shares them.
    import subprocess

    child = Path(__file__).with_name("shadow_extensions_child.py")
    done = subprocess.run([sys.executable, str(child), str(tmp_path)], capture_output=True, text=True, timeout=300,
                          check=False)
    assert done.returncode == 0, done.stderr[-3000:]
    outputs = json.loads(done.stdout.strip().splitlines()[-1])
    assert [o["ok"] for o in outputs] == [True, True] and all(o["encodings"] > 0 for o in outputs)
