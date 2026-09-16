"""RF-14a / RF-14b: canonical prompt integrity.

RF-14a - every file registered in MANIFEST.md still hashes to its recorded value.
RF-14b - the canonical v5.1 body contains no post-v5.1 addendum material, and
         addenda are separate files with their own hashes.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MANIFEST = REPO_ROOT / "docs" / "prompts" / "MANIFEST.md"
CANONICAL = REPO_ROOT / "docs" / "prompts" / "short-surge-v5.1.original.md"
ADDENDA_DIR = REPO_ROOT / "docs" / "prompts" / "addenda"
VERIFIER = REPO_ROOT / "scripts" / "verify_manifest_hashes.py"

ROW = re.compile(r"^\|\s*`([^`]+)`\s*\|([^|]*)\|\s*`?([0-9a-f]{64})`?\s*\|", re.MULTILINE)

POST_V51_TERMS = (
    "SETUP_EOD",
    "POST_CLOSE_CATALYST_SETUP",
    "entry_reference_price",
    "decision_price",
    "AMBIGUOUS_PATH",
    "PIPELINE_MISSED_ACTIONABLE_SIGNAL",
    "counterfactual_horizon_outcome",
    "initial_failure_line",
    "available_to_model_at",
)


def test_canonical_v51_is_registered_and_matches_its_hash():
    assert CANONICAL.exists(), "canonical v5.1 must be committed before Phase 1 work"
    rows = {path: digest for path, _kind, digest in ROW.findall(MANIFEST.read_text(encoding="utf-8"))}
    relative = "docs/prompts/short-surge-v5.1.original.md"
    assert relative in rows, "canonical v5.1 must be registered in MANIFEST.md"
    assert hashlib.sha256(CANONICAL.read_bytes()).hexdigest() == rows[relative]


def test_canonical_v51_contains_no_post_v51_decisions():
    body = CANONICAL.read_text(encoding="utf-8")
    leaked = [term for term in POST_V51_TERMS if term in body]
    assert not leaked, f"post-v5.1 terms leaked into canonical v5.1: {leaked}"


def test_addenda_are_separate_files():
    addenda = sorted(path.name for path in ADDENDA_DIR.glob("*.md"))
    assert addenda, "post-v5.1 decisions must live in addenda files"
    assert CANONICAL.name not in addenda


def test_verifier_script_passes():
    result = subprocess.run(
        [sys.executable, str(VERIFIER)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
