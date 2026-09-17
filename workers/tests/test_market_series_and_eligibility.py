"""The comparable series and the 3,000 JPY filter.

These are the two places where a quiet error becomes an expensive one: a split
read as a 50% fall, or a threshold applied to a price the system could not have
known. Both are tested for what they refuse as much as for what they compute.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from surge.licensing import AvailabilityBasis
from surge.market.eligibility import (
    DEFAULT_RULE,
    EligibilityDecision,
    FilterRule,
    FxObservation,
    evaluate,
    summarise,
)
from surge.market.models import CanonicalAction, CanonicalBar, CorporateActionType, PriceBasis
from surge.market.series import SeriesError, build_comparable_series, latest_price

T0 = datetime(2026, 9, 16, 6, 0, tzinfo=UTC)


def bar(
    day: date,
    close: str,
    *,
    symbol: str = "13010",
    market: str = "JP",
    currency: str = "JPY",
    volume: str | None = "1000",
    turnover: str | None = None,
    volume_basis: PriceBasis = PriceBasis.RAW,
    available_at: datetime | None = T0,
    high: str | None = None,
    low: str | None = None,
    open_: str | None = None,
    close_basis: PriceBasis = PriceBasis.RAW,
) -> CanonicalBar:
    price = Decimal(close)
    return CanonicalBar(
        provider_id="test",
        dataset_key="TEST_EOD",
        market_code=market,
        native_symbol=symbol,
        trade_date=day,
        currency=currency,
        open=Decimal(open_) if open_ else price,
        high=Decimal(high) if high else price,
        low=Decimal(low) if low else price,
        close=price,
        volume=None if volume is None else Decimal(volume),
        turnover=None if turnover is None else Decimal(turnover),
        open_basis=PriceBasis.RAW,
        high_basis=PriceBasis.RAW,
        low_basis=PriceBasis.RAW,
        close_basis=close_basis,
        volume_basis=volume_basis,
        observed_at=T0,
        available_at=available_at,
        availability_basis=AvailabilityBasis.OBSERVED_NOW,
    )


def split(day: date, to: str, frm: str = "1", symbol: str = "13010") -> CanonicalAction:
    return CanonicalAction(
        provider_id="test",
        dataset_key="TEST_ACTIONS",
        market_code="JP",
        native_symbol=symbol,
        action_type=CorporateActionType.SPLIT,
        ex_date=day,
        split_to=Decimal(to),
        split_from=Decimal(frm),
    )


# --------------------------------------------------------------- the series
def test_a_two_for_one_split_is_not_a_fifty_percent_fall():
    """The whole reason this module exists."""

    bars = [
        bar(date(2026, 9, 14), "1000"),
        bar(date(2026, 9, 15), "1000"),
        bar(date(2026, 9, 16), "500"),  # ex-split day: the price halves
    ]
    actions = [split(date(2026, 9, 16), "2")]

    series = build_comparable_series(bars, actions, as_of=date(2026, 9, 16))

    # restated into today's share count, nothing happened
    assert [b.close for b in series.bars] == [Decimal(500), Decimal(500), Decimal(500)]
    assert series.basis == "SPLIT_ADJUSTED_TO_AS_OF"
    assert len(series.applied_actions) == 1


def test_volume_moves_the_other_way():
    bars = [bar(date(2026, 9, 15), "1000", volume="100"), bar(date(2026, 9, 16), "500", volume="200")]
    series = build_comparable_series(bars, [split(date(2026, 9, 16), "2")], as_of=date(2026, 9, 16))

    assert [b.volume for b in series.bars] == [Decimal(200), Decimal(200)]


def test_turnover_is_money_and_a_split_does_not_change_it():
    bars = [
        bar(date(2026, 9, 15), "1000", volume="100", turnover="100000"),
        bar(date(2026, 9, 16), "500", volume="200", turnover="100000"),
    ]
    series = build_comparable_series(bars, [split(date(2026, 9, 16), "2")], as_of=date(2026, 9, 16))

    assert [b.turnover for b in series.bars] == [Decimal(100000), Decimal(100000)]


def test_a_reverse_split_restates_the_other_way():
    bars = [bar(date(2026, 9, 15), "10"), bar(date(2026, 9, 16), "500")]
    reverse = CanonicalAction(
        provider_id="test", dataset_key="TEST_ACTIONS", market_code="JP", native_symbol="13010",
        action_type=CorporateActionType.REVERSE_SPLIT, ex_date=date(2026, 9, 16),
        split_to=Decimal(1), split_from=Decimal(50),
    )
    series = build_comparable_series(bars, [reverse], as_of=date(2026, 9, 16))

    assert series.bars[0].close == Decimal(500)  # 10 / (1/50)
    assert series.bars[1].close == Decimal(500)


def test_a_split_after_the_as_of_date_is_not_applied():
    """A series for March must not know about June."""

    bars = [bar(date(2026, 3, 2), "1000"), bar(date(2026, 3, 3), "1000")]
    future = [split(date(2026, 6, 1), "2")]

    series = build_comparable_series(bars, future, as_of=date(2026, 3, 3))
    assert [b.close for b in series.bars] == [Decimal(1000), Decimal(1000)]
    assert series.applied_actions == []


def test_a_cash_dividend_does_not_restate_anything():
    """It changes total return, not the share count, and this project excludes it."""

    bars = [bar(date(2026, 9, 15), "1000"), bar(date(2026, 9, 16), "990")]
    dividend = CanonicalAction(
        provider_id="test", dataset_key="TEST_ACTIONS", market_code="JP", native_symbol="13010",
        action_type=CorporateActionType.CASH_DIVIDEND, ex_date=date(2026, 9, 16),
        amount=Decimal(10),
    )
    series = build_comparable_series(bars, [dividend], as_of=date(2026, 9, 16))
    assert [b.close for b in series.bars] == [Decimal(1000), Decimal(990)]


def test_an_unreadable_split_breaks_the_series_rather_than_defaulting_to_one():
    broken = CanonicalAction(
        provider_id="test", dataset_key="TEST_ACTIONS", market_code="JP", native_symbol="13010",
        action_type=CorporateActionType.SPLIT, ex_date=date(2026, 9, 16),
        split_to=None, split_from=None, adjustment_factor=None,
    )
    series = build_comparable_series(
        [bar(date(2026, 9, 15), "1000"), bar(date(2026, 9, 16), "500")], [broken], as_of=date(2026, 9, 16)
    )

    assert series.is_broken is True
    assert series.unusable_actions
    assert "unreliable" in " ".join(series.notes)


def test_a_jquants_style_adjustment_factor_is_inverted_correctly():
    """J-Quants publishes the PRICE factor: 0.5 on a two-for-one."""

    action = CanonicalAction(
        provider_id="jquants", dataset_key="JQ_EQ_BARS_DAILY", market_code="JP", native_symbol="13010",
        action_type=CorporateActionType.SPLIT, ex_date=date(2026, 9, 16),
        adjustment_factor=Decimal("0.5"),
    )
    assert action.share_multiplier == Decimal(2)


def test_a_vendor_adjusted_volume_is_not_adjusted_twice():
    bars = [
        bar(date(2026, 9, 15), "1000", volume="200", volume_basis=PriceBasis.SPLIT_ADJUSTED),
        bar(date(2026, 9, 16), "500", volume="200", volume_basis=PriceBasis.SPLIT_ADJUSTED),
    ]
    series = build_comparable_series(bars, [split(date(2026, 9, 16), "2")], as_of=date(2026, 9, 16))

    assert [b.volume for b in series.bars] == [Decimal(200), Decimal(200)]
    assert series.volume_comparable is True


def test_a_vendor_adjusted_volume_is_dropped_when_a_later_split_makes_it_unrestatable():
    """The vendor adjusted to ITS fetch date, not to our as-of date.

    Un-doing that needs the splits between the two, which is future information
    at the as-of date. Dropping the volume is the only honest answer.
    """

    bars = [
        bar(date(2026, 3, 2), "1000", volume="200", volume_basis=PriceBasis.SPLIT_ADJUSTED),
        bar(date(2026, 3, 3), "1000", volume="200", volume_basis=PriceBasis.SPLIT_ADJUSTED),
    ]
    series = build_comparable_series(bars, [split(date(2026, 6, 1), "2")], as_of=date(2026, 3, 3))

    assert series.volume_comparable is False
    assert all(b.volume is None for b in series.bars)
    assert any("future information" in note for note in series.notes)


def test_a_series_refuses_adjusted_prices():
    adjusted = bar(date(2026, 9, 16), "500", close_basis=PriceBasis.SPLIT_ADJUSTED)
    with pytest.raises(SeriesError, match="must be built from unadjusted prices"):
        build_comparable_series([adjusted], [], as_of=date(2026, 9, 16))


def test_a_series_refuses_two_securities():
    with pytest.raises(SeriesError, match="must be one security"):
        build_comparable_series(
            [bar(date(2026, 9, 16), "100", symbol="A"), bar(date(2026, 9, 16), "100", symbol="B")],
            [],
            as_of=date(2026, 9, 16),
        )


def test_latest_price_never_reaches_past_the_cutoff():
    bars = [bar(date(2026, 9, 15), "100"), bar(date(2026, 9, 16), "200")]
    assert latest_price(bars, as_of=date(2026, 9, 15)).close == Decimal(100)
    assert latest_price(bars, as_of=date(2026, 9, 14)) is None


# ----------------------------------------------------------- the 3,000 filter
def test_a_cheap_japanese_share_is_eligible():
    result = evaluate(
        market_code="JP", as_of_date=date(2026, 9, 16), knowledge_cutoff=T0 + timedelta(hours=12),
        bar=bar(date(2026, 9, 16), "2999"),
    )
    assert result.decision is EligibilityDecision.PRICE_ELIGIBLE
    assert result.converted_jpy == Decimal(2999)
    assert result.fx_rate is None


def test_the_boundary_is_inclusive():
    for price, expected in (("3000", EligibilityDecision.PRICE_ELIGIBLE),
                            ("3000.01", EligibilityDecision.PRICE_ABOVE_3000)):
        result = evaluate(
            market_code="JP", as_of_date=date(2026, 9, 16), knowledge_cutoff=T0 + timedelta(hours=12),
            bar=bar(date(2026, 9, 16), price),
        )
        assert result.decision is expected, price


def test_a_us_share_is_converted_at_the_rate_the_cutoff_could_know():
    cutoff = T0 + timedelta(hours=12)
    fx = FxObservation(
        rate=Decimal("155.0"), source_date=date(2026, 9, 16), observed_at=T0, available_at=T0
    )
    result = evaluate(
        market_code="US", as_of_date=date(2026, 9, 16), knowledge_cutoff=cutoff,
        bar=bar(date(2026, 9, 16), "19", market="US", currency="USD"), fx=fx,
    )
    assert result.decision is EligibilityDecision.PRICE_ELIGIBLE
    assert result.converted_jpy == Decimal("2945.000000")
    assert result.fx_age_seconds == Decimal(12 * 3600)


def test_a_us_share_over_the_threshold_after_conversion():
    fx = FxObservation(Decimal("155.0"), date(2026, 9, 16), T0, T0)
    result = evaluate(
        market_code="US", as_of_date=date(2026, 9, 16), knowledge_cutoff=T0,
        bar=bar(date(2026, 9, 16), "20", market="US", currency="USD"), fx=fx,
    )
    assert result.decision is EligibilityDecision.PRICE_ABOVE_3000
    assert result.converted_jpy == Decimal("3100.000000")


def test_no_price_is_not_the_same_as_too_expensive():
    result = evaluate(
        market_code="JP", as_of_date=date(2026, 9, 16), knowledge_cutoff=T0, bar=None
    )
    assert result.decision is EligibilityDecision.PRICE_MISSING
    assert result.converted_jpy is None


def test_a_price_the_cutoff_could_not_know_is_refused():
    """The leak the whole bitemporal design exists to prevent."""

    future_bar = bar(date(2026, 9, 16), "100", available_at=T0 + timedelta(days=1))
    result = evaluate(
        market_code="JP", as_of_date=date(2026, 9, 16), knowledge_cutoff=T0, bar=future_bar
    )
    assert result.decision is EligibilityDecision.PRICE_MISSING
    assert "after the cutoff" in result.reason


def test_an_adjusted_close_is_not_an_as_traded_price():
    adjusted = bar(date(2026, 9, 16), "100", close_basis=PriceBasis.SPLIT_AND_DIVIDEND_ADJUSTED)
    result = evaluate(
        market_code="JP", as_of_date=date(2026, 9, 16), knowledge_cutoff=T0, bar=adjusted
    )
    assert result.decision is EligibilityDecision.PRICE_MISSING
    assert "as-traded price" in result.reason


def test_a_price_older_than_the_rule_allows_is_stale_not_eligible():
    old = bar(date(2026, 9, 1), "100")
    result = evaluate(
        market_code="JP", as_of_date=date(2026, 9, 16), knowledge_cutoff=T0, bar=old
    )
    assert result.decision is EligibilityDecision.STALE_PRICE
    assert result.price_age_days == 15


def test_a_price_within_the_bound_is_used_even_though_it_is_not_today():
    """A security that did not trade on Friday keeps Thursday's close."""

    result = evaluate(
        market_code="JP", as_of_date=date(2026, 9, 16), knowledge_cutoff=T0,
        bar=bar(date(2026, 9, 13), "100"),
    )
    assert result.decision is EligibilityDecision.PRICE_ELIGIBLE
    assert result.price_age_days == 3


