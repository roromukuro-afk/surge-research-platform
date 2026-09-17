"""What each provider lets us do with its data, and what we must do when we stop.

The database is the source of truth for the licence itself (
``market.provider_license_policies``): it is versioned, append-only, and every
stored object names the version that governed it. This module holds the part the
worker needs before it has a database connection - which datasets exist, which
provider and plan they come from, and what the lowest plan is that entitles us
to each one - and a guard that refuses to do anything the terms do not permit.

The asymmetry is deliberate. A missing or unreadable policy is treated as
prohibition, not as freedom.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

LICENSE_CATALOGUE_VERSION = "license-catalogue-2026-09-17"


class LicenseMode(StrEnum):
    PRIVATE_PERSONAL_RESEARCH_ONLY = "PRIVATE_PERSONAL_RESEARCH_ONLY"
    INTERNAL_COMMERCIAL = "INTERNAL_COMMERCIAL"
    PUBLIC_REDISTRIBUTABLE = "PUBLIC_REDISTRIBUTABLE"
    PUBLIC_DOMAIN = "PUBLIC_DOMAIN"


class Permission(StrEnum):
    ALLOWED = "ALLOWED"
    PROHIBITED = "PROHIBITED"
    NOT_SPECIFIED = "NOT_SPECIFIED"
    UNKNOWN = "UNKNOWN"


class Obligation(StrEnum):
    REQUIRED = "REQUIRED"
    NOT_REQUIRED = "NOT_REQUIRED"
    NOT_SPECIFIED = "NOT_SPECIFIED"
    UNKNOWN = "UNKNOWN"


class AvailabilityBasis(StrEnum):
    """How an ``available_at`` was arrived at.

    A bar fetched today is ``OBSERVED_NOW`` however old its trade date is. The
    whole point of the column is that a historical backfill cannot quietly claim
    the system knew something years before it did.
    """

    OBSERVED_NOW = "OBSERVED_NOW"
    PROVIDER_PUBLISHED_TIMESTAMP = "PROVIDER_PUBLISHED_TIMESTAMP"
    DOCUMENTED_SCHEDULE = "DOCUMENTED_SCHEDULE"
    HISTORICAL_REPLAY_ASSUMPTION = "HISTORICAL_REPLAY_ASSUMPTION"


class LicenseAction(StrEnum):
    PUBLIC_DISPLAY = "PUBLIC_DISPLAY"
    THIRD_PARTY_ACCESS = "THIRD_PARTY_ACCESS"
    COMMERCIAL_USE = "COMMERCIAL_USE"
    ACADEMIC_USE = "ACADEMIC_USE"
    RAW_REDISTRIBUTION = "RAW_REDISTRIBUTION"
    DERIVED_OUTPUT_SHARING = "DERIVED_OUTPUT_SHARING"


class LicenseViolation(RuntimeError):
    """Raised instead of doing something the provider's terms do not permit."""


@dataclass(frozen=True)
class Dataset:
    """One independently fetched, independently licensed body of data."""

    dataset_key: str
    provider_id: str
    required_min_plan: str
    content_type: str
    extension: str
    description: str


