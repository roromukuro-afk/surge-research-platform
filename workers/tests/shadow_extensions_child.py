"""The shadow's C extensions inside workflow steps, in a process of its own (run by test_shadow_service).

Only ``shadow_service.flows`` is imported here - never curl_cffi or tiktoken themselves - so a step that uses
them works only if that module loads them on the host before the SDK's first run swaps ``sys.modules``.
Prints one JSON line: the two runs' outputs.

    python tests/shadow_extensions_child.py <scratch dir>
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

TESTS = Path(__file__).resolve().parent
REPO = TESTS.parents[1]
for path in (TESTS.parent / "src", REPO):
    sys.path.insert(0, str(path))


def main(scratch: Path) -> list[dict]:
    os.environ["WORKFLOW_TARGET_WORLD"] = "local"
    os.environ["WORKFLOW_LOCAL_DATA_DIR"] = str(scratch / "workflow-data")
    from shadow_service.flows import extension_selftest
    from vercel.workflow import start
    from vercel.workflow._internal.world import get_world

    async def go() -> list[dict]:
        outputs = []
        try:
            for _ in range(2):  # the first run swaps sys.modules; the second is a warm function's
                run = await start(extension_selftest, "7203.T", True)
                outputs.append(await asyncio.wait_for(run.return_value(), 120))
        finally:
            await get_world().aclose()
        return outputs

    return asyncio.run(go())


if __name__ == "__main__":
    print(json.dumps(main(Path(sys.argv[1]))))
