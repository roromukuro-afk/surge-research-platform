"""The collection loop: licence first, then time, then content.

A collector for one source has to answer three questions per item, in this order:

1. **May we keep this at all, and how much of it?** Decided from the source's
   licence, not from what is convenient. A source that does not permit keeping
   the text gets metadata only, and the row says so.
2. **When did we know it?** See :mod:`surge.news.models`. Never the publication
   time.
3. **Is this new, a repeat, or an amendment?** Amendments become new revisions;
   they never overwrite.

Counting is part of the job rather than an afterthought. A day with no items and
a day where every fetch failed both produce zero documents, and the difference
between them is the difference between a quiet market and a broken pipeline.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from surge.licensing import LicenseMode, LicenseViolation, Obligation, Permission
from surge.news.models import (
    AccessMechanism,
    AuthRequirement,
    BodyStorage,
    CollectedDocument,
    DocumentType,
    FeedItem,
    KnowledgeTimes,
    SourceKind,
    TimePrecision,
    utcnow,
)

COLLECTOR_VERSION = "news-collector-1.0.0"


@dataclass(frozen=True)
class SourceSpec:
    """Static description of a source. Mirrors a ``news.sources`` row."""

    source_key: str
    name: str
    scope: str
    source_kind: SourceKind
    access_mechanism: AccessMechanism
    official_url: str
    feed_url: str | None = None
    docs_url: str | None = None
    auth_requirement: AuthRequirement = AuthRequirement.NONE
    required_env: tuple[str, ...] = ()
    documented_rate_limit: str | None = None
    min_request_interval_seconds: float = 1.0
    # Whether this source may be used to find out that something happened, to
    # confirm something another source found, or both. These are capabilities,
    # not a ranking: the project forbids a fixed "IR beats news" ordering, so
    # material strength is measured per event in Phase 5 and never inferred here.
    discovery_role: bool = True
    verification_role: bool = False
    fetch_priority: int = 100
    default_document_type: DocumentType = DocumentType.PRESS_RELEASE
    notes: str | None = None


@dataclass(frozen=True)
class SourcePolicy:
    """What a source permits. Mirrors a ``news.source_policies`` row."""

    source_key: str
    policy_version: str
    license_mode: LicenseMode
    full_text_storage_allowed: Permission = Permission.UNKNOWN
    metadata_storage_allowed: Permission = Permission.UNKNOWN
    derived_output_sharing_allowed: Permission = Permission.UNKNOWN
    raw_redistribution_allowed: Permission = Permission.UNKNOWN
    commercial_use_allowed: Permission = Permission.UNKNOWN
    attribution_required: Obligation = Obligation.UNKNOWN
    delete_on_cancel: Obligation = Obligation.UNKNOWN
    robots_allows_path: Permission = Permission.UNKNOWN
    crawl_delay_seconds: float | None = None
    deciding_clause: str | None = None
    terms_url: str | None = None

    def assert_may_collect(self) -> None:
        """Raise unless we may fetch and keep at least the metadata.

        Fetching a page we may not keep anything from is not research, it is
        traffic. Better to refuse loudly at the top of the loop than to discover
        it per item.
        """

        if self.robots_allows_path is Permission.PROHIBITED:
            raise LicenseViolation(
                f"{self.source_key}: robots.txt disallows the paths we would fetch ({self.terms_url})"
            )
        if self.metadata_storage_allowed is not Permission.ALLOWED:
            raise LicenseViolation(
                f"{self.source_key} does not permit storing even metadata "
                f"(metadata_storage_allowed = {self.metadata_storage_allowed}). "
                f"Deciding clause: {self.deciding_clause or 'none recorded'}"
            )

    def decide_body_storage(self) -> tuple[BodyStorage, str]:
        """How much of each document we may keep, and why.

        The reason is stored on the row. Six months from now, "why is the body
        empty for this source" should be answerable from the data rather than
        from someone's memory of reading the terms.
        """

        if self.full_text_storage_allowed is Permission.ALLOWED:
            return BodyStorage.FULL_TEXT, f"{self.source_key} permits full-text storage ({self.policy_version})"
        return (
            BodyStorage.METADATA_ONLY,
            f"{self.source_key} full_text_storage_allowed = {self.full_text_storage_allowed}; "
            f"keeping metadata only. Deciding clause: {self.deciding_clause or 'none recorded'}",
        )


class NewsSource(Protocol):
    """What every source adapter provides.

    Listing and body retrieval are separate because they have different costs and
    different licence answers: many sources let us list freely and read the body
    only under conditions, and a few let us keep the listing but not the text.
    """

    spec: SourceSpec

    def list_items(self, *, since: datetime | None = None) -> Sequence[FeedItem]: ...

    def fetch_body(self, item: FeedItem) -> str | None: ...


@dataclass
class CollectionResult:
    """Counts that map one-to-one onto a ``news.source_coverage`` row."""

    source_key: str
    as_of_date: datetime
    documents: list[CollectedDocument] = field(default_factory=list)
    items_listed: int = 0
    items_new: int = 0
    items_duplicate: int = 0
    items_revised: int = 0
    items_skipped_licence: int = 0
    fetch_errors: int = 0
    parse_errors: int = 0
    quality_warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def coverage_quality(self) -> str:
        if self.fetch_errors or self.parse_errors:
            return "PARTIAL_KNOWN_GAP"
        if self.items_listed == 0:
            # Not an error. News sources are legitimately quiet, especially
            # ministries at weekends. But an empty result and a silent outage
            # look identical from here, so it is recorded rather than smoothed
            # over, and the caller decides using the source's own schedule.
            return "UNKNOWN"
        return "COMPLETE"

    def as_dict(self) -> dict:
        return {
            "source_key": self.source_key,
            "items_listed": self.items_listed,
            "items_new": self.items_new,
            "items_duplicate": self.items_duplicate,
            "items_revised": self.items_revised,
            "items_skipped_licence": self.items_skipped_licence,
            "fetch_errors": self.fetch_errors,
            "parse_errors": self.parse_errors,
            "quality_warnings": len(self.quality_warnings),
            "coverage_quality": self.coverage_quality,
        }


def content_fingerprint(
    *,
    title: str | None,
    url: str | None,
    body: str | None,
    summary: str | None,
) -> str:
    """Hash of everything we actually hold for a document.

    Deliberately over what we *keep*, not over what exists. For a metadata-only
    source the hash covers the title and URL, so an edit to the body we never
    saw will not be detected - which is correct: claiming to detect a revision
    we have no way of seeing would be worse than admitting we cannot.
    """

    parts = [title or "", url or "", body if body is not None else "", summary or ""]
    joined = "".join(parts).encode("utf-8")
    return hashlib.sha256(joined).hexdigest()


@dataclass(frozen=True)
class KnownDocument:
    """What we already hold for one source document, for revision detection."""

    content_sha256: str
    revision_seq: int
    document_id: str | None = None


def collect(
    source: NewsSource,
    policy: SourcePolicy,
    *,
    known: Mapping[str, KnownDocument] | None = None,
    since: datetime | None = None,
    now: datetime | None = None,
    document_type: DocumentType | None = None,
    assume_source_tz_note: str | None = None,
) -> CollectionResult:
    """Run one collection pass over one source.

    ``known`` maps ``source_document_id`` to what we already hold, so an amended
    document becomes a new revision rather than a duplicate or an overwrite.
    """

    policy.assert_may_collect()
    body_storage, storage_reason = policy.decide_body_storage()

    known = known or {}
    first_seen_at = now or utcnow()
    result = CollectionResult(source_key=source.spec.source_key, as_of_date=first_seen_at)
    if assume_source_tz_note:
        result.notes.append(assume_source_tz_note)

    try:
        items = list(source.list_items(since=since))
    except Exception as exc:  # noqa: BLE001 - one bad source must not stop the rest
        result.fetch_errors += 1
        result.notes.append(f"listing failed: {type(exc).__name__}: {exc}")
        return result

    result.items_listed = len(items)
    if not items:
        result.notes.append(
            "the source listed nothing. That is normal for a source that publishes irregularly, "
            "and indistinguishable from an outage from here; the caller decides using the "
            "source's documented schedule."
        )

    for item in items:
        body: str | None = None
        if body_storage is BodyStorage.FULL_TEXT:
            try:
                body = source.fetch_body(item)
            except Exception as exc:  # noqa: BLE001
                result.fetch_errors += 1
                result.notes.append(f"body fetch failed for {item.source_document_id}: {type(exc).__name__}: {exc}")
                continue
            if body is None:
                # Permitted to keep the text, but there was none to keep. Record
                # the document rather than dropping it: its existence and timing
                # are themselves information.
                result.quality_warnings.append(f"no body returned for {item.source_document_id}")

        effective_storage = body_storage
        if body_storage is BodyStorage.FULL_TEXT and body is None:
            effective_storage = BodyStorage.METADATA_ONLY

        fingerprint = content_fingerprint(
            title=item.title,
            url=item.url,
            body=body,
            summary=item.summary,
        )

        previous = known.get(item.source_document_id)
        if previous is not None and previous.content_sha256 == fingerprint:
            result.items_duplicate += 1
            continue

        ingested_at = now or utcnow()
        times = KnowledgeTimes.observed(
            first_seen_at=first_seen_at,
            ingested_at=ingested_at,
            source_published_at=item.published_at,
            source_published_precision=item.published_precision,
        )

        warnings: list[str] = []
        if times.published_after_we_saw_it:
            warnings.append(
                f"source_published_at ({item.published_at.isoformat() if item.published_at else '?'}) is after "
                f"system_first_seen_at ({first_seen_at.isoformat()}); clock skew or a timezone error at the source"
            )
        if item.published_precision is TimePrecision.UNKNOWN:
            warnings.append("no usable publication timestamp from the source")
        result.quality_warnings.extend(warnings)

        document = CollectedDocument(
            source_key=source.spec.source_key,
            source_document_id=item.source_document_id,
            document_type=document_type or source.spec.default_document_type,
            times=times,
            content_sha256=fingerprint,
            body_storage=effective_storage,
            body_storage_reason=storage_reason,
            title=item.title,
            document_url=item.url,
            language=item.language,
            body_text=body if effective_storage in (BodyStorage.FULL_TEXT, BodyStorage.EXCERPT) else None,
            byte_size=len(body.encode("utf-8")) if body is not None else None,
            revision_seq=(previous.revision_seq + 1) if previous else 1,
            supersedes_document_id=previous.document_id if previous else None,
            quality_warnings=tuple(warnings),
        )
        result.documents.append(document)
        if previous is not None:
            result.items_revised += 1
        else:
            result.items_new += 1

    return result
