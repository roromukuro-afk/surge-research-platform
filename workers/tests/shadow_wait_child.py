"""A shadow day started 1.5 s before its window, on the SDK's local world, in a process of its own.

Run by test_shadow_service. The local world keeps process-wide state, and a
second event loop in one process does not get its delayed deliveries (a
workflow's sleep); on Vercel every invocation is a process of its own anyway.
Prints one JSON line.

    python tests/shadow_wait_child.py <scratch dir>
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from datetime import timedelta
from pathlib import Path

TESTS = Path(__file__).resolve().parent
REPO = TESTS.parents[1]
for path in (TESTS.parent / "src", TESTS, REPO):
    sys.path.insert(0, str(path))


def main(scratch: Path) -> dict:
    os.environ["WORKFLOW_TARGET_WORLD"] = "local"
    os.environ["WORKFLOW_LOCAL_DATA_DIR"] = str(scratch / "workflow-data")
    import pytest
    from shadow_service.flows import shadow_day
    from vercel.workflow import start
    from vercel.workflow._internal.world import get_world

    import test_evaluation_phase_b as pb
    from surge.evaluation import phase_b
    from surge.shadow import day
    from surge.storage import vercel_blob
    from test_evaluation import _FakeYahoo
    from test_shadow import FakeBlobClient

    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(pb, "COHORT", day.OFFICIAL.cohort_id)
        env = pb._setup(scratch / "pc", monkeypatch, n=40, seed=day.OFFICIAL.experiment_seed)
        client = FakeBlobClient()
        monkeypatch.setattr(vercel_blob.VercelBlobObjectStore, "from_env",
                            classmethod(lambda cls, env=None: cls(client)))
        opens, began = phase_b.close_confirmed_at(pb.S0), time.monotonic()
        fakes = {"issues": lambda: (env.issues, "f" * 64), "yahoo": lambda: _FakeYahoo(env.charts),
                 "yanoshin": pb._FakeYanoshinRecent, "sleep": lambda _s: None, "monotonic": time.monotonic,
                 "now": lambda: opens - timedelta(seconds=1.5) + timedelta(seconds=time.monotonic() - began)}
        monkeypatch.setattr(day, "providers", lambda: fakes)

        async def go():
            run = await start(shadow_day, pb.S0.isoformat(), "day", "test", "2026-09-24T07:00:00+00:00")
            try:
                return await asyncio.wait_for(run.return_value(), 120)
            finally:
                await get_world().aclose()

        output = asyncio.run(go())
        return {"opens": opens.isoformat(), "opened_now": output["opened"]["now"],
                "written": output["written"]["written"], "elapsed": round(time.monotonic() - began, 3)}
    finally:
        monkeypatch.undo()


if __name__ == "__main__":
    print(json.dumps(main(Path(sys.argv[1]))))
