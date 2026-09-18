"""The method every request carries: Canonical v5.1 and the addenda, verified against MANIFEST.

Both come from ``docs/prompts/MANIFEST.md``: each file's SHA-256 must match its
row there, and the addenda are ordered as they were registered - older first,
since a newer addendum overrides an older one (CLAUDE.md 1-2). Ordering them by
file name would put ``v5.1-addendum-2026-09-15.md`` after the phase 0.x
addenda that were registered later and amend it. An addendum file that is not
registered is refused, not appended.

Text is decoded from the file's bytes, not read in text mode, so what is sent
hashes to exactly what MANIFEST records.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
MANIFEST = "docs/prompts/MANIFEST.md"
CANONICAL = "docs/prompts/short-surge-v5.1.original.md"
ADDENDA_DIR = "docs/prompts/addenda"
_ROW = re.compile(r"^\| `([^`]+)` \| [^|]* \| `([0-9a-f]{64})` \|")


class MethodError(RuntimeError):
    pass


@dataclass(frozen=True)
class MethodFile:
    path: str
    sha256: str
    text: str


@dataclass(frozen=True)
class Method:
    canonical: MethodFile
    addenda: tuple[MethodFile, ...]

    def manifest_entry(self) -> dict:
        return {
            "canonical": {"path": self.canonical.path, "sha256": self.canonical.sha256},
            "addenda_newer_overrides_older": [{"path": a.path, "sha256": a.sha256} for a in self.addenda],
        }


def _read(root: Path, path: str, expected: str) -> MethodFile:
    data = (root / path).read_bytes()
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected:
        raise MethodError(f"{path} hashes to {actual}, MANIFEST records {expected}")
    return MethodFile(path=path, sha256=actual, text=data.decode("utf-8"))


def load_method(root: Path = REPO_ROOT) -> Method:
    rows = []
    for line in (root / MANIFEST).read_text(encoding="utf-8").splitlines():
        match = _ROW.match(line)
        if match:
            rows.append((match.group(1), match.group(2)))
    registered = dict(rows)
    if CANONICAL not in registered:
        raise MethodError(f"{CANONICAL} is not registered in {MANIFEST}")
    addenda = [path for path, _ in rows if path.startswith(ADDENDA_DIR + "/")]
    present = {f"{ADDENDA_DIR}/{p.name}" for p in (root / ADDENDA_DIR).glob("*.md")}
    unregistered = sorted(present - set(addenda))
    if unregistered:
        raise MethodError(f"addenda not registered in {MANIFEST}: {unregistered}")
    return Method(
        canonical=_read(root, CANONICAL, registered[CANONICAL]),
        addenda=tuple(_read(root, path, registered[path]) for path in addenda),
    )


__all__ = ["Method", "MethodError", "MethodFile", "load_method"]
