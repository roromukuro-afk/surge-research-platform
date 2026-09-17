"""Object storage that cannot overwrite what it already holds.

Every raw payload is stored under a key derived from the hash of its own bytes:

    raw/<provider>/<dataset>/<sha256>.<ext>

Two consequences follow, and both are the point. Fetching the same payload twice
lands on the same key, so a re-run is free and idempotent rather than a second
copy. And different bytes can never land on an existing key, so a correction is
always a new object next to the old one instead of a silent replacement - which
is what makes "what did we think on the 3rd?" answerable at all.

The stores still refuse overwrites explicitly, because the key convention is a
convention and an audited store should not depend on callers keeping it.
"""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass


class ObjectStoreError(RuntimeError):
    pass


class ImmutableObjectConflict(ObjectStoreError):
    """The key exists and holds different bytes. Never resolved by overwriting."""


@dataclass(frozen=True)
class StoredObject:
    key: str
    store_id: str
    sha256: str
    bytes: int
    content_type: str
    created: bool


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def content_addressed_key(provider_id: str, dataset_key: str, sha256: str, extension: str) -> str:
    if not sha256 or len(sha256) != 64:
        raise ValueError(f"expected a sha256 hex digest, got {sha256!r}")
    extension = extension.lstrip(".")
    return f"raw/{provider_id}/{dataset_key}/{sha256}.{extension}"


class ObjectStore(ABC):
    """The storage interface the ingestion jobs see.

    ``delete`` exists because a licence purge must be able to destroy what it
    stored, and a store that cannot delete cannot honour J-Quants' terms. It is
    called by the purge command and by nothing else; the ingestion worker's
    credentials and its database role are both chosen so that it could not run a
    purge even if its code tried to.
    """

    store_id: str

    @abstractmethod
    def put_immutable(self, key: str, data: bytes, content_type: str) -> StoredObject:
        """Store bytes at a key that does not yet exist.

        Returns ``created=False`` when the key already holds exactly these bytes
        - a re-run, not a conflict. Raises ImmutableObjectConflict when it holds
        different ones.
        """

    @abstractmethod
    def get(self, key: str) -> bytes: ...

    @abstractmethod
    def head(self, key: str) -> StoredObject | None:
        """Metadata for the key, or None when it does not exist."""

    @abstractmethod
    def list(self, prefix: str) -> Iterator[str]: ...

    @abstractmethod
    def delete(self, key: str) -> bool:
        """Remove the object. Returns False when it was already gone.

        A purge treats "already gone" as satisfied, not as failure: the
        obligation is that we do not hold the data, not that we personally
        deleted it.
        """

    def put_content_addressed(
        self, provider_id: str, dataset_key: str, data: bytes, content_type: str, extension: str
    ) -> StoredObject:
        digest = sha256_hex(data)
        key = content_addressed_key(provider_id, dataset_key, digest, extension)
        return self.put_immutable(key, data, content_type)
