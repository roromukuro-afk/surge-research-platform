"""The run directory: write-once files, each with its SHA-256.

``<root>/evaluation/jev/<run_id>/``. The root is outside the repository
(``SURGE_EVALUATION_ROOT``, default ``~/.surge``): the files hold market data
and model answers about real securities, which are never committed.

Immutable means write-once. A file that exists is never rewritten; a stage that
would change a result writes a new run instead. Every write returns the file's
SHA-256, and a stage records the hashes of what it wrote, so a report can show
exactly which inputs it was computed from.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

ENV_ROOT = "SURGE_EVALUATION_ROOT"


class StoreError(RuntimeError):
    pass


def default_root() -> Path:
    return Path(os.environ.get(ENV_ROOT) or Path.home() / ".surge")


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@dataclass(frozen=True)
class RunStore:
    root: Path
    run_id: str

    @property
    def path(self) -> Path:
        return self.root / "evaluation" / "jev" / self.run_id

    def _target(self, name: str) -> Path:
        target = self.path / name
        if target.exists():
            raise StoreError(f"{target} already exists; evaluation files are write-once")
        target.parent.mkdir(parents=True, exist_ok=True)
        return target

    def write_bytes(self, name: str, data: bytes) -> str:
        target = self._target(name)
        temporary = target.with_suffix(target.suffix + ".partial")
        temporary.write_bytes(data)
        temporary.replace(target)
        return hashlib.sha256(data).hexdigest()

    def write_json(self, name: str, payload) -> str:
        return self.write_bytes(name, (json.dumps(payload, indent=2, ensure_ascii=False, default=str) + "\n").encode())

    def write_jsonl(self, name: str, rows: Iterable[dict]) -> str:
        lines = "".join(json.dumps(row, ensure_ascii=False, default=str) + "\n" for row in rows)
        return self.write_bytes(name, lines.encode())

    def write_parquet(self, name: str, rows: list[dict]) -> str:
        import io

        import pyarrow as pa
        import pyarrow.parquet as pq

        flat = [{k: (json.dumps(v, ensure_ascii=False, default=str) if isinstance(v, dict | list) else v)
                 for k, v in row.items()} for row in rows]
        buffer = io.BytesIO()
        pq.write_table(pa.Table.from_pylist(flat), buffer)
        return self.write_bytes(name, buffer.getvalue())

    def read_json(self, name: str):
        return json.loads((self.path / name).read_text(encoding="utf-8"))

    def read_jsonl(self, name: str) -> list[dict]:
        return [json.loads(line) for line in (self.path / name).read_text(encoding="utf-8").splitlines() if line]

    def exists(self, name: str) -> bool:
        return (self.path / name).exists()

    def verify(self, name: str, expected_sha256: str) -> None:
        actual = sha256_file(self.path / name)
        if actual != expected_sha256:
            raise StoreError(f"{name} does not match the hash recorded for it ({actual} != {expected_sha256})")


__all__ = ["ENV_ROOT", "RunStore", "StoreError", "default_root", "sha256_file"]