# The datasets Phase 2.1 touches. required_min_plan is what a downgrade is
# measured against: an object whose dataset needs a plan above the one we still
# hold has to be deleted.
DATASETS: dict[str, Dataset] = {
    d.dataset_key: d
    for d in (
        Dataset(
            "JQ_EQ_BARS_DAILY", "jquants", "Light", "application/json", "json",
            "Daily OHLCV for every TSE listed issue, unadjusted and adjusted side by side.",
        ),
        Dataset(
            "JQ_EQ_MASTER", "jquants", "Light", "application/json", "json",
            "The issue master snapshot for a date. J-Quants publishes no delisting list, "
            "so these snapshots are the only way to detect a delisting or a code change.",
        ),
        Dataset(
            "EODHD_US_EOD_BULK", "eodhd", "EOD Historical Data - All World", "text/csv", "csv",
            "Every US listing's daily bar for one session, in one request.",
        ),
        Dataset(
            "EODHD_SPLITS", "eodhd", "EOD Historical Data - All World", "application/json", "json",
            "Split history for one symbol.",
        ),
        Dataset(
            "EODHD_DIVIDENDS", "eodhd", "EOD Historical Data - All World", "application/json", "json",
            "Dividend history for one symbol, adjusted and unadjusted.",
        ),
        Dataset(
            "EODHD_SYMBOL_LIST", "eodhd", "EOD Historical Data - All World", "application/json", "json",
            "The US symbol list. delisted=1 REPLACES the result set with delisted names rather than adding them.",
        ),
        Dataset(
            "EODHD_SYMBOL_CHANGES", "eodhd", "EOD Historical Data - All World", "application/json", "json",
            "US ticker renames.",
        ),
        Dataset(
            "EODHD_FX_EOD", "eodhd", "EOD Historical Data - All World", "application/json", "json",
            "USDJPY.FOREX daily bars. Secondary FX source, used to cross-check the ECB cross.",
        ),
        Dataset(
            "ALPACA_US_BARS_SIP", "alpaca_historical_sip", "Basic (no cost)", "application/json", "json",
            "US session bars from the consolidated tape (feed=sip), readable without a subscription "
            "only for windows ending at least 15 minutes ago. Whether these may be kept in a private "
            "research database is NOT_SPECIFIED by the terms, which is not the same as permitted.",
        ),
        Dataset(
            "ECB_EXR_DAILY", "ecb", "public", "text/csv", "csv",
            "The euro reference rates for USD and JPY, from which USD/JPY is derived.",
        ),
        Dataset(
            "OPENFIGI_MAPPING", "openfigi", "public", "application/json", "json",
            "OpenFIGI mapping responses. Research enrichment; changes no security_id.",
        ),
    )
}


@dataclass(frozen=True)
class LicensePolicy:
    """One reading of one provider's terms, as recorded in the database."""

    provider_id: str
    dataset_key: str
    policy_version: str
    license_mode: LicenseMode
    public_display_allowed: Permission
    third_party_access_allowed: Permission
    commercial_use_allowed: Permission
    academic_use_allowed: Permission
    raw_redistribution_allowed: Permission
    derived_output_sharing_allowed: Permission
    delete_on_cancel: Obligation
    delete_on_downgrade: Obligation
    attribution_required: Obligation
    modification_disclosure_required: Obligation
    entitlement_plan: str | None
    terms_url: str
    notes: str | None = None

    def permission(self, action: LicenseAction) -> Permission:
        return {
            LicenseAction.PUBLIC_DISPLAY: self.public_display_allowed,
            LicenseAction.THIRD_PARTY_ACCESS: self.third_party_access_allowed,
            LicenseAction.COMMERCIAL_USE: self.commercial_use_allowed,
            LicenseAction.ACADEMIC_USE: self.academic_use_allowed,
            LicenseAction.RAW_REDISTRIBUTION: self.raw_redistribution_allowed,
            LicenseAction.DERIVED_OUTPUT_SHARING: self.derived_output_sharing_allowed,
        }[action]

    def assert_allows(self, action: LicenseAction) -> None:
        """Raise unless the terms explicitly permit this.

        NOT_SPECIFIED raises as surely as PROHIBITED does. A licence that does
        not mention an activity has not agreed to it.
        """

        granted = self.permission(action)
        if granted is not Permission.ALLOWED:
            raise LicenseViolation(
                f"{self.provider_id}/{self.dataset_key} does not permit {action}: "
                f"the terms say {granted} ({self.terms_url})"
            )

    @property
    def must_delete_on_exit(self) -> bool:
        """Whether leaving the plan obliges us to destroy what we stored.

        Only REQUIRED is treated as an obligation here, because this drives the
        purge. NOT_SPECIFIED is reported separately rather than silently
        upgraded to an obligation or silently dropped.
        """

        return Obligation.REQUIRED in (self.delete_on_cancel, self.delete_on_downgrade)


def dataset(dataset_key: str) -> Dataset:
    try:
        return DATASETS[dataset_key]
    except KeyError:  # pragma: no cover - programmer error
        raise KeyError(f"unknown dataset {dataset_key!r}; add it to surge.licensing.DATASETS") from None


def datasets_for(provider_id: str) -> list[Dataset]:
    return [d for d in DATASETS.values() if d.provider_id == provider_id]