def test_a_us_share_with_no_fx_is_unmeasured_not_ineligible():
    result = evaluate(
        market_code="US", as_of_date=date(2026, 9, 16), knowledge_cutoff=T0,
        bar=bar(date(2026, 9, 16), "19", market="US", currency="USD"), fx=None,
    )
    assert result.decision is EligibilityDecision.FX_MISSING


def test_an_fx_rate_older_than_the_rule_allows_is_stale():
    stale = FxObservation(
        Decimal("155.0"), date(2026, 9, 5), T0 - timedelta(days=10), T0 - timedelta(days=10)
    )
    result = evaluate(
        market_code="US", as_of_date=date(2026, 9, 16), knowledge_cutoff=T0,
        bar=bar(date(2026, 9, 16), "19", market="US", currency="USD"), fx=stale,
    )
    assert result.decision is EligibilityDecision.STALE_FX
    assert result.fx_age_seconds == Decimal(10 * 86400)


def test_a_long_weekend_rate_is_used_and_its_age_recorded():
    """Three days old is legitimate for a reference rate. It is not same-day."""

    friday = T0 - timedelta(days=3)
    fx = FxObservation(Decimal("155.0"), date(2026, 9, 13), friday, friday)
    result = evaluate(
        market_code="US", as_of_date=date(2026, 9, 16), knowledge_cutoff=T0,
        bar=bar(date(2026, 9, 16), "19", market="US", currency="USD"), fx=fx,
    )
    assert result.decision is EligibilityDecision.PRICE_ELIGIBLE
    assert result.fx_source_date == date(2026, 9, 13)
    assert result.fx_age_seconds == Decimal(3 * 86400)


