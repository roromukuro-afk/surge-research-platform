"""Fetching a day of market data, and being able to prove what happened.

The orchestration knows nothing about any particular provider. It takes an
``EodFetcher`` - anything that can hand back one day's raw bytes plus the
canonical rows parsed from them - and does the same six things every time:

1. store the raw payload, content addressed, so the bytes behind every number
   remain available;
2. record it in the manifest, so a licence purge can find it;
3. compare its digest with the last one for the same logical request, so a
   provider that corrects by silent overwrite becomes visible;
4. write the canonical bars as a Parquet day partition;
5. compute per-security coverage, so gaps look like gaps;
6. return counts and errors rather than raising, so a backfill over two thousand
   days can report what failed without losing what succeeded.

A day that fetches nothing is a failure, not an empty success. That distinction
matters more than it looks: a holiday and an outage both produce no rows, and
only one of them is fine.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Protocol

from surge.licensing import AvailabilityBasis, PersistenceDecision, dataset
from surge.manifest import RawObjectRecord, build_manifest_record
from surge.market.models import CanonicalCoverage, NormalisationResult
from surge.market.parquet_store import ParquetSeriesStore, WrittenPartition
from surge.models import Provenance
from surge.storage.base import ObjectStore

INGEST_JOB_VERSION = "market-ingest-1.0.0"

# Classified so a backfill can tell "this day will never work" from "try again".
TRANSIENT_ERROR = "TRANSIENT"
PERMANENT_ERROR = "PERMANENT"
EMPTY_RESULT = "EMPTY"


@dataclass(frozen=True)
class FetchedDay:
    """What a provider adapter hands back for one trading day."""

    raw_body: bytes
    provenance: Provenance
    normalised: NormalisationResult
    entitlement_plan: str
    license_policy_version: str


class EodFetcher(Protocol):
    """Anything that can produce one day of end-of-day data.

    Deliberately small. A free bulk file, a broker API and a paid vendor all fit
    behind it, which is what lets the core run at zero cost and a paid provider
    be swapped in as a challenger without touching the pipeline.
    """

    provider_id: str
    dataset_key: str
    market_code: str

    def fetch_day(self, trade_date: date) -> FetchedDay: ...


class MarketWriter(Protocol):
    """The database side, kept behind a protocol so tests do not need one."""

    def record_raw_object(self, record: RawObjectRecord) -> None: ...

    def last_digest(self, provider_id: str, dataset_key: str, natural_key: str) -> tuple[str, str] | None:
        """(sha256, object_key) of the previous payload for this request, if any."""

    def record_source_revision(
        self,
        *,
        provider_id: str,
        dataset_key: str,
        natural_key: str,
        previous_sha256: str | None,
        new_sha256: str,
        previous_object_key: str | None,
        new_object_key: str,
        run_id: str | None,
        notes: str | None = None,
    ) -> None: ...

    def write_bars(self, result: NormalisationResult, *, run_id: str | None) -> int: ...

    def write_coverage(self, coverage: list[CanonicalCoverage], *, run_id: str | None) -> int: ...


@dataclass
class DayOutcome:
    trade_date: date
    ok: bool
    rows: int = 0
    raw_object_key: str | None = None
    partition: WrittenPartition | None = None
    revision_detected: bool = False
    previous_sha256: str | None = None
    new_sha256: str | None = None
    error_class: str | None = None
    error: str | None = None
    attempts: int = 1

    def as_dict(self) -> dict:
        return {
            "trade_date": self.trade_date.isoformat(),
            "ok": self.ok,
            "rows": self.rows,
            "raw_object_key": self.raw_object_key,
            "partition_key": self.partition.key if self.partition else None,
            "revision_detected": self.revision_detected,
            "error_class": self.error_class,
            "error": self.error,
            "attempts": self.attempts,
        }


@dataclass
class IngestReport:
    provider_id: str
    dataset_key: str
    market_code: str
    started_at: datetime
    days: list[DayOutcome] = field(default_factory=list)

    @property
    def succeeded(self) -> list[DayOutcome]:
        return [d for d in self.days if d.ok]

    @property
    def failed(self) -> list[DayOutcome]:
        return [d for d in self.days if not d.ok]

    @property
    def rows(self) -> int:
        return sum(d.rows for d in self.days)

    @property
    def revisions(self) -> list[DayOutcome]:
        return [d for d in self.days if d.revision_detected]

    def as_dict(self) -> dict:
        return {
            "provider_id": self.provider_id,
            "dataset_key": self.dataset_key,
            "market_code": self.market_code,
            "started_at": self.started_at.isoformat(),
            "days_attempted": len(self.days),
            "days_succeeded": len(self.succeeded),
            "days_failed": len(self.failed),
            "rows": self.rows,
            "revisions_detected": len(self.revisions),
            "failures": [d.as_dict() for d in self.failed],
            "revisions": [d.as_dict() for d in self.revisions],
        }


def idempotency_key(
    *,
    job_name: str,
    run_mode: str,
    market_code: str,
    provider_id: str,
    dataset_key: str,
    trade_date: date,
    config_hash: str,
    job_version: str = INGEST_JOB_VERSION,
) -> str:
    """The logical identity of one invocation. It does not include the run id.

    Two attempts at the same day under the same configuration are the same
    invocation and must collide; a different provider, date or configuration is
    a different one and must not.
    """

    payload = json.dumps(
        {
            "job": job_name,
            "run_mode": run_mode,
            "market": market_code,
            "provider": provider_id,
            "dataset": dataset_key,
            "trade_date": trade_date.isoformat(),
            "job_version": job_version,
            "config_hash": config_hash,
        },
        sort_keys=True,
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    return f"{job_name}:{market_code}:{provider_id}:{trade_date.isoformat()}:{digest}"


def natural_key_for_day(market_code: str, trade_date: date) -> str:
    return f"{market_code}/{trade_date.isoformat()}"


def compute_coverage(result: NormalisationResult, *, observed_at: datetime) -> list[CanonicalCoverage]:
    """What each security actually had in this payload.

    Coverage is computed from what arrived rather than from what the provider
    claims, because the gap between the two is the thing worth knowing.
    """

    by_symbol: dict[tuple[str, str, str], list] = {}
    for bar in result.bars:
        by_symbol.setdefault((bar.provider_id, bar.dataset_key, bar.native_symbol), []).append(bar)

    coverage: list[CanonicalCoverage] = []
    for (provider_id, dataset_key, symbol), bars in by_symbol.items():
        dates = sorted(b.trade_date for b in bars)
        coverage.append(
            CanonicalCoverage(
                provider_id=provider_id,
                dataset_key=dataset_key,
                market_code=bars[0].market_code,
                native_symbol=symbol,
                first_trade_date=dates[0],
                last_trade_date=dates[-1],
                bar_count=len(bars),
                provider_security_id=bars[0].provider_security_id,
                observed_at=observed_at,
                available_at=bars[0].available_at or observed_at,
                availability_basis=bars[0].availability_basis,
                raw_object_key=bars[0].raw_object_key,
            )
        )
    return coverage


class MarketIngestJob:
    """One provider, one dataset, one market. Days go in; a report comes out."""

    def __init__(
        self,
        fetcher: EodFetcher,
        *,
        store: ObjectStore,
        persistence: PersistenceDecision,
        parquet: ParquetSeriesStore | None = None,
        writer: MarketWriter | None = None,
        run_id: str | None = None,
        max_attempts: int = 3,
        backoff_seconds: float = 1.5,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        # Required, with no default. Whether a provider's data may be kept is
        # not something this job can work out, and a default would answer it -
        # in the permissive direction, silently, for every provider added later.
        # A caller that has not read the terms passes PersistenceDecision.unknown
        # and is refused, which is the correct outcome for not knowing.
        #
        # Checked against the fetcher here as well as against the provenance at
        # write time. Failing at construction is a better place to find out than
        # halfway through a backfill, and the write-time check still stands
        # because the provenance is what actually describes the bytes.
        persistence.assert_is_for(
            provider_id=fetcher.provider_id, dataset_key=fetcher.dataset_key
        )
        self._persistence = persistence
        self._fetcher = fetcher
        self._store = store
        self._parquet = parquet
        self._writer = writer
        self._run_id = run_id
        self._max_attempts = max(1, max_attempts)
        self._backoff = backoff_seconds
        self._sleep = sleep
        self._clock = clock

    # ------------------------------------------------------------- one day
    def ingest_day(self, trade_date: date) -> DayOutcome:
        attempts = 0
        last_error: Exception | None = None

        while attempts < self._max_attempts:
            attempts += 1
            try:
                fetched = self._fetcher.fetch_day(trade_date)
            except Exception as exc:  # noqa: BLE001 - classified below, never swallowed
                last_error = exc
                if not _is_transient(exc) or attempts >= self._max_attempts:
                    return DayOutcome(
                        trade_date, ok=False, attempts=attempts,
                        error_class=TRANSIENT_ERROR if _is_transient(exc) else PERMANENT_ERROR,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                self._sleep(self._backoff * attempts)
                continue

            return self._store_day(trade_date, fetched, attempts)

        return DayOutcome(  # pragma: no cover - the loop always returns first
            trade_date, ok=False, attempts=attempts, error_class=TRANSIENT_ERROR,
            error=f"exhausted retries: {last_error}",
        )

    def _store_day(self, trade_date: date, fetched: FetchedDay, attempts: int) -> DayOutcome:
        provenance = fetched.provenance
        spec = dataset(provenance.dataset)

        # Before the first byte lands anywhere. Everything below this line is
        # durable - the object store, the raw object manifest, the parquet
        # partition, the bars and the coverage rows - so this is the one place
        # that has to hold for a provider whose terms do not permit storage.
        #
        # Two checks, and the order matters. First that the permission is about
        # *this* data: an ALLOWED decision for another provider is a real
        # permission for something else, and letting it through would put a
        # genuine licence in the audit trail for a dataset it was never about.
        # Only then whether it says yes.
        self._persistence.assert_governs_fetch(
            provider_id=provenance.source_id,
            dataset_key=provenance.dataset,
            policy_version=fetched.license_policy_version,
        )
        self._persistence.assert_may_persist(target="the object store and the market tables")

        stored = self._store.put_content_addressed(
            provenance.source_id, provenance.dataset, fetched.raw_body, spec.content_type, spec.extension
        )
        record = build_manifest_record(
            stored=stored,
            provenance=provenance,
            license_policy_version=fetched.license_policy_version,
            entitlement_plan=fetched.entitlement_plan,
            availability_basis=AvailabilityBasis.OBSERVED_NOW,
            data_from=trade_date,
            data_to=trade_date,
            run_id=self._run_id,
        )

        outcome = DayOutcome(trade_date, ok=True, raw_object_key=stored.key, attempts=attempts)
        outcome.new_sha256 = stored.sha256

        if self._writer is not None:
            self._writer.record_raw_object(record)
            outcome.revision_detected, outcome.previous_sha256 = self._detect_revision(
                provenance, trade_date, stored.key, stored.sha256
            )

        bars = fetched.normalised.bars
        if not bars:
            # An empty day is a finding. A market holiday and a failed fetch look
            # identical here, and only the caller knows the trading calendar.
            outcome.ok = False
            outcome.error_class = EMPTY_RESULT
            outcome.error = "the payload parsed to no bars"
            return outcome

        outcome.rows = len(bars)

        if self._parquet is not None:
            outcome.partition = self._parquet.write_day(
                bars,
                provider_id=self._fetcher.provider_id,
                market_code=self._fetcher.market_code,
                dataset_key=self._fetcher.dataset_key,
                trade_date=trade_date,
            )

        if self._writer is not None:
            self._writer.write_bars(fetched.normalised, run_id=self._run_id)
            self._writer.write_coverage(
                compute_coverage(fetched.normalised, observed_at=self._clock()), run_id=self._run_id
            )

        return outcome

    def _detect_revision(
        self, provenance: Provenance, trade_date: date, object_key: str, digest: str
    ) -> tuple[bool, str | None]:
        """Did the same request come back with different bytes than last time?"""

        assert self._writer is not None
        natural_key = natural_key_for_day(self._fetcher.market_code, trade_date)
        previous = self._writer.last_digest(provenance.source_id, provenance.dataset, natural_key)

        if previous is None or previous[0] == digest:
            return (False, previous[0] if previous else None)

        self._writer.record_source_revision(
            provider_id=provenance.source_id,
            dataset_key=provenance.dataset,
            natural_key=natural_key,
            previous_sha256=previous[0],
            new_sha256=digest,
            previous_object_key=previous[1],
            new_object_key=object_key,
            run_id=self._run_id,
            notes="a rolling refetch of the same day returned different bytes",
        )
        return (True, previous[0])

    # --------------------------------------------------------- many days
    def backfill(self, days: Iterable[date], *, stop_after_failures: int | None = None) -> IngestReport:
        """Walk a date range, keeping what worked.

        One bad day does not abandon the range: a backfill that dies on a
        holiday and loses nine years of progress is worse than one that reports
        the holiday. ``stop_after_failures`` exists for the other case, where a
        credential has expired and every remaining day will fail too.
        """

        report = IngestReport(
            provider_id=self._fetcher.provider_id,
            dataset_key=self._fetcher.dataset_key,
            market_code=self._fetcher.market_code,
            started_at=self._clock(),
        )
        consecutive_failures = 0

        for day in days:
            outcome = self.ingest_day(day)
            report.days.append(outcome)

            if outcome.ok:
                consecutive_failures = 0
                continue

            consecutive_failures += 1
            if stop_after_failures is not None and consecutive_failures >= stop_after_failures:
                break

        return report

    def incremental(self, *, latest: date, revision_window: Iterable[date] = ()) -> IngestReport:
        """Today's data, plus a rolling refetch of recent days.

        The refetch is the only defence against a provider that corrects data by
        overwriting it: nothing announces the correction, so the window is
        re-read and the digests compared.
        """

        days = [latest, *[d for d in revision_window if d != latest]]
        return self.backfill(days)


def _is_transient(exc: Exception) -> bool:
    """Network and server failures are worth retrying; a 404 is not."""

    text = f"{type(exc).__name__}: {exc}".lower()
    if any(token in text for token in ("timeout", "timed out", "temporarily", "connection", "reset")):
        return True
    if any(token in text for token in (" 500", " 502", " 503", " 504", "429")):
        return True
    return isinstance(exc, TimeoutError | ConnectionError)
