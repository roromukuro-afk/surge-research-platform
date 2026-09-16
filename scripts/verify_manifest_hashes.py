#!/usr/bin/env python3
"""Verify that every file registered in docs/prompts/MANIFEST.md still matches its hash.

Canonical sources (v5.1, the implementation instructions, the audit originals)
must never drift. This is the executable form of that rule (RF-14a), and it also
checks that the canonical v5.1 body does not contain post-v5.1 addendum material
(RF-14b).
"""

from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST = REPO_ROOT / "docs" / "prompts" / "MANIFEST.md"

ROW = re.compile(r"^\|\s*`([^`]+)`\s*\|([^|]*)\|\s*`?([0-9a-f]{64})`?\s*\|", re.MULTILINE)

# Terms that only exist in post-v5.1 decisions. None of them may appear inside
# the canonical v5.1 file itself.
POST_V51_TERMS = (
    "SETUP_EOD",
    "POST_CLOSE_CATALYST_SETUP",
    "entry_reference_price",
    "decision_price",
    "AMBIGUOUS_PATH",
    "UNRESOLVED_MISSING_DATA",
    "PIPELINE_MISSED_ACTIONABLE_SIGNAL",
    "counterfactual_horizon_outcome",
    "initial_failure_line",
    "available_to_model_at",
    "universe-1.0.0",
)

CANONICAL_V51 = REPO_ROOT / "docs" / "prompts" / "short-surge-v5.1.original.md"


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    if not MANIFEST.exists():
        print(f"MANIFEST not found: {MANIFEST}", file=sys.stderr)
        return 1

    manifest_text = MANIFEST.read_text(encoding="utf-8")
    rows = ROW.findall(manifest_text)
    if not rows:
        print("MANIFEST has no registered hashes", file=sys.stderr)
        return 1

    failures: list[str] = []
    checked = 0

    for relative_path, _kind, expected in rows:
        path = REPO_ROOT / relative_path
        if not path.exists():
            failures.append(f"missing file registered in MANIFEST: {relative_path}")
            continue
        actual = sha256_of(path)
        checked += 1
        if actual != expected:
            failures.append(f"hash mismatch: {relative_path}\n  expected {expected}\n  actual   {actual}")

    if CANONICAL_V51.exists():
        body = CANONICAL_V51.read_text(encoding="utf-8")
        leaked = [term for term in POST_V51_TERMS if term in body]
        if leaked:
            failures.append(f"post-v5.1 terms found inside canonical v5.1: {', '.join(leaked)}")
    else:
        failures.append(f"canonical v5.1 missing: {CANONICAL_V51.relative_to(REPO_ROOT)}")

    if failures:
        for failure in failures:
            print(f"FAIL {failure}", file=sys.stderr)
        return 1

    print(f"OK: {checked} registered file(s) match their recorded SHA-256; canonical v5.1 is unmixed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