def test_a_japanese_share_never_reports_an_fx_outcome():
    result = evaluate(
        market_code="JP", as_of_date=date(2026, 9, 16), knowledge_cutoff=T0,
        bar=bar(date(2026, 9, 16), "100"), fx=None,
    )
    assert result.decision is EligibilityDecision.PRICE_ELIGIBLE


def test_a_price_dated_after_the_as_of_date_is_refused():
    result = evaluate(
        market_code="JP", as_of_date=date(2026, 9, 15), knowledge_cutoff=T0 + timedelta(days=5),
        bar=bar(date(2026, 9, 16), "100"),
    )
    assert result.decision is EligibilityDecision.PRICE_MISSING
    assert "after the as-of date" in result.reason


def test_the_rule_is_versioned_and_changing_it_changes_the_answer():
    tighter = FilterRule("price-filter-test", Decimal(1000), 5, 345600)
    cheap = bar(date(2026, 9, 16), "2000")

    assert evaluate(
        market_code="JP", as_of_date=date(2026, 9, 16), knowledge_cutoff=T0, bar=cheap
    ).decision is EligibilityDecision.PRICE_ELIGIBLE
    result = evaluate(
        market_code="JP", as_of_date=date(2026, 9, 16), knowledge_cutoff=T0, bar=cheap, rule=tighter
    )
    assert result.decision is EligibilityDecision.PRICE_ABOVE_3000
    assert result.rule_version == "price-filter-test"


def test_the_default_rule_is_the_projects_standing_limit():
    assert DEFAULT_RULE.threshold_jpy == Decimal(3000)
    assert DEFAULT_RULE.rule_version == "price-filter-1.0.0"


def test_the_summary_counts_every_outcome():
    results = [
        evaluate(market_code="JP", as_of_date=date(2026, 9, 16), knowledge_cutoff=T0,
                 bar=bar(date(2026, 9, 16), "100")),
        evaluate(market_code="JP", as_of_date=date(2026, 9, 16), knowledge_cutoff=T0,
                 bar=bar(date(2026, 9, 16), "5000")),
        evaluate(market_code="JP", as_of_date=date(2026, 9, 16), knowledge_cutoff=T0, bar=None),
    ]
    summary = summarise(results)
    assert summary["evaluated"] == 3
    assert summary["price_eligible"] == 1
    assert summary["price_above_3000"] == 1
    assert summary["price_missing"] == 1
