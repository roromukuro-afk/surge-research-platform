"""The shape every provider is normalised into.

Providers disagree about almost everything: field names, which columns are
adjusted, whether a split is a ratio string or a factor, whether volume is share
count or restated share count. The ingestion jobs should not know any of that.
So each adapter produces these, and everything downstream - storage, features,
the eligibility filter - sees one shape.

The one thing the normalisation never does is smooth over an adjustment basis.
A canonical bar carries the basis of each of its own columns, because that is a
property of the number and not of the provider.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from surge.licensing import AvailabilityBasis


class PriceBasis(StrEnum):
    RAW = "RAW"
    SPLIT_ADJUSTED = "SPLIT_ADJUSTED"
    SPLIT_AND_DIVIDEND_ADJUSTED = "SPLIT_AND_DIVIDEND_ADJUSTED"
    PROVIDER_UNSPECIFIED = "PROVIDER_UNSPECIFIED"


class CorporateActionType(StrEnum):
    SPLIT = "SPLIT"
    REVERSE_SPLIT = "REVERSE_SPLIT"
    RIGHTS_ISSUE = "RIGHTS_ISSUE"
    STOCK_DIVIDEND = "STOCK_DIVIDEND"
    CASH_DIVIDEND = "CASH_DIVIDEND"
    SPINOFF = "SPINOFF"
    ADR_RATIO_CHANGE = "ADR_RATIO_CHANGE"
    TICKER_CHANGE = "TICKER_CHANGE"
    OTHER = "OTHER"
    UNKNOWN = "UNKNOWN"


# Actions that change the share count, and therefore the comparability of a
# price series. A cash dividend does not: it changes total return, not the
# number of shares, and this project does not add dividends to its target.
SHARE_COUNT_ACTIONS = frozenset(
    {
        CorporateActionType.SPLIT,
        CorporateActionType.REVERSE_SPLIT,
        CorporateActionType.STOCK_DIVIDEND,
    }
)


@dataclass(frozen=True)
class CanonicalBar:
    """One session for one security, as the provider reported it."""

    provider_id: str
    dataset_key: str
    market_code: str
    native_symbol: str
    trade_date: date
    currency: str

    open: Decimal | None = None
    high: Decimal | None = None
    low: Decimal | None = None
    close: Decimal | None = None
    volume: Decimal | None = None
    turnover: Decimal | None = None

    open_basis: PriceBasis = PriceBasis.PROVIDER_UNSPECIFIED
    high_basis: PriceBasis = PriceBasis.PROVIDER_UNSPECIFIED
    low_basis: PriceBasis = PriceBasis.PROVIDER_UNSPECIFIED
    close_basis: PriceBasis = PriceBasis.PROVIDER_UNSPECIFIED
    volume_basis: PriceBasis = PriceBasis.PROVIDER_UNSPECIFIED

    exchange_code: str | None = None
    provider_security_id: str | None = None
    source_data_version: str | None = None
    identity_version: str | None = None

    source_timestamp: datetime | None = None
    observed_at: datetime | None = None
    available_at: datetime | None = None
    availability_basis: AvailabilityBasis = AvailabilityBasis.OBSERVED_NOW
    raw_object_key: str | None = None

    @property
    def key(self) -> tuple[str, str, str, date]:
        return (self.provider_id, self.dataset_key, self.native_symbol, self.trade_date)

    @property
    def has_prices(self) -> bool:
        return self.close is not None

    def is_ohlc_raw(self) -> bool:
        """Whether every price column is as traded.

        The eligibility filter insists on this: a 3,000 JPY test run against a
        split-adjusted close compares today's threshold with yesterday's share.
        """

        return all(
            basis is PriceBasis.RAW
            for basis in (self.open_basis, self.high_basis, self.low_basis, self.close_basis)
        )


@dataclass(frozen=True)
class CanonicalAdjustedBar:
    """The provider's own adjusted series, kept beside the raw one, never merged."""

    provider_id: str
    dataset_key: str
    native_symbol: str
    trade_date: date

    adj_open: Decimal | None = None
    adj_high: Decimal | None = None
    adj_low: Decimal | None = None
    adj_close: Decimal | None = None
    adj_volume: Decimal | None = None
    adjusted_close: Decimal | None = None

    adjustment_basis: PriceBasis = PriceBasis.SPLIT_ADJUSTED
    adjusted_close_basis: PriceBasis | None = None
    adjustment_factor: Decimal | None = None
    ex_event_code: str | None = None
    # J-Quants recomputes its adjusted series backwards on every new split, with
    # no stated limit. A value like that is not a historical fact.
    recomputed_retroactively: bool = False

    provider_security_id: str | None = None
    source_data_version: str | None = None
    observed_at: datetime | None = None
    available_at: datetime | None = None
    availability_basis: AvailabilityBasis = AvailabilityBasis.OBSERVED_NOW
    raw_object_key: str | None = None


