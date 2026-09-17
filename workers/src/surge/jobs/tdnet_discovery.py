"""The TDnet discovery pass: index in, material candidates out.

    Yanoshin item
      -> security master mapping
      -> material candidate
      -> title classification
      -> verification source lookup (issuer IR, EDINET)
      -> source link on the same event

The rule that shapes the last two steps: **the discovery timestamp is when
Yanoshin first gave us the item, and fetching the official document later never
moves it.** A verification source arriving hours afterwards joins the event as a
second source with its own, later, availability time; the event's own
``first_known_at`` is the minimum across its sources, so it stays where discovery
put it. That is enforced by a database trigger rather than by this code, which is
why it cannot be undone by a careless write here.

What this job does not do is fetch any TDnet document. The index is what we are
licensed to keep.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Protocol

from surge.material.tdnet_classify import CLASSIFIER_VERSION, Classification, classify
from surge.news.models import BodyStorage, CollectedDocument, DocumentType, KnowledgeTimes, TimePrecision
from surge.news.sources.yanoshin_tdnet import (
    ADAPTER_VERSION,
    SOURCE_KEY,
    CodeNormalisation,
    FetchResult,
    TdnetItem,
    YanoshinTdnetSource,
    recovery_window,
)

JOB_VERSION = "tdnet-discovery-1.0.0"


class SecurityResolver(Protocol):
    """Maps a normalised TDnet code to a security id, or admits it cannot."""

    def resolve(self, normalised_code: str, *, as_of: datetime) -> tuple[str | None, str | None]:
        """Return ``(security_id, market_code)``; ``(None, None)`` if unmapped."""
        ...


class VerificationFinder(Protocol):
    """Looks for a storable source that confirms what a disclosure said."""

    def find(self, item: TdnetItem, classification: Classification) -> dict | None: ...


@dataclass
class DiscoveredItem:
    """One index row, mapped and typed, ready to be written."""

    item: TdnetItem
    classification: Classification
    document: CollectedDocument
    security_id: str | None = None
    market_code: str | None = None
    unmapped_reason: str | None = None
    verification: dict | None = None
    #: Which lookup key matched. Differs from the normalised code when the
    #: master held the security under its four-character base.
    matched_key: str | None = None
    matched_on_base: bool = False

    @property
    def mapping_confidence(self) -> str | None:
        """How the link was established, not how strongly anyone feels about it.

        An exact code match against the exchange's own listing is
        REGISTRY_ANCHORED. A match on the four-character base is one inference
        step further out, so it is PROVISIONAL - which is a legitimate state to
        store and an illegitimate one to hide.
        """

        if self.security_id is None:
            return None
        return "PROVISIONAL" if self.matched_on_base else "REGISTRY_ANCHORED"

    @property
    def is_mapped(self) -> bool:
        return self.security_id is not None

    @property
    def metadata_only(self) -> bool:
        """No storable source confirms this, so the index row is all we hold."""

        return self.verification is None

    def tdnet_row(self, *, run_id: str | None, response_sha256: str, endpoint: str) -> dict:
        times = self.document.times
        return {
            "yanoshin_id": self.item.yanoshin_id,
            "pubdate": self.item.pubdate,
            "raw_company_code": self.item.code.raw,
            "normalised_company_code": self.item.code.normalised,
            "code_normalisation": self.item.code.kind.value,
            "company_name": self.item.company_name,
            "title": self.item.title,
            "document_url": self.item.tdnet_document_url,
            "url_xbrl": self.item.url_xbrl,
            "markets_string": self.item.markets_string,
            "update_history": self.item.update_history,
            "security_id": self.security_id,
            "listing_market_code": self.market_code,
            "mapping_confidence": self.mapping_confidence,
            "unmapped_reason": self.unmapped_reason,
            "matched_lookup_key": self.matched_key,
            "source_endpoint": endpoint,
            "raw_response_sha256": response_sha256,
            "system_first_seen_at": times.system_first_seen_at,
            "ingested_at": times.ingested_at,
            "available_to_model_at": times.available_to_model_at,
            "run_id": run_id,
        }


@dataclass
class DiscoveryReport:
    """Counts that map one-to-one onto a ``news.tdnet_coverage`` row."""

    as_of_date: date
    endpoint: str
    job_version: str = JOB_VERSION
    adapter_version: str = ADAPTER_VERSION
    classifier_version: str = CLASSIFIER_VERSION

    items: list[DiscoveredItem] = field(default_factory=list)
    items_retrieved: int = 0
    unique_items: int = 0
    new_items: int = 0
    duplicate_items: int = 0
    unmapped_company_codes: int = 0
    document_url_present: int = 0
    xbrl_url_present: int = 0
    verification_source_found: int = 0
    metadata_only_events: int = 0
    fetch_errors: int = 0
    last_seen_id: int | None = None
    collection_lag_seconds: float | None = None
    window_was_full: bool = False
    notes: list[str] = field(default_factory=list)

    def as_coverage_row(self, *, run_id: str | None = None) -> dict:
        return {
            "run_id": run_id,
            "as_of_date": self.as_of_date,
            "items_retrieved": self.items_retrieved,
            "unique_items": self.unique_items,
            "new_items": self.new_items,
            "duplicate_items": self.duplicate_items,
            "unmapped_company_codes": self.unmapped_company_codes,
            "document_url_present": self.document_url_present,
            "xbrl_url_present": self.xbrl_url_present,
            "verification_source_found": self.verification_source_found,
            "metadata_only_events": self.metadata_only_events,
            "fetch_errors": self.fetch_errors,
            "last_seen_id": self.last_seen_id,
            "collection_lag_seconds": self.collection_lag_seconds,
            "endpoint": self.endpoint,
            "notes": "\n".join(self.notes) or None,
        }

    @property
    def summary(self) -> dict:
        return {k: v for k, v in self.as_coverage_row().items() if k not in ("run_id", "notes")}


class NullResolver:
    """Resolves nothing, and says so per code.

    The default, because the security master is a separate concern and a
    discovery pass that cannot reach it should still record what it saw.
    """

    def resolve(self, normalised_code: str, *, as_of: datetime) -> tuple[str | None, str | None]:
        return None, None


class TdnetDiscoveryJob:
    job_name = "tdnet_discovery"
    job_version = JOB_VERSION

    def __init__(
        self,
        *,
        source: YanoshinTdnetSource | None = None,
        resolver: SecurityResolver | None = None,
        verifier: VerificationFinder | None = None,
    ) -> None:
        self._source = source or YanoshinTdnetSource()
        self._resolver = resolver or NullResolver()
        self._verifier = verifier

    def run(
        self,
        *,
        now: datetime | None = None,
        known_ids: Sequence[int] | None = None,
        known_hashes: Mapping[int, str] | None = None,
        last_success: datetime | None = None,
        force_range: tuple[date, date] | None = None,
    ) -> DiscoveryReport:
        """One pass.

        ``known_ids`` and ``known_hashes`` come from what is already stored. The
        hash matters as much as the id: the service can revise an item in place
        without changing its id - ``update_history`` exists for exactly that - so
        an id we have seen before with different content is a revision rather
        than a duplicate.
        """

        now = now or datetime.now(UTC)
        known_ids = set(known_ids or ())
        known_hashes = dict(known_hashes or {})

        report = DiscoveryReport(as_of_date=now.date(), endpoint="")

        try:
            if force_range is not None:
                start, end = force_range
                result = self._source.fetch_date_range(start, end, now=now)
                report.notes.append(f"date range {start}..{end} (recovery or integrity check)")
            else:
                result = self._source.fetch_recent(now=now)
        except Exception as exc:  # noqa: BLE001 - one bad pass must not end the day
            report.fetch_errors += 1
            report.notes.append(f"fetch failed: {type(exc).__name__}: {exc}")
            return report

        report.endpoint = result.endpoint
        report.items_retrieved = len(result.items)
        report.collection_lag_seconds = result.collection_lag_seconds()
        report.window_was_full = not self._source.window_is_safe(result)

        if report.window_was_full and force_range is None:
            # A full page may have been truncated, so something older than its
            # oldest row could have been missed. Say so and name the window that
            # would close the gap rather than assuming it is closed.
            start, end = recovery_window(last_success, now)
            report.notes.append(
                f"the page came back full ({report.items_retrieved} items), so it may be truncated; "
                f"re-read {start}..{end} by date range before trusting this as complete"
            )

        seen_this_pass: set[int] = set()
        for item in result.items:
            if item.yanoshin_id in seen_this_pass:
                report.duplicate_items += 1
                continue
            seen_this_pass.add(item.yanoshin_id)

            discovered = self._process(item, result, now=now)

            # Known id AND unchanged content is a duplicate. Known id with
            # different content is a revision, and gets carried.
            fingerprint = discovered.document.content_sha256
            if item.yanoshin_id in known_ids and known_hashes.get(item.yanoshin_id) == fingerprint:
                report.duplicate_items += 1
                continue

            report.items.append(discovered)
            report.new_items += 1
            if not discovered.is_mapped:
                report.unmapped_company_codes += 1
            if discovered.item.tdnet_document_url:
                report.document_url_present += 1
            if discovered.item.url_xbrl:
                report.xbrl_url_present += 1
            if discovered.verification is not None:
                report.verification_source_found += 1
            else:
                report.metadata_only_events += 1

        report.unique_items = len(seen_this_pass)
        report.last_seen_id = self._source.advance_cursor(result)
        return report

    def _process(self, item: TdnetItem, result: FetchResult, *, now: datetime) -> DiscoveredItem:
        classification = classify(item.title)

        security_id: str | None = None
        market_code: str | None = None
        unmapped_reason: str | None = None
        matched_key: str | None = None
        matched_on_base = False

        # The four times come first, because the mapping needs one of them. The
        # code is resolved as of the disclosure's publication (which listing was
        # in force then) using everything the master knows as of our collection
        # (which is the only knowledge we actually have). Backfilling a 2025
        # disclosure in 2026 therefore resolves it correctly and does not claim
        # we knew that mapping in 2025.
        times = KnowledgeTimes.observed(
            first_seen_at=result.fetched_at,
            ingested_at=now if now >= result.fetched_at else result.fetched_at,
            source_published_at=item.pubdate,
            source_published_precision=TimePrecision.EXACT,
        )

        if item.code.kind is CodeNormalisation.UNEXPECTED_SHAPE:
            unmapped_reason = f"code {item.code.raw!r} is not a shape the normaliser recognises"
        else:
            resolve_code = getattr(self._resolver, "resolve_code", None)
            if resolve_code is not None:
                security_id, market_code, matched_key = _call_resolver(
                    resolve_code, item.code, item.pubdate, times.available_to_model_at
                )
            else:
                security_id, market_code = _call_resolver_simple(
                    self._resolver.resolve,
                    item.code.normalised,
                    item.pubdate,
                    times.available_to_model_at,
                )
                matched_key = item.code.normalised if security_id else None

            if security_id is None:
                unmapped_reason = (
                    f"{item.code.normalised} (raw {item.code.raw}) is not in the security master "
                    f"as of {item.pubdate.date()}"
                )
            elif matched_key != item.code.normalised:
                # Matched on the four-character base rather than on the code as
                # given. Almost always an ETF the master carries without the
                # suffix - but "almost always" is why this is recorded as a
                # weaker claim rather than treated as an exact hit.
                matched_on_base = True

        document = CollectedDocument(
            source_key=SOURCE_KEY,
            source_document_id=str(item.yanoshin_id),
            document_type=DocumentType.TIMELY_DISCLOSURE,
            times=times,
            content_sha256=_fingerprint(item),
            # Never FULL_TEXT. The body is a TDnet PDF and this source's licence
            # does not cover it; the database refuses a full-text write too.
            body_storage=BodyStorage.METADATA_ONLY,
            body_storage_reason=(
                "Yanoshin provides an index and a link; the TDnet document behind it is subject to "
                "TDnet's own prohibition on 複製 and is never archived on the strength of this API"
            ),
            title=item.title,
            document_url=item.tdnet_document_url,
            language="ja",
        )

        verification = None
        if self._verifier is not None:
            try:
                verification = self._verifier.find(item, classification)
            except Exception as exc:  # noqa: BLE001
                unmapped_reason = unmapped_reason or None
                verification = None
                _ = exc

        return DiscoveredItem(
            item=item,
            classification=classification,
            document=document,
            security_id=security_id,
            market_code=market_code,
            unmapped_reason=unmapped_reason,
            verification=verification,
            matched_key=matched_key,
            matched_on_base=matched_on_base,
        )


def _call_resolver(resolve_code, code, effective_at: datetime, known_at: datetime):
    """Pass known_at where the resolver accepts it, and not where it does not.

    Test doubles and the null resolver predate the bitemporal signature. Probing
    rather than requiring keeps them working, and keeps the production resolver
    getting both times.
    """

    try:
        return resolve_code(code, as_of=effective_at, known_at=known_at)
    except TypeError:
        return resolve_code(code, as_of=effective_at)


def _call_resolver_simple(resolve, code: str, effective_at: datetime, known_at: datetime):
    try:
        return resolve(code, as_of=effective_at, known_at=known_at)
    except TypeError:
        return resolve(code, as_of=effective_at)


def _fingerprint(item: TdnetItem) -> str:
    """Hash of what we hold for this item.

    Covers the fields that can change under a stable id - the service revises
    titles and URLs in place and records it in ``update_history`` - so a revision
    produces a different hash and is carried rather than dropped as a duplicate.
    """

    from surge.news.collector import content_fingerprint

    return content_fingerprint(
        title=item.title,
        url=item.tdnet_document_url,
        body=None,
        summary=f"{item.code.raw}|{item.url_xbrl or ''}|{item.update_history or ''}",
    )


class DatabaseResolver:
    """Resolves a TDnet code against the security master, bitemporally.

    Two times, and they are not the same time.

    ``effective_at`` is when the disclosure was published - which listing was in
    force then. A code reassigned after a delisting must resolve to whoever held
    it on the day, not to whoever holds it now.

    ``known_at`` is when *we* learned the master's answer, which is now. Reading
    a 2025 disclosure in 2026 is allowed to use everything the master has learned
    since; what it is not allowed to do is claim we knew that mapping in 2025.
    The two arguments keep those apart, and ``ref.listings_as_of`` is the only
    read path that enforces it - a direct query on current rows silently answers
    both questions with today's knowledge.

    The cache is keyed on all three of code, effective date and known date.
    Keying on the code alone would let the first answer for 7203 stand in for
    every later question about 7203, including ones asked as of a different day.
    """

    def __init__(self, conn) -> None:
        self._conn = conn
        self._cache: dict[tuple[str, str, str], tuple[str | None, str | None]] = {}

    SQL = """
    select l.security_id::text, s.market_code::text
    from ref.listings_as_of(%(effective_at)s, %(known_at)s) l
    join ref.securities s on s.security_id = l.security_id
    where l.symbol = %(code)s
      and s.market_code = 'JP'
    order by l.symbol_effective_from desc nulls last
    limit 1
    """

    @staticmethod
    def _bucket(moment: datetime) -> str:
        """Day granularity for the cache key.

        A listing does not change within a day in this master, and bucketing to
        the day keeps one collection pass to a handful of queries instead of one
        per item. Bucketing any coarser would start merging days.
        """

        return moment.astimezone(UTC).date().isoformat()

    def resolve(
        self,
        normalised_code: str,
        *,
        as_of: datetime,
        known_at: datetime | None = None,
    ) -> tuple[str | None, str | None]:
        known_at = known_at or datetime.now(UTC)
        key = (normalised_code, self._bucket(as_of), self._bucket(known_at))
        if key in self._cache:
            return self._cache[key]
        with self._conn.cursor() as cur:
            cur.execute(
                self.SQL,
                {"code": normalised_code, "effective_at": as_of, "known_at": known_at},
            )
            row = cur.fetchone()
        resolved = (row[0], row[1]) if row else (None, None)
        self._cache[key] = resolved
        return resolved

    def resolve_code(
        self,
        code,
        *,
        as_of: datetime,
        known_at: datetime | None = None,
    ) -> tuple[str | None, str | None, str | None]:
        """Try each lookup key in turn and say which one matched.

        Returns ``(security_id, market_code, matched_key)``. A match on the
        four-character base rather than on the exact code is a weaker claim, and
        the caller downgrades the mapping confidence accordingly - the point of
        returning the key rather than only the result.
        """

        for key in code.lookup_keys:
            security_id, market_code = self.resolve(key, as_of=as_of, known_at=known_at)
            if security_id is not None:
                return security_id, market_code, key
        return None, None, None
