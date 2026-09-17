"""The vocabulary of Phase 4, and the one thing it exists to get right: time.

A news item carries four timestamps and they are not interchangeable:

``source_published_at``
    When the source says it published the item. A fact about the world.
``system_first_seen_at``
    When this system first observed that the item existed.
``ingested_at``
    When this system stored the item's content.
``available_to_model_at``
    When a model is permitted to use it. **The only one that gates a decision.**

The failure this module is built to prevent is using the first as the fourth.
Backfilling a year of press releases and stamping each with its publication time
produces a backtest that knew the news the moment it broke, which no live system
could have done. So ``available_to_model_at`` is never derived from
``source_published_at``; it is derived from when we actually had the thing.

A replay experiment that legitimately wants to ask "what if the collector had
been running?" states that assumption in ``replay_assumed_available_at`` with a
written note, where it is visible and cannot be mistaken for an observation.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum


class SourceKind(StrEnum):
    OFFICIAL_DISCLOSURE = "OFFICIAL_DISCLOSURE"
    REGULATOR = "REGULATOR"
    EXCHANGE = "EXCHANGE"
    CENTRAL_BANK = "CENTRAL_BANK"
    GOVERNMENT = "GOVERNMENT"
    COMPANY_IR = "COMPANY_IR"
    VENDOR_WIRE = "VENDOR_WIRE"
    NEWS_MEDIA = "NEWS_MEDIA"


class AccessMechanism(StrEnum):
    REST_API = "REST_API"
    RSS = "RSS"
    ATOM = "ATOM"
    BULK_FILE = "BULK_FILE"
    HTML_PAGE = "HTML_PAGE"
    EMAIL = "EMAIL"


class AuthRequirement(StrEnum):
    NONE = "NONE"
    FREE_API_KEY = "FREE_API_KEY"
    PAID_API_KEY = "PAID_API_KEY"
    ACCOUNT_REQUIRED = "ACCOUNT_REQUIRED"
    UNKNOWN = "UNKNOWN"


class DocumentType(StrEnum):
    TIMELY_DISCLOSURE = "TIMELY_DISCLOSURE"
    STATUTORY_FILING = "STATUTORY_FILING"
    PRESS_RELEASE = "PRESS_RELEASE"
    POLICY_STATEMENT = "POLICY_STATEMENT"
    STATISTIC_RELEASE = "STATISTIC_RELEASE"
    IR_RELEASE = "IR_RELEASE"
    NEWS_ARTICLE = "NEWS_ARTICLE"
    OTHER = "OTHER"


class TimePrecision(StrEnum):
    """How much of a publication timestamp we actually know.

    Many official sources publish a date with no time of day. Recording that as
    ``DATE_ONLY`` stops a later reader treating midnight as a real minute - which
    would put a morning release nine hours before it happened.
    """

    EXACT = "EXACT"
    DATE_ONLY = "DATE_ONLY"
    INFERRED = "INFERRED"
    UNKNOWN = "UNKNOWN"


class BodyStorage(StrEnum):
    FULL_TEXT = "FULL_TEXT"
    METADATA_ONLY = "METADATA_ONLY"
    EXCERPT = "EXCERPT"
    NOT_STORED = "NOT_STORED"


class AvailabilityBasis(StrEnum):
    """Mirrors ``market.availability_basis``; see the Phase 2 migration."""

    OBSERVED_NOW = "OBSERVED_NOW"
    PROVIDER_PUBLISHED_TIMESTAMP = "PROVIDER_PUBLISHED_TIMESTAMP"
    DOCUMENTED_SCHEDULE = "DOCUMENTED_SCHEDULE"
    HISTORICAL_REPLAY_ASSUMPTION = "HISTORICAL_REPLAY_ASSUMPTION"


class TimeError(ValueError):
    """Raised when a set of timestamps would misrepresent what we knew."""


@dataclass(frozen=True)
class KnowledgeTimes:
    """The four timestamps, validated against each other.

    Build these with :meth:`observed` rather than by hand. The constructor is
    permissive enough to round-trip a database row; the classmethod is where the
    rules live.
    """

    system_first_seen_at: datetime
    ingested_at: datetime
    available_to_model_at: datetime
    availability_basis: AvailabilityBasis = AvailabilityBasis.OBSERVED_NOW
    source_published_at: datetime | None = None
    source_published_precision: TimePrecision = TimePrecision.UNKNOWN
    replay_assumed_available_at: datetime | None = None
    replay_assumption_note: str | None = None

    def __post_init__(self) -> None:
        for name in ("system_first_seen_at", "ingested_at", "available_to_model_at"):
            value = getattr(self, name)
            if value.tzinfo is None:
                raise TimeError(f"{name} must be timezone-aware; a naive timestamp has no knowledge time")
        if self.source_published_at is not None and self.source_published_at.tzinfo is None:
            raise TimeError("source_published_at must be timezone-aware")

        if self.system_first_seen_at > self.ingested_at:
            raise TimeError(
                f"system_first_seen_at ({self.system_first_seen_at.isoformat()}) is after ingested_at "
                f"({self.ingested_at.isoformat()}); we cannot store something before noticing it"
            )
        if self.ingested_at > self.available_to_model_at:
            raise TimeError(
                f"ingested_at ({self.ingested_at.isoformat()}) is after available_to_model_at "
                f"({self.available_to_model_at.isoformat()}); the model would be reading data we had not stored"
            )
        if self.replay_assumed_available_at is not None and not self.replay_assumption_note:
            raise TimeError(
                "replay_assumed_available_at without a note. A counterfactual availability time is only "
                "honest if the assumption behind it is written down"
            )
        if self.source_published_at is None and self.source_published_precision is TimePrecision.EXACT:
            raise TimeError("source_published_precision=EXACT but there is no source_published_at")

    @classmethod
    def observed(
        cls,
        *,
        first_seen_at: datetime,
        ingested_at: datetime,
        source_published_at: datetime | None = None,
        source_published_precision: TimePrecision = TimePrecision.UNKNOWN,
    ) -> KnowledgeTimes:
        """The normal path: we found it, so we know it from now on.

        ``available_to_model_at`` is ``ingested_at`` - deliberately, and even when
        ``source_published_at`` is much earlier. A document fetched today was not
        knowable yesterday, whatever its own timestamp says, and this is the one
        place where that could quietly stop being true.
        """

        return cls(
            system_first_seen_at=first_seen_at,
            ingested_at=ingested_at,
            available_to_model_at=ingested_at,
            availability_basis=AvailabilityBasis.OBSERVED_NOW,
            source_published_at=source_published_at,
            source_published_precision=source_published_precision,
        )

    def with_replay_assumption(self, *, assumed_at: datetime, note: str) -> KnowledgeTimes:
        """Attach a research-only counterfactual availability time.

        Production never reads it: ``news.documents_as_of()`` does not return the
        column. This exists so a replay can say "assume a 15-minute collection lag
        from publication" out loud instead of editing the observed time.
        """

        if not note.strip():
            raise TimeError("a replay assumption needs a note explaining it")
        return replace(self, replay_assumed_available_at=assumed_at, replay_assumption_note=note)

    @property
    def published_after_we_saw_it(self) -> bool:
        """Clock skew or a timezone bug at the source, worth flagging not fixing."""

        return self.source_published_at is not None and self.source_published_at > self.system_first_seen_at


@dataclass(frozen=True)
class FeedItem:
    """One entry as the source presented it, before any of our decisions."""

    source_document_id: str
    title: str | None
    url: str | None
    published_at: datetime | None
    published_precision: TimePrecision
    summary: str | None = None
    language: str | None = None
    raw_fields: dict[str, str] | None = None


@dataclass(frozen=True)
class CollectedDocument:
    """A feed item plus everything we decided about it: time, licence, content."""

    source_key: str
    source_document_id: str
    document_type: DocumentType
    times: KnowledgeTimes
    content_sha256: str
    body_storage: BodyStorage
    title: str | None = None
    document_url: str | None = None
    language: str | None = None
    body_text: str | None = None
    body_storage_reason: str | None = None
    byte_size: int | None = None
    raw_object_key: str | None = None
    revision_seq: int = 1
    supersedes_document_id: str | None = None
    quality_warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        has_body = self.body_storage in (BodyStorage.FULL_TEXT, BodyStorage.EXCERPT)
        if has_body and self.body_text is None:
            raise ValueError(f"body_storage={self.body_storage.value} but no body_text for {self.source_document_id}")
        if not has_body and self.body_text is not None:
            raise ValueError(
                f"body_storage={self.body_storage.value} but body_text is present for {self.source_document_id}; "
                "a row that says it kept no text must not be carrying text"
            )


def utcnow() -> datetime:
    return datetime.now(UTC)
