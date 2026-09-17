"""Stage 1: the indicators, the feature engine, and Routes A-H.

Indicators are checked against values worked out by hand rather than against
another implementation, because two implementations of the same mistake agree.
The feature engine is checked mostly for what it refuses. The routes are checked
for firing, for declining on absent data, and for being ORed.
"""

from __future__ import annotations

import math
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from surge.features import indicators
from surge.features.engine import WARMUP_BARS, FeatureError, compute_features
from surge.licensing import AvailabilityBasis
from surge.market.models import CanonicalAction, CanonicalBar, CorporateActionType, PriceBasis
from surge.market.series import build_comparable_series
from surge.routes.engine import ROUTE_DEFINITIONS, evaluate_routes, route_summary

T0 = datetime(2026, 9, 16, 6, 0, tzinfo=UTC)


# --------------------------------------------------------------- indicators
def test_sma_and_ema_on_a_series_worked_out_by_hand():
    values = [1, 2, 3, 4, 5]
    assert indicators.sma(values, 5) == 3.0
    assert indicators.sma(values, 2) == 4.5

    # EMA(3) seeded with the SMA of the first three: (1+2+3)/3 = 2
    # then 4 -> (4-2)*0.5+2 = 3 ; then 5 -> (5-3)*0.5+3 = 4
    assert indicators.ema(values, 3) == pytest.approx(4.0)


def test_a_window_longer_than_the_history_is_none_not_a_short_window():
    assert indicators.sma([1, 2, 3], 5) is None
    assert indicators.ema([1, 2, 3], 5) is None
    assert indicators.rsi([1, 2, 3], 14) is None
    assert indicators.atr([1, 2], [1, 2], [1, 2], 14) is None


def test_a_hole_in_the_window_is_refused_not_interpolated():
    assert indicators.sma([1, None, 3], 3) is None
    assert indicators.rolling_max([1, None, 3], 3) is None


def test_rsi_matches_a_hand_computation():
    """closes 10, 11, 10.5, 11.5, 11 with window 2.

    gains 1, 0, 1, 0 and losses 0, .5, 0, .5.
    seed  avg_gain .5   avg_loss .25
    then  .75/.125, then .375/.3125 -> RS 1.2 -> 100 - 100/2.2
    """

    value = indicators.rsi([10, 11, 10.5, 11.5, 11], window=2)
    assert value == pytest.approx(100 - 100 / 2.2, rel=1e-9)


def test_rsi_is_100_when_nothing_fell_and_0_when_nothing_rose():
    assert indicators.rsi([1, 2, 3, 4, 5], window=2) == 100.0
    assert indicators.rsi([5, 4, 3, 2, 1], window=2) == 0.0


def test_atr_matches_a_hand_computation():
    """H 10,11,12  L 9,10,11  C 9.5,10.5,11.5, window 2.

    TR after the first bar is 1.5 and 1.5, so the seeded average is 1.5.
    """

    value = indicators.atr([10, 11, 12], [9, 10, 11], [9.5, 10.5, 11.5], window=2)
    assert value == pytest.approx(1.5)


def test_true_range_uses_the_previous_close_not_just_the_bar():
    # a gap up: the range from the previous close is wider than the bar itself
    ranges = indicators.true_ranges([10, 20], [9, 19], [9.5, 19.5])
    assert ranges[1] == pytest.approx(20 - 9.5)


def test_bollinger_matches_a_hand_computation():
    """1..20: mean 10.5, population variance (n^2-1)/12 = 33.25."""

    closes = list(range(1, 21))
    mid, upper, lower, width, percent_b = indicators.bollinger(closes, window=20)
    sigma = math.sqrt(33.25)

    assert mid == pytest.approx(10.5)
    assert upper == pytest.approx(10.5 + 2 * sigma)
    assert lower == pytest.approx(10.5 - 2 * sigma)
    assert width == pytest.approx(4 * sigma / 10.5)
    assert percent_b == pytest.approx((20 - lower) / (upper - lower))


def test_realized_volatility_of_a_flat_series_is_zero():
    assert indicators.realized_volatility([100] * 25, 20) == pytest.approx(0.0)


def test_relative_volume_excludes_today_from_its_own_baseline():
    """Otherwise a huge day dilutes the very average it is being measured against."""

    volumes = [100] * 20 + [300]
    assert indicators.relative_volume(volumes, 20) == pytest.approx(3.0)


def test_candle_shape_on_a_hammer():
    body, upper, lower, clv = indicators.candle_shape(open_=95, high=100, low=80, close=98)
    assert body == pytest.approx(3 / 20 * 100)
    assert upper == pytest.approx(2 / 20 * 100)
    assert lower == pytest.approx(15 / 20 * 100)
    assert clv == pytest.approx(((98 - 80) - (100 - 98)) / 20)


