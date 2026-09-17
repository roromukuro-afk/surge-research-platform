"""The 3,000 JPY hard filter.

The rule is one line. Everything here is about the cases where the rule cannot
honestly be applied, because those are the ones that corrupt a research set if
they are quietly folded into "not eligible":

* no price at all is not the same as too expensive;
* a price from three weeks ago is not a price;
* a US security with no USD/JPY the cutoff could have known is not cheap and is
  not dear - it is unmeasured.

Three further rules come from the project's own constraints:

* the price must be RAW as traded. A split-adjusted close compares today's
  threshold against a share count that no longer exists.
* nothing later than the knowledge cutoff may be read, for either the price or
  the rate. This is the leak the whole bitemporal design exists to prevent.
* the age of both inputs is recorded. A rate carried across a long weekend is a
  legitimate answer, but it is not a same-day rate and must not be stored as one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum

from surge.market.models import CanonicalBar, PriceBasis

MARKET_JP = "JP"
MARKET_US = "US"


class EligibilityDecision(StrEnum):
    PRICE_ELIGIBLE = "PRICE_ELIGIBLE"
    PRICE_ABOVE_3000 = "PRICE_ABOVE_3000"
    PRICE_MISSING = "PRICE_MISSING"
    FX_MISSING = "FX_MISSING"
    STALE_PRICE = "STALE_PRICE"
    STALE_FX = "STALE_FX"


@dataclass(frozen=True)
class FilterRule:
    rule_version: str
    threshold_jpy: Decimal
    max_price_age_days: int
    max_fx_age_seconds: int


# Mirrors the row seeded into market.price_filter_rules. The database is the
# source of truth; this is the default a job uses when it has not been told
# otherwise, and a test asserts the two agree.
DEFAULT_RULE = FilterRule(
    rule_version="price-filter-1.0.0",
    threshold_jpy=Decimal(3000),
    max_price_age_days=5,
    max_fx_age_seconds=345600,
)


@dataclass(frozen=True)
class FxObservation:
    rate: Decimal
    source_date: date
    observed_at: datetime
    available_at: datetime
    provider_id: str = "ecb"
    run_id: str | None = None


@dataclass(frozen=True)
class EligibilityResult:
    decision: EligibilityDecision
    rule_version: str

    price: Decimal | None = None
    price_currency: str | None = None
    price_basis: PriceBasis | None = None
    price_trade_date: date | None = None
    price_observed_at: datetime | None = None
    price_available_at: datetime | None = None
    price_age_days: int | None = None

    fx_rate: Decimal | None = None
    fx_source_date: date | None = None
    fx_observed_at: datetime | None = None
    fx_available_at: datetime | None = None
    fx_age_seconds: Decimal | None = None

    converted_jpy: Decimal | None = None
    reason: str | None = None

    @property
    def is_eligible(self) -> bool:
        return self.decision is EligibilityDecision.PRICE_ELIGIBLE


def evaluate(
    *,
    market_code: str,
    as_of_date: date,
    knowledge_cutoff: datetime,
    bar: CanonicalBar | None,
    fx: FxObservation | None = None,
    rule: FilterRule = DEFAULT_RULE,
) -> EligibilityResult:
    """Apply the filter to one security, or say why it could not be applied."""

    # ---------------------------------------------------------------- price
    if bar is None or bar.close is None:
        return EligibilityResult(
            EligibilityDecision.PRICE_MISSING, rule.rule_version,
            reason="no price at or before the cutoff",
        )

    if bar.available_at is not None and bar.available_at > knowledge_cutoff:
        # A caller that hands us a bar the cutoff could not know is a bug, and a
        # silent one: it would produce a plausible decision from future data.
        return EligibilityResult(
            EligibilityDecision.PRICE_MISSING, rule.rule_version,
            reason=f"the only price became available at {bar.available_at.isoformat()}, "
                   f"after the cutoff {knowledge_cutoff.isoformat()}",
        )

    if bar.close_basis is not PriceBasis.RAW:
        # The threshold is an absolute price in yen. Comparing it against an
        # adjusted close measures a share that is not the one being bought.
        return EligibilityResult(
            EligibilityDecision.PRICE_MISSING, rule.rule_version,
            price=bar.close, price_currency=bar.currency, price_basis=bar.close_basis,
            price_trade_date=bar.trade_date,
            reason=f"the close is {bar.close_basis}, not RAW; the 3,000 JPY test needs an as-traded price",
        )

    price_age_days = (as_of_date - bar.trade_date).days
    common = {
        "price": bar.close,
        "price_currency": bar.currency,
        "price_basis": bar.close_basis,
        "price_trade_date": bar.trade_date,
        "price_observed_at": bar.observed_at,
        "price_available_at": bar.available_at,
        "price_age_days": price_age_days,
    }

    if price_age_days < 0:
        return EligibilityResult(
            EligibilityDecision.PRICE_MISSING, rule.rule_version, **common,
            reason=f"the price is dated {bar.trade_date}, after the as-of date {as_of_date}",
        )

    if price_age_days > rule.max_price_age_days:
        return EligibilityResult(
            EligibilityDecision.STALE_PRICE, rule.rule_version, **common,
            reason=f"the newest price is {price_age_days} days old, over the {rule.max_price_age_days} day bound",
        )

    # ------------------------------------------------------------------- fx
    if market_code == MARKET_JP:
        converted = bar.close
    else:
        if fx is None:
            return EligibilityResult(
                EligibilityDecision.FX_MISSING, rule.rule_version, **common,
                reason="no USD/JPY observation at or before the cutoff",
            )
        if fx.available_at > knowledge_cutoff:
            return EligibilityResult(
                EligibilityDecision.FX_MISSING, rule.rule_version, **common,
                reason=f"the only FX rate became available at {fx.available_at.isoformat()}, "
                       f"after the cutoff {knowledge_cutoff.isoformat()}",
            )

        fx_age_seconds = Decimal((knowledge_cutoff - fx.available_at).total_seconds())
        common |= {
            "fx_rate": fx.rate,
            "fx_source_date": fx.source_date,
            "fx_observed_at": fx.observed_at,
            "fx_available_at": fx.available_at,
            "fx_age_seconds": fx_age_seconds,
        }

        if fx_age_seconds > rule.max_fx_age_seconds:
            return EligibilityResult(
                EligibilityDecision.STALE_FX, rule.rule_version, **common,
                reason=f"the newest FX rate is {fx_age_seconds} seconds old, "
                       f"over the {rule.max_fx_age_seconds} second bound",
            )

        converted = (bar.close * fx.rate).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)

    # ------------------------------------------------------------- the rule
    decision = (
        EligibilityDecision.PRICE_ELIGIBLE
        if converted <= rule.threshold_jpy
        else EligibilityDecision.PRICE_ABOVE_3000
    )
    return EligibilityResult(decision, rule.rule_version, converted_jpy=converted, **common)


def summarise(results: list[EligibilityResult]) -> dict[str, int]:
    """Counts by outcome. The failure outcomes are the ones worth watching."""

    summary = {decision.value.lower(): 0 for decision in EligibilityDecision}
    summary["evaluated"] = len(results)
    for result in results:
        summary[result.decision.value.lower()] += 1
    return summary
