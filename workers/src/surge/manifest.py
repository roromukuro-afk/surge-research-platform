"""The record of every raw payload we hold.

Written before the payload is useful, not after. A licence purge works from this
list, so an object stored without a manifest row is an object we cannot prove we
deleted - which, under J-Quants' terms, is worse than not having fetched it.

Each row names the reading of the terms that governed the fetch
(``license_policy_version``) and the plan that entitled us to it, so a later
change of terms or a downgrade can be turned into a precise list of objects
rather than a guess.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from surge.licensing import AvailabilityBasis, Dataset, dataset
from surge.models import Provenance
from surge.storage.base import StoredObject


@dataclass(frozen=True)
class RawObjectRecord:
    object_key: str
    store_id: str
    provider_id: str
    dataset_key: str
    license_policy_version: str
    entitlement_plan: str
    required_min_plan: str
    sha256: str
    bytes: int
    content_type: str
    request_url: str
    observed_at: datetime
    available_at: datetime
    availability_basis: AvailabilityBasis
    data_from: date | None = None
    data_to: date | None = None
    http_status: int | None = None
    source_published_at: datetime | None = None
    run_id: str | None = None

    @property
    def dataset(self) -> Dataset:
        return dataset(self.dataset_key)


def build_manifest_record(
    *,
    stored: StoredObject,
    provenance: Provenance,
    license_policy_version: str,
    entitlement_plan: str,
    availability_basis: AvailabilityBasis = AvailabilityBasis.OBSERVED_NOW,
    data_from: date | None = None,
    data_to: date | None = None,
    run_id: str | None = None,
) -> RawObjectRecord:
    """Describe a stored payload for the manifest.

    ``availability_basis`` defaults to OBSERVED_NOW because that is what an
    ingestion run can honestly claim: it learned this when it fetched it. A
    caller that wants a different basis has to say so, and say why.
    """

    spec = dataset(provenance.dataset)
    return RawObjectRecord(
        object_key=stored.key,
        store_id=stored.store_id,
        provider_id=provenance.source_id,
        dataset_key=provenance.dataset,
        license_policy_version=license_policy_version,
        entitlement_plan=entitlement_plan,
        required_min_plan=spec.required_min_plan,
        sha256=stored.sha256,
        bytes=stored.bytes,
        content_type=stored.content_type,
        request_url=provenance.endpoint,
        observed_at=provenance.observed_at,
        available_at=provenance.available_at,
        availability_basis=availability_basis,
        data_from=data_from,
        data_to=data_to,
        http_status=provenance.http_status,
        source_published_at=provenance.source_published_at,
        run_id=run_id,
    )


INSERT_RAW_OBJECT = """
insert into market.raw_objects (
  object_key, store_id, provider_id, dataset_key, license_policy_version,
  entitlement_plan, required_min_plan, sha256, bytes, content_type,
  data_from, data_to, request_url, http_status,
  observed_at, available_at, availability_basis, source_published_at, run_id
) values (
  %(object_key)s, %(store_id)s, %(provider_id)s, %(dataset_key)s, %(license_policy_version)s,
  %(entitlement_plan)s, %(required_min_plan)s, %(sha256)s, %(bytes)s, %(content_type)s,
  %(data_from)s, %(data_to)s, %(request_url)s, %(http_status)s,
  %(observed_at)s, %(available_at)s, %(availability_basis)s, %(source_published_at)s, %(run_id)s
)
on conflict (object_key) do nothing
"""


def manifest_params(record: RawObjectRecord) -> dict[str, object]:
    return {
        "object_key": record.object_key,
        "store_id": record.store_id,
        "provider_id": record.provider_id,
        "dataset_key": record.dataset_key,
        "license_policy_version": record.license_policy_version,
        "entitlement_plan": record.entitlement_plan,
        "required_min_plan": record.required_min_plan,
        "sha256": record.sha256,
        "bytes": record.bytes,
        "content_type": record.content_type,
        "data_from": record.data_from,
        "data_to": record.data_to,
        "request_url": record.request_url,
        "http_status": record.http_status,
        "observed_at": record.observed_at,
        "available_at": record.available_at,
        "availability_basis": str(record.availability_basis),
        "source_published_at": record.source_published_at,
        "run_id": record.run_id,
    }
