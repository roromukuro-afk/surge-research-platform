"""Object stores. Write-once by construction, purgeable by a separate principal."""

from surge.storage.base import (
    ImmutableObjectConflict,
    ObjectStore,
    ObjectStoreError,
    StoredObject,
    content_addressed_key,
    sha256_hex,
)
from surge.storage.local import LocalObjectStore

__all__ = [
    "ImmutableObjectConflict",
    "LocalObjectStore",
    "ObjectStore",
    "ObjectStoreError",
    "StoredObject",
    "content_addressed_key",
    "open_store",
    "sha256_hex",
]


def open_store(target: str | None = None, *, env: dict[str, str] | None = None) -> ObjectStore:
    """Open the store named by ``target`` or by SURGE_OBJECT_STORE.

    ``local:<path>`` for development and tests, ``r2`` for the real thing, and
    ``vercel-blob`` for the Phase B web shadow's private store (D-279). A cloud
    store is never the default: a misconfigured job should fail to find a store
    rather than write licensed data somewhere nobody expected.
    """

    import os  # noqa: PLC0415 - keep the module import-light

    source = env if env is not None else dict(os.environ)
    target = target or source.get("SURGE_OBJECT_STORE") or ""

    if target.startswith("local:"):
        return LocalObjectStore(target.removeprefix("local:"))
    if target == "r2":
        from surge.storage.r2 import R2ObjectStore  # noqa: PLC0415 - optional dependency

        return R2ObjectStore.from_env(source)
    if target == "vercel-blob":
        from surge.storage.vercel_blob import VercelBlobObjectStore  # noqa: PLC0415 - optional dependency

        return VercelBlobObjectStore.from_env(source)
    raise ObjectStoreError(
        "no object store selected; set SURGE_OBJECT_STORE to 'r2', 'vercel-blob' or 'local:<path>'"
    )
