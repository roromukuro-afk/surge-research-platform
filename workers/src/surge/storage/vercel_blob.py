"""Vercel Blob as a write-once object store (the Phase B web shadow, D-279).

Every write is a PUT with the store's own overwrite refused (``overwrite=False``
reaches Vercel as ``x-allow-overwrite: 0``) and no random suffix, so a key names
exactly one object for good. When the store refuses a PUT - the key is taken, or
an earlier attempt of this same PUT landed and its answer was lost - the object
is read back and compared by SHA-256: the same bytes are a re-run, different
bytes are a conflict, and neither ever becomes an overwrite.

The store is private: nothing in it can be read without the token. The token is
``BLOB_READ_WRITE_TOKEN`` from the environment (Vercel sets it when a store is
connected to the project) and comes from nowhere else; it is never logged,
passed on a command line, or written to an artifact.

Blob keeps no per-object metadata, so a digest is only ever learned by reading
the bytes. ``head`` therefore costs a read, and the jobs avoid it: they know
what they wrote.

Operations are counted as they are made. On Hobby a team gets 2,000 advanced
operations (put, copy, list) and 10,000 simple ones (reads that miss the cache,
head) a month, shared by every project in the team, and a team over the limit
loses Blob for 30 days. Each run records what it spent (``BlobOperations``), so
the budget can be enforced before a run rather than discovered after one.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass

from surge.storage.base import (
    ImmutableObjectConflict,
    ObjectStore,
    ObjectStoreError,
    StoredObject,
    sha256_hex,
)

ACCESS = "private"
TOKEN_ENV = "BLOB_READ_WRITE_TOKEN"


@dataclass
class BlobOperations:
    """What one store instance has spent, by Vercel's billing classes."""

    advanced: int = 0  # put, copy, list
    simple: int = 0  # head, and a read that misses the cache (counted as if every read missed)
    deletes: int = 0  # free on every plan
    bytes_written: int = 0
    bytes_read: int = 0

    def as_dict(self) -> dict:
        return {"advanced": self.advanced, "simple": self.simple, "deletes": self.deletes,
                "bytes_written": self.bytes_written, "bytes_read": self.bytes_read}


def _is_not_found(exc: Exception) -> bool:
    if type(exc).__name__ == "BlobNotFoundError":
        return True
    response = getattr(exc, "response", None)
    return getattr(response, "status_code", None) == 404


def _is_blob_error(exc: Exception) -> bool:
    """An answer from the Blob API, as opposed to a bug on our side."""

    return any(cls.__name__ == "BlobError" for cls in type(exc).__mro__)


class VercelBlobObjectStore(ObjectStore):
    """``ObjectStore`` over a private Vercel Blob store.

    ``client`` is anything with the methods of ``vercel.blob.BlobClient`` this
    class calls (``put``, ``get``, ``head``, ``iter_objects``, ``delete``); the
    tests pass a fake that keeps Blob's semantics.
    """

    def __init__(self, client, *, store_id: str = "vercel-blob") -> None:
        self._client = client
        self.store_id = store_id
        self.operations = BlobOperations()

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> VercelBlobObjectStore:
        source = env if env is not None else dict(os.environ)
        token = source.get(TOKEN_ENV)
        if not token:
            # Name the variable, never a value.
            raise ObjectStoreError(f"Vercel Blob is not configured; set {TOKEN_ENV} in the job's environment")
        try:
            from vercel.blob import BlobClient  # noqa: PLC0415 - optional dependency
        except ImportError as exc:  # pragma: no cover - depends on the install extra
            raise ObjectStoreError("the 'vercel' package is required for the Blob store; install the "
                                   "'vercel-blob' extra") from exc
        return cls(BlobClient(token=token))

    @staticmethod
    def _check_key(key: str) -> None:
        if not key or key.startswith("/") or ".." in key.split("/") or "\\" in key:
            raise ValueError(f"refusing a key outside the store's namespace: {key!r}")

    # ------------------------------------------------------------------ write
    def put_immutable(self, key: str, data: bytes, content_type: str) -> StoredObject:
        self._check_key(key)
        digest = sha256_hex(data)
        self.operations.advanced += 1
        try:
            self._client.put(key, data, access=ACCESS, content_type=content_type, add_random_suffix=False,
                             overwrite=False)
        except Exception as exc:  # noqa: BLE001 - the SDK's errors, at the boundary
            if not _is_blob_error(exc):
                raise
            existing = self._read(key)
            if existing is None:
                raise ObjectStoreError(f"{key} could not be written and is not there: {exc}") from exc
            if sha256_hex(existing) != digest:
                raise ImmutableObjectConflict(
                    f"{key} already exists with different content; write a new key instead of overwriting"
                ) from exc
            return StoredObject(key, self.store_id, digest, len(existing), content_type, created=False)
        self.operations.bytes_written += len(data)
        return StoredObject(key, self.store_id, digest, len(data), content_type, created=True)

    # ------------------------------------------------------------------- read
    def _read(self, key: str) -> bytes | None:
        self.operations.simple += 1
        try:
            # Past the CDN: after a refused PUT, what counts is what the store holds.
            result = self._client.get(key, access=ACCESS, use_cache=False)
        except Exception as exc:  # noqa: BLE001 - see put_immutable
            if _is_not_found(exc):
                return None
            raise
        content = bytes(result.content)
        self.operations.bytes_read += len(content)
        return content

    def get(self, key: str) -> bytes:
        self._check_key(key)
        content = self._read(key)
        if content is None:
            raise ObjectStoreError(f"{key} is not in the store")
        return content

    def head(self, key: str) -> StoredObject | None:
        self._check_key(key)
        content = self._read(key)
        if content is None:
            return None
        return StoredObject(key, self.store_id, sha256_hex(content), len(content), "application/octet-stream",
                            created=False)

    def exists(self, key: str) -> bool:
        """Whether the key is taken, for one Blob head (a simple operation) and no download."""

        self._check_key(key)
        self.operations.simple += 1
        try:
            self._client.head(key)
        except Exception as exc:  # noqa: BLE001 - see put_immutable
            if _is_not_found(exc):
                return False
            raise
        return True

    def list(self, prefix: str) -> Iterator[str]:
        """Every key under ``prefix``. Each page is an advanced operation: for tools, not for jobs or pages."""

        self.operations.advanced += 1
        for item in self._client.iter_objects(prefix=prefix):
            yield item.pathname

    def delete(self, key: str) -> bool:
        self._check_key(key)
        if not self.exists(key):
            return False
        self.operations.deletes += 1
        self._client.delete(key)
        return True


__all__ = ["ACCESS", "TOKEN_ENV", "BlobOperations", "VercelBlobObjectStore"]
