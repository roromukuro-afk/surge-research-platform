"""Market data: one canonical shape, one comparable series, one eligibility rule."""

from surge.market.eligibility import (
    DEFAULT_RULE,
    EligibilityDecision,
    EligibilityResult,
    FilterRule,
    FxObservation,
)
from surge.market.models import (
    SHARE_COUNT_ACTIONS,
    CanonicalAction,
    CanonicalAdjustedBar,
    CanonicalBar,
    CanonicalCoverage,
    CorporateActionType,
    NormalisationResult,
    PriceBasis,
)
from surge.market.series import (
    ComparableBar,
    ComparableSeries,
    SeriesError,
    build_comparable_series,
    latest_price,
)

__all__ = [
    "DEFAULT_RULE",
    "SHARE_COUNT_ACTIONS",
    "CanonicalAction",
    "CanonicalAdjustedBar",
    "CanonicalBar",
    "CanonicalCoverage",
    "ComparableBar",
    "ComparableSeries",
    "CorporateActionType",
    "EligibilityDecision",
    "EligibilityResult",
    "FilterRule",
    "FxObservation",
    "NormalisationResult",
    "PriceBasis",
    "SeriesError",
    "build_comparable_series",
    "latest_price",
]