def test_a_bar_with_no_range_has_no_shape_rather_than_a_zero_shape():
    """A limit move with one print. Undefined is not the same as flat."""

    assert indicators.candle_shape(100, 100, 100, 100) == (None, None, None, None)


def test_adx_needs_roughly_twice_the_window_before_it_exists():
    highs = [10 + i for i in range(20)]
    lows = [9 + i for i in range(20)]
    closes = [9.5 + i for i in range(20)]
    adx, plus_di, minus_di = indicators.adx_dmi(highs, lows, closes, window=14)

    assert plus_di is not None and minus_di is not None
    assert adx is None, "the DX series is not long enough to smooth yet"

    long_highs = [10 + i for i in range(40)]
    long_lows = [9 + i for i in range(40)]
    long_closes = [9.5 + i for i in range(40)]
    adx_long, _, _ = indicators.adx_dmi(long_highs, long_lows, long_closes, window=14)
    assert adx_long is not None


def test_a_steady_uptrend_has_a_positive_di_above_the_negative_one():
    highs = [10 + i for i in range(40)]
    lows = [9 + i for i in range(40)]
    closes = [9.5 + i for i in range(40)]
    _, plus_di, minus_di = indicators.adx_dmi(highs, lows, closes, 14)
    assert plus_di > minus_di


def test_macd_histogram_is_the_line_minus_its_signal():
    closes = [100 + math.sin(i / 5) * 10 for i in range(80)]
    macd_value, signal, hist = indicators.macd(closes)
    assert hist == pytest.approx(macd_value - signal)


def test_consecutive_up_days_counts_only_the_current_run():
    assert indicators.consecutive_up_days([1, 2, 3, 2, 3, 4]) == 2
    assert indicators.consecutive_up_days([5, 4, 3]) == 0


# ------------------------------------------------------------ the engine
def _bars(
    closes: list[float],
    *,
    symbol: str = "13010",
    market: str = "JP",
    start: date = date(2026, 1, 5),
    volumes: list[float] | None = None,
    turnovers: list[float] | None = None,
    highs: list[float] | None = None,
    lows: list[float] | None = None,
    opens: list[float] | None = None,
) -> list[CanonicalBar]:
    bars = []
    day = start
    for index, close in enumerate(closes):
        while day.weekday() >= 5:
            day += timedelta(days=1)
        bars.append(
            CanonicalBar(
                provider_id="test", dataset_key="TEST_EOD", market_code=market,
                native_symbol=symbol, trade_date=day, currency="JPY" if market == "JP" else "USD",
                open=Decimal(str(opens[index] if opens else close)),
                high=Decimal(str(highs[index] if highs else close * 1.01)),
                low=Decimal(str(lows[index] if lows else close * 0.99)),
                close=Decimal(str(close)),
                volume=Decimal(str(volumes[index] if volumes else 1000)),
                turnover=None if turnovers is None else Decimal(str(turnovers[index])),
                open_basis=PriceBasis.RAW, high_basis=PriceBasis.RAW, low_basis=PriceBasis.RAW,
                close_basis=PriceBasis.RAW, volume_basis=PriceBasis.RAW,
                observed_at=T0, available_at=T0, availability_basis=AvailabilityBasis.OBSERVED_NOW,
            )
        )
        day += timedelta(days=1)
    return bars


def _features(bars: list[CanonicalBar], actions: list[CanonicalAction] | None = None, as_of: date | None = None):
    as_of = as_of or bars[-1].trade_date
    series = build_comparable_series(bars, actions or [], as_of=as_of)
    return compute_features(series), series


def test_features_are_computed_and_the_warmup_flag_is_honest():
    features, _ = _features(_bars([100 + i for i in range(40)]))

    assert features.bars_available == 40
    assert features.warmup_satisfied is False, "40 bars is short of the 75-bar longest window"
    assert features.sma_25 is not None
    assert features.sma_75 is None, "a 75-day average of 40 bars would be a different statistic"

    long_features, _ = _features(_bars([100 + i for i in range(WARMUP_BARS + 5)]))
    assert long_features.warmup_satisfied is True
    assert long_features.sma_75 is not None


def test_a_broken_series_produces_no_features_at_all():
    """A partially wrong feature row is worse than none."""

    bars = _bars([100, 100, 50])
    broken = CanonicalAction(
        provider_id="test", dataset_key="TEST_ACTIONS", market_code="JP", native_symbol="13010",
        action_type=CorporateActionType.SPLIT, ex_date=bars[-1].trade_date,
    )
    series = build_comparable_series(bars, [broken], as_of=bars[-1].trade_date)
    with pytest.raises(FeatureError, match="refusing to produce features"):
        compute_features(series)


