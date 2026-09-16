"""Shared value objects for provider output and universe decisions."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

MARKET_JP = "JP"
MARKET_US = "US"

# Security types must match the ref.security_type enum in the database.
SECURITY_TYPES = frozenset(
    {
        "COMMON_STOCK",
        "FOREIGN_COMMON_STOCK",
        "ADR",
        "PREFERRED",
        "ETF",
        "ETN",
        "REIT_OR_FUND",
        "WARRANT",
        "RIGHT",
        "UNIT",
        "INVESTMENT_CERTIFICATE",
        "OTHER",
        "UNKNOWN",
    }
)


@dataclass(frozen=True)
class Provenance:
    """Where a piece of data came from and when it became usable."""

    source_id: str
    endpoint: str
    requested_at: datetime
    received_at: datetime
    http_status: int
    bytes: int
    content_sha256: str
    item_count: int
    observed_at: datetime
    available_at: datetime
    source_published_at: datetime | None = None


@dataclass(frozen=True)
class RawSecurityRecord:
    """One security as reported by a provider, normalised but not yet judged."""

    source_id: str
    source_record_id: str
    market_code: str
    exchange_id: str | None
    local_code: str
    name: str
    security_type: str
    symbol: str | None = None
    market_segment_code: str | None = None
    market_segment_name: str | None = None
    currency: str = ""
    country: str = ""
    is_adr: bool = False
    is_test_issue: bool = False
    listing_status: str = "LISTED"
    cik: str | None = None
    edinet_code: str | None = None
    corporate_number: str | None = None
    # The issuing entity's own registry name (SEC registrant / EDINET 提出者名).
    # `name` above is the provider's security display name and must never be
    # used as the issuer's legal name.
    issuer_name: str | None = None
    issuer_name_source: str | None = None
    type_evidence: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.security_type not in SECURITY_TYPES:
            raise ValueError(f"unknown security_type: {self.security_type}")


@dataclass(frozen=True)
class FetchResult:
    records: tuple[RawSecurityRecord, ...]
    provenance: Provenance
    errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class RunError:
    """One diagnostic from a run, typed so coverage can separate the kinds.

    A provider that failed and a classification warning are not the same thing,
    and neither may be reported as "the provider returned N errors".
    """

    error_type: str  # PROVIDER_DATA | DATA_QUALITY | IDENTITY_COLLISION
    message: str
    context: dict[str, Any] = field(default_factory=dict)
    stage: str = "provider_fetch"
    severity: str = "WARNING"


# Error types the database and the coverage function agree on.
ERROR_PROVIDER_DATA = "PROVIDER_DATA"
ERROR_DATA_QUALITY = "DATA_QUALITY"
ERROR_IDENTITY_COLLISION = "IDENTITY_COLLISION"


@dataclass(frozen=True)
class ProviderCapabilities:
    provider_id: str
    markets: tuple[str, ...]
    roles: tuple[str, ...]
    update_schedule: str
    latency_class: str
    license_scope: str
    provides_delisted: bool = False


@dataclass(frozen=True)
class UniverseDecision:
    decision: str  # INCLUDED | EXCLUDED | UNRESOLVED
    reason_code: str
    detail: dict[str, Any] = field(default_factory=dict)
