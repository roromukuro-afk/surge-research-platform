"""The FX job: fetch the ECB reference rates, derive USD/JPY, store all of it.

This one costs nothing and needs no credential, so it is the job that can be run
for real at any time - which makes it the one worth getting exactly right, since
every US eligibility decision multiplies by its output.

Three behaviours are deliberate:

*Conditional refetch.* The ECB returns Last-Modified and honours
If-Modified-Since, so a rolling re-read of recent days costs one 304 on a quiet
day. That is what makes checking for revisions cheap enough to do daily rather
than hopefully.

*Both legs stored.* The cross is derived, and the derivation is recorded as a
string on the row. The ECB's own terms require a modification of its data to be
declared; more practically, the legs are published to different precisions and
only they can explain the rounding in the quotient.

*Incomplete days stored as incomplete.* A date with one leg produces a row with
a null cross and a note, not a silently skipped date. The difference matters
when something later asks why a Tuesday has no rate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Protocol

from surge.licensing import AvailabilityBasis, dataset
from surge.manifest import RawObjectRecord, build_manifest_record
from surge.market.eligibility import FxObservation
from surge.providers.ecb_fx import DATASET_KEY, PROVIDER_ID, EcbFxProvider, UsdJpyRate
from surge.storage.base import ObjectStore

FX_JOB_VERSION = "fx-ingest-1.0.0"
RATE_KIND_REFERENCE = "REFERENCE_RATE"

ECB_LICENSE_POLICY = "ecb-2026-09-16"
ECB_PLAN = "public"


class FxWriter(Protocol):
    def record_raw_object(self, record: RawObjectRecord) -> None: ...

    def write_fx(self, params: dict) -> None: ...


@dataclass
class FxIngestReport:
    provider_id: str = PROVIDER_ID
    dataset_key: str = DATASET_KEY
    not_modified: bool = False
    observations: int = 0
    complete_rates: int = 0
    incomplete_rates: int = 0
    stored_object_key: str | None = None
    source_published_at: datetime | None = None
    first_date: date | None = None
    last_date: date | None = None
    notes: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    def as_dict(self) -> dict:
        return {
            "provider_id": self.provider_id,
            "dataset_key": self.dataset_key,
            "not_modified": self.not_modified,
            "observations": self.observations,
            "complete_rates": self.complete_rates,
            "incomplete_rates": self.incomplete_rates,
            "stored_object_key": self.stored_object_key,
            "source_published_at": (
                self.source_published_at.isoformat() if self.source_published_at else None
            ),
            "first_date": self.first_date.isoformat() if self.first_date else None,
            "last_date": self.last_date.isoformat() if self.last_date else None,
            "notes": self.notes,
            "error": self.error,
            "ok": self.ok,
        }


def rate_params(
    rate: UsdJpyRate,
    *,
    content_sha256: str,
    raw_object_key: str,
    observed_at: datetime,
    available_at: datetime,
    source_published_at: datetime | None,
    run_id: str | None,
) -> dict:
    return {
        "provider_id": PROVIDER_ID,
        "dataset_key": DATASET_KEY,
        "source_date": rate.source_date,
        "eur_usd": rate.eur_usd,
        "eur_jpy": rate.eur_jpy,
        "derived_usd_jpy": rate.derived_usd_jpy,
        "derivation_method": rate.derivation_method,
        "eur_usd_decimals": rate.eur_usd_decimals,
        "eur_jpy_decimals": rate.eur_jpy_decimals,
        "rate_kind": RATE_KIND_REFERENCE,
        "source_published_at": source_published_at,
        "observed_at": observed_at,
        # We could act on it when it arrived, not when the ECB published it.
        # source_published_at keeps the ECB's own claim alongside.
        "available_at": available_at,
        "availability_basis": str(AvailabilityBasis.OBSERVED_NOW),
        "content_sha256": content_sha256,
        "raw_object_key": raw_object_key,
        "run_id": run_id,
        "notes": rate.note,
    }


class FxIngestJob:
    def __init__(
        self,
        *,
        store: ObjectStore,
        writer: FxWriter | None = None,
        provider: EcbFxProvider | None = None,
        run_id: str | None = None,
    ) -> None:
        self._store = store
        self._writer = writer
        self._provider = provider or EcbFxProvider()
        self._run_id = run_id

    def run(
        self,
        *,
        start: date | None = None,
        end: date | None = None,
        last_n: int | None = None,
        if_modified_since: str | None = None,
    ) -> FxIngestReport:
        report = FxIngestReport()

        if start is None and end is None and last_n is None:
            last_n = 10  # a fortnight of TARGET days: today plus a revision window

        try:
            fetched = self._provider.fetch(
                start=start, end=end, last_n=last_n, if_modified_since=if_modified_since
            )
        except Exception as exc:  # noqa: BLE001 - a job reports, it does not raise
            report.error = f"{type(exc).__name__}: {exc}"
            return report

        report.source_published_at = fetched.source_published_at

        if fetched.not_modified:
            report.not_modified = True
            report.notes.append("the ECB reported no change since the last fetch")
            return report

        report.observations = len(fetched.observations)
        complete = [r for r in fetched.rates if r.is_complete]
        report.complete_rates = len(complete)
        report.incomplete_rates = len(fetched.rates) - len(complete)
        if fetched.rates:
            report.first_date = min(r.source_date for r in fetched.rates)
            report.last_date = max(r.source_date for r in fetched.rates)
        if report.incomplete_rates:
            report.notes.append(
                f"{report.incomplete_rates} date(s) had only one leg and are stored without a cross"
            )

        if not fetched.rates:
            report.error = "the response parsed to no observations"
            return report

        spec = dataset(DATASET_KEY)
        stored = self._store.put_content_addressed(
            PROVIDER_ID, DATASET_KEY, fetched.body, spec.content_type, spec.extension
        )
        report.stored_object_key = stored.key

        if self._writer is not None:
            self._writer.record_raw_object(
                build_manifest_record(
                    stored=stored,
                    provenance=fetched.provenance,
                    license_policy_version=ECB_LICENSE_POLICY,
                    entitlement_plan=ECB_PLAN,
                    availability_basis=AvailabilityBasis.OBSERVED_NOW,
                    data_from=report.first_date,
                    data_to=report.last_date,
                    run_id=self._run_id,
                )
            )
            for rate in fetched.rates:
                self._writer.write_fx(
                    rate_params(
                        rate,
                        content_sha256=stored.sha256,
                        raw_object_key=stored.key,
                        observed_at=fetched.provenance.observed_at,
                        available_at=fetched.provenance.available_at,
                        source_published_at=fetched.source_published_at,
                        run_id=self._run_id,
                    )
                )

        return report


def latest_observation(rates: list[UsdJpyRate], *, observed_at: datetime, available_at: datetime) -> FxObservation | None:
    """The newest complete cross, in the shape the eligibility filter wants."""

    complete = [r for r in rates if r.is_complete]
    if not complete:
        return None
    newest = max(complete, key=lambda r: r.source_date)
    return FxObservation(
        rate=newest.derived_usd_jpy,  # type: ignore[arg-type]
        source_date=newest.source_date,
        observed_at=observed_at,
        available_at=available_at,
        provider_id=PROVIDER_ID,
    )


def http_date(moment: datetime) -> str:
    """Format a timestamp the way If-Modified-Since wants it."""

    return moment.astimezone(UTC).strftime("%a, %d %b %Y %H:%M:%S GMT")