def test_a_split_does_not_show_up_as_a_return():
    """The point of the comparable series, asserted end to end."""

    closes = [1000.0] * 30 + [500.0]
    bars = _bars(closes)
    ex_date = bars[-1].trade_date
    action = CanonicalAction(
        provider_id="test", dataset_key="TEST_ACTIONS", market_code="JP", native_symbol="13010",
        action_type=CorporateActionType.SPLIT, ex_date=ex_date,
        split_to=Decimal(2), split_from=Decimal(1),
    )

    with_split, _ = _features(bars, [action])
    assert with_split.ret_1d == pytest.approx(0.0)

    # and without the action recorded, the same prices look like a 50% collapse
    without_split, _ = _features(bars, [])
    assert without_split.ret_1d == pytest.approx(-50.0)


def test_future_bars_cannot_move_a_feature_row_forward_in_time():
    """The leakage guard: an as-of date means an as-of date."""

    bars = _bars([100 + i for i in range(60)])
    cut = bars[39].trade_date

    truncated, _ = _features(bars[:40], as_of=cut)
    with_future, _ = _features(bars, as_of=cut)

    assert with_future.trade_date == cut
    assert with_future.close == truncated.close
    assert with_future.ret_20d == pytest.approx(truncated.ret_20d)
    assert with_future.bars_available == truncated.bars_available


def test_vwap_exists_only_where_the_provider_gives_turnover():
    closes = [100.0] * 30
    with_turnover, _ = _features(
        _bars(closes, volumes=[1000] * 30, turnovers=[101000] * 30)
    )
    assert with_turnover.vwap_day == pytest.approx(101.0)
    assert with_turnover.vwap_source == "TURNOVER_OVER_VOLUME"

    without, _ = _features(_bars(closes, volumes=[1000] * 30))
    assert without.vwap_day is None
    assert without.vwap_source is None


def test_distance_from_the_high_is_positive_when_below_it():
    bars = _bars([100.0] * 20 + [90.0], highs=[100.0] * 20 + [90.0])
    features, _ = _features(bars)
    assert features.dist_from_high_20d_pct == pytest.approx(10.0)
    assert features.days_since_high_20d == 1


# ------------------------------------------------------------------ routes
def _route_features(**overrides):
    """A feature row with everything present, so a test can vary one thing."""

    base, series = _features(_bars([100 + i * 0.1 for i in range(WARMUP_BARS + 5)]))
    from dataclasses import replace  # noqa: PLC0415

    return replace(base, **overrides), series


def test_route_a_fires_near_a_high_on_expanding_volume():
    features, series = _route_features(dist_from_high_20d_pct=1.0, rvol_20d=1.5)
    result = evaluate_routes(features, series=series)

    assert "A" in result.discovery_routes
    assert result.route_evidence["A"]["dist_from_high_20d_pct"] == 1.0


def test_route_a_declines_when_the_volume_is_not_there():
    features, series = _route_features(dist_from_high_20d_pct=1.0, rvol_20d=0.5)
    assert "A" not in evaluate_routes(features, series=series).discovery_routes


def test_no_route_fires_on_a_missing_feature():
    """Firing on absence selects for thin data rather than for setups."""

    features, series = _route_features(
        dist_from_high_20d_pct=None, rvol_20d=None, ret_1d=None, ret_5d=None, ret_20d=None,
        range_contraction_ratio=None, bb_width_20=None, lower_wick_pct=None,
        close_location_value=None, turnover_avg_20d=None, turnover_rvol=None,
        close_vs_sma25_pct=None, rvol_5d=None,
    )
    result = evaluate_routes(features, series=series)
    assert result.discovery_routes == []
    assert result.is_candidate is False


def test_route_c_wants_volume_without_a_price_move_yet():
    fires, series = _route_features(rvol_20d=3.0, ret_1d=1.0)
    assert "C" in evaluate_routes(fires, series=series).discovery_routes

    already_moved, series = _route_features(rvol_20d=3.0, ret_1d=9.0)
    assert "C" not in evaluate_routes(already_moved, series=series).discovery_routes


def test_route_d_wants_both_a_narrow_range_and_narrow_bands():
    fires, series = _route_features(range_contraction_ratio=0.4, bb_width_20=0.05)
    assert "D" in evaluate_routes(fires, series=series).discovery_routes

    one_only, series = _route_features(range_contraction_ratio=0.4, bb_width_20=0.30)
    assert "D" not in evaluate_routes(one_only, series=series).discovery_routes


def test_route_e_wants_a_long_lower_wick_after_a_fall():
    fires, series = _route_features(lower_wick_pct=55.0, close_location_value=0.8, ret_5d=-8.0)
    assert "E" in evaluate_routes(fires, series=series).discovery_routes

    after_a_rise, series = _route_features(lower_wick_pct=55.0, close_location_value=0.8, ret_5d=+8.0)
    assert "E" not in evaluate_routes(after_a_rise, series=series).discovery_routes


