"""A filesystem object store, for development and for the tests.

Write-once is enforced by the operating system: O_CREAT|O_EXCL fails if the path
already exists, so two processes racing on the same key cannot both believe they
wrote it. That matters more than it looks - a test that fakes immutability with
``if not path.exists()`` would pass while the real store raced.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from pathlib import Path

from surge.storage.base import (
    ImmutableObjectConflict,
    ObjectStore,
    StoredObject,
    sha256_hex,
)


def _long_path_safe(path: Path) -> Path:
    """Work around Windows' 260 character path limit.

    Object keys are deliberately long: a Hive-style partition path plus a 64
    character content digest is most of the budget before the store root is even
    counted. R2 allows 1024 byte keys and does not care, but the local store
    would fail with a bare FileNotFoundError - which reads like a missing file
    rather than a path that was never openable. The extended-length prefix lifts
    the limit to 32,767 characters.
    """

    if sys.platform != "win32":
        return path
    resolved = path.absolute()
    text = str(resolved)
    if text.startswith("\\\\?\\"):
        return resolved
    return Path("\\\\?\\" + text)


class LocalObjectStore(ObjectStore):
    def __init__(self, root: Path | str, *, store_id: str = "local") -> None:
        self._root = Path(root)
        self.store_id = store_id
        _long_path_safe(self._root).mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        if key.startswith("/") or ".." in key.split("/"):
            raise ValueError(f"refusing a key that escapes the store root: {key!r}")
        return _long_path_safe(self._root / key)

    def put_immutable(self, key: str, data: bytes, content_type: str) -> StoredObject:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        digest = sha256_hex(data)
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0))
        except FileExistsError:
            existing = path.read_bytes()
            if sha256_hex(existing) == digest:
                return StoredObject(key, self.store_id, digest, len(existing), content_type, created=False)
            raise ImmutableObjectConflict(
                f"{key} already exists with different content; write a new key instead of overwriting"
            ) from None
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        return StoredObject(key, self.store_id, digest, len(data), content_type, created=True)

    def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def head(self, key: str) -> StoredObject | None:
        path = self._path(key)
        if not path.exists():
            return None
        data = path.read_bytes()
        return StoredObject(key, self.store_id, sha256_hex(data), len(data), "application/octet-stream", created=False)

    def list(self, prefix: str) -> Iterator[str]:
        base = _long_path_safe(self._root)
        for path in sorted(base.rglob("*")):
            if not path.is_file():
                continue
            key = path.relative_to(base).as_posix()
            if key.startswith(prefix):
                yield key

    def delete(self, key: str) -> bool:
        path = self._path(key)
        if not path.exists():
            return False
        path.unlink()
        return True
