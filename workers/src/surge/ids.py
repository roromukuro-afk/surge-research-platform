"""Deterministic surrogate keys.

Mirrors ``ref.deterministic_uuid`` in SQL so the worker and the database agree
on identity without a round trip. Re-running an ingestion therefore updates the
same rows instead of creating duplicates.
"""

from __future__ import annotations

import hashlib
import uuid

_NAMESPACE = "surge-research-platform"


def deterministic_uuid(key: str) -> uuid.UUID:
    digest = hashlib.md5(f"{_NAMESPACE}|{key}".encode()).hexdigest()
    return uuid.UUID(digest)


def issuer_key(country: str, normalized_name: str) -> str:
    return f"issuer|{country}|{normalized_name}"


def security_key(market_code: str, exchange_id: str | None, local_code: str) -> str:
    return f"security|{market_code}|{exchange_id or 'NA'}|{local_code}"


def listing_key(exchange_id: str, local_code: str) -> str:
    return f"listing|{exchange_id}|{local_code}"