def test_route_f_applies_a_different_turnover_ceiling_per_market():
    """Turnover is money in the security's own currency."""

    jp, series = _route_features(market_code="JP", turnover_avg_20d=4e8, turnover_rvol=4.0)
    assert "F" in evaluate_routes(jp, series=series).discovery_routes

    # the same number of dollars is a hundred times more money
    us, series = _route_features(market_code="US", turnover_avg_20d=4e8, turnover_rvol=4.0)
    assert "F" not in evaluate_routes(us, series=series).discovery_routes

    small_us, series = _route_features(market_code="US", turnover_avg_20d=3e6, turnover_rvol=4.0)
    assert "F" in evaluate_routes(small_us, series=series).discovery_routes


def test_route_f_declines_where_the_provider_gives_no_turnover():
    features, series = _route_features(turnover_avg_20d=None, turnover_rvol=None)
    assert "F" not in evaluate_routes(features, series=series).discovery_routes


def test_route_g_wants_a_fresh_move_not_an_extended_one():
    fresh, series = _route_features(ret_5d=8.0, ret_20d=12.0, rvol_20d=2.0)
    assert "G" in evaluate_routes(fresh, series=series).discovery_routes

    extended, series = _route_features(ret_5d=8.0, ret_20d=60.0, rvol_20d=2.0)
    assert "G" not in evaluate_routes(extended, series=series).discovery_routes


def test_route_h_wants_a_pullback_inside_a_band_not_a_collapse():
    pullback, series = _route_features(close_vs_sma25_pct=5.0, dist_from_high_20d_pct=8.0, rvol_5d=0.7)
    assert "H" in evaluate_routes(pullback, series=series).discovery_routes

    collapse, series = _route_features(close_vs_sma25_pct=5.0, dist_from_high_20d_pct=40.0, rvol_5d=0.7)
    assert "H" not in evaluate_routes(collapse, series=series).discovery_routes


def test_routes_are_ored_and_a_candidate_keeps_every_route_that_found_it():
    features, series = _route_features(
        dist_from_high_20d_pct=1.0, rvol_20d=3.0, ret_1d=1.0, ret_5d=8.0, ret_20d=12.0
    )
    result = evaluate_routes(features, series=series)

    assert set(result.discovery_routes) >= {"A", "C", "G"}
    assert result.route_count == len(result.discovery_routes)
    assert set(result.route_evidence) == set(result.discovery_routes)


def test_a_route_does_not_run_without_enough_history():
    short, series = _features(_bars([100 + i for i in range(12)]))
    result = evaluate_routes(short, series=series)
    # E needs only 10 bars; A, B, D, H need 25-30 and must not fire on 12
    assert "A" not in result.discovery_routes
    assert "D" not in result.discovery_routes


def test_route_b_needs_the_price_to_have_been_below_the_average_first():
    """A reclaim has a before. Without one it is just an uptrend."""

    rising = _bars([100 + i for i in range(60)])
    features, series = _features(rising)
    assert "B" not in evaluate_routes(features, series=series).discovery_routes

    # fall below the average, then recover through it
    dip = [100.0] * 30 + [80.0] * 8 + [95.0, 103.0, 108.0]
    features, series = _features(_bars(dip))
    assert "B" in evaluate_routes(features, series=series).discovery_routes


def test_the_route_summary_counts_each_route_and_the_overlap():
    features, series = _route_features(
        dist_from_high_20d_pct=1.0, rvol_20d=3.0, ret_1d=1.0, ret_5d=8.0, ret_20d=12.0
    )
    quiet, series2 = _route_features(
        dist_from_high_20d_pct=30.0, rvol_20d=0.2, ret_1d=0.0, ret_5d=0.0, ret_20d=0.0,
        range_contraction_ratio=0.9, bb_width_20=0.5, lower_wick_pct=1.0,
        close_location_value=0.1, turnover_rvol=0.1, close_vs_sma25_pct=-20.0, rvol_5d=5.0,
    )
    summary = route_summary([evaluate_routes(features, series=series), evaluate_routes(quiet, series=series2)])

    assert summary["candidates"] == 1
    assert summary["multi_route"] == 1
    assert summary["route_a"] == 1


def test_every_route_has_a_definition_and_a_function():
    from surge.routes.engine import ROUTE_FUNCTIONS  # noqa: PLC0415

    assert set(ROUTE_DEFINITIONS) == set("ABCDEFGH")
    assert set(ROUTE_FUNCTIONS) == set(ROUTE_DEFINITIONS)
    for code, definition in ROUTE_DEFINITIONS.items():
        assert definition.code == code
        assert definition.min_bars > 0
        assert definition.thresholds, f"route {code} fires on nothing"