@dataclass(frozen=True)
class CanonicalAction:
    """A corporate action, kept apart from prices so a split is never a return."""

    provider_id: str
    dataset_key: str
    market_code: str
    native_symbol: str
    action_type: CorporateActionType
    ex_date: date

    split_from: Decimal | None = None
    split_to: Decimal | None = None
    adjustment_factor: Decimal | None = None
    amount: Decimal | None = None
    unadjusted_amount: Decimal | None = None
    currency: str | None = None

    record_date: date | None = None
    payment_date: date | None = None
    declaration_date: date | None = None
    from_symbol: str | None = None
    to_symbol: str | None = None

    provider_native_type: str | None = None
    provider_security_id: str | None = None
    source_data_version: str | None = None
    observed_at: datetime | None = None
    available_at: datetime | None = None
    availability_basis: AvailabilityBasis = AvailabilityBasis.OBSERVED_NOW
    raw_object_key: str | None = None

    @property
    def share_multiplier(self) -> Decimal | None:
        """How many shares one share becomes. 2-for-1 gives 2; 1-for-50 gives 1/50.

        None when the action does not change the share count, or when the
        provider gave a ratio we could not read - in which case the caller must
        treat the series as broken at this date rather than assume 1.
        """

        if self.action_type not in SHARE_COUNT_ACTIONS:
            return None
        if self.split_to is not None and self.split_from not in (None, 0):
            return self.split_to / self.split_from
        if self.adjustment_factor is not None and self.adjustment_factor != 0:
            # J-Quants publishes the price factor: 0.5 on a two-for-one. The
            # share count moves the other way.
            return Decimal(1) / self.adjustment_factor
        return None


@dataclass(frozen=True)
class CanonicalCoverage:
    """What a provider says it has for one security."""

    provider_id: str
    dataset_key: str
    market_code: str
    native_symbol: str
    first_trade_date: date | None = None
    last_trade_date: date | None = None
    is_delisted: bool | None = None
    delisted_on: date | None = None
    corporate_action_completeness: str = "UNKNOWN"
    completeness_reason: str | None = None
    provider_security_id: str | None = None
    bar_count: int | None = None
    observed_at: datetime | None = None
    available_at: datetime | None = None
    availability_basis: AvailabilityBasis = AvailabilityBasis.OBSERVED_NOW
    raw_object_key: str | None = None


@dataclass
class NormalisationResult:
    """What one fetch normalised into, plus what it could not."""

    bars: list[CanonicalBar] = field(default_factory=list)
    adjusted: list[CanonicalAdjustedBar] = field(default_factory=list)
    actions: list[CanonicalAction] = field(default_factory=list)
    coverage: list[CanonicalCoverage] = field(default_factory=list)
    rejected: list[dict] = field(default_factory=list)

    def extend(self, other: NormalisationResult) -> None:
        self.bars.extend(other.bars)
        self.adjusted.extend(other.adjusted)
        self.actions.extend(other.actions)
        self.coverage.extend(other.coverage)
        self.rejected.extend(other.rejected)

    @property
    def counts(self) -> dict[str, int]:
        return {
            "bars": len(self.bars),
            "adjusted": len(self.adjusted),
            "actions": len(self.actions),
            "coverage": len(self.coverage),
            "rejected": len(self.rejected),
        }
