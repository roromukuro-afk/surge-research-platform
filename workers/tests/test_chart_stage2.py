"""Phase 6: Stage 2 measurements, concept matching and price obstacles.

The measurements are checked for what they refuse as much as for what they
compute. A null that means "not measurable" and a null that means "nobody looked"
are the same value, and the tests here are mostly about keeping them apart.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from surge.chart import (
    ObstacleKind,
    ObstacleMisuse,
    Stage2Assessment,
    assess,
    find_obstacles,
    mark_weakened,
    match_concepts,
)
from surge.licensing import AvailabilityBasis
from surge.market.models import CanonicalBar, PriceBasis
from surge.market.series import ComparableSeries, build_comparable_series

T0 = datetime(2026, 9, 16, 6, 0, tzinfo=UTC)


def _bars(
    closes,
    *,
    symbol="13010",
    market="JP",
    start=date(2026, 1, 5),
    volumes=None,
    highs=None,
    lows=None,
    opens=None,
):
    bars = []
    day = start
    for index, close in enumerate(closes):
        while day.weekday() >= 5:
            day += timedelta(days=1)
        bars.append(
            CanonicalBar(
                provider_id="test",
                dataset_key="TEST_EOD",
                market_code=market,
                native_symbol=symbol,
                trade_date=day,
                currency="JPY" if market == "JP" else "USD",
                open=Decimal(str(opens[index] if opens else close)),
                high=Decimal(str(highs[index] if highs else close * 1.01)),
                low=Decimal(str(lows[index] if lows else close * 0.99)),
                close=Decimal(str(close)),
                volume=Decimal(str(volumes[index] if volumes else 1000)),
                open_basis=PriceBasis.RAW,
                high_basis=PriceBasis.RAW,
                low_basis=PriceBasis.RAW,
                close_basis=PriceBasis.RAW,
                volume_basis=PriceBasis.RAW,
                observed_at=T0,
                available_at=T0,
                availability_basis=AvailabilityBasis.OBSERVED_NOW,
            )
        )
        day += timedelta(days=1)
    return bars


def _series(closes, **kwargs):
    bars = _bars(closes, **kwargs)
    return build_comparable_series(bars, [], as_of=bars[-1].trade_date)


def _assess(closes, **kwargs):
    return assess(_series(closes, **kwargs), security_id="sec-1")


# ------------------------------------------------------------------ measurement


def test_an_empty_series_reports_a_gap_rather_than_zeroes():
    """build_comparable_series refuses to produce one, so this is belt and braces.

    A series with no bars can still arrive from a database read or a stub, and
    the difference between "measured zero" and "could not measure" has to survive
    that route too.
    """

    empty = ComparableSeries(
        native_symbol="13010",
        market_code="JP",
        provider_id="test",
        as_of=date(2026, 9, 16),
        basis="SPLIT_ADJUSTED_TO_AS_OF",
        bars=[],
        applied_actions=[],
        unusable_actions=[],
        volume_comparable=True,
        notes=[],
    )
    assessment = assess(empty, security_id="sec-1")

    assert assessment.close is None
    assert any("no bars" in gap for gap in assessment.measurement_gaps)
    assert assessment.concepts_fired == []


def test_a_short_history_says_so_instead_of_computing_long_windows():
    assessment = _assess([100.0 + n for n in range(8)])
    assert any("fewer than the" in gap for gap in assessment.measurement_gaps)
    assert assessment.realized_vol_20d is None


def test_basic_session_measurements():
    assessment = _assess([100.0] * 29 + [110.0], highs=[120.0] * 30, lows=[100.0] * 30, opens=[105.0] * 30)
    assert assessment.close == pytest.approx(110.0)
    assert assessment.prior_close == pytest.approx(100.0)
    assert assessment.session_range_pct == pytest.approx(20.0)
    # close 110 in a 100-120 range sits halfway up it
    assert assessment.close_position_in_range == pytest.approx(0.5)


def test_volume_measurements_are_withheld_when_volume_is_not_comparable(monkeypatch):
    """A split we could not read makes volume incomparable, so it is not reported.

    Computing relative volume across a share-count change would compare two
    different units and call the result a ratio.
    """

    series = _series([100.0 + n for n in range(40)])
    object.__setattr__(series, "volume_comparable", False)
    assessment = assess(series, security_id="sec-1")

    assert assessment.relative_volume_20d is None
    assert assessment.up_volume_ratio_5d is None
    assert any("volume could not be restated" in gap for gap in assessment.measurement_gaps)


def test_session_vwap_from_daily_data_says_it_is_not_intraday():
    assessment = _assess([100.0] * 30)
    assert assessment.session_vwap is not None
    assert assessment.intraday_available is False
    assert any("no intraday data" in note for note in assessment.notes)


def test_intraday_bars_produce_a_real_session_vwap():
    series = _series([100.0] * 30)
    assessment = assess(
        series,
        security_id="sec-1",
        intraday_bars=[{"price": 100.0, "volume": 1.0}, {"price": 110.0, "volume": 3.0}],
    )
    assert assessment.intraday_available is True
    assert assessment.session_vwap == pytest.approx(107.5)


def test_consecutive_down_days_counts_the_current_run_only():
    assessment = _assess([100, 101, 102, 101, 100, 99])
    assert assessment.consecutive_down_days == 3


def test_turnover_carries_its_currency():
    """A turnover threshold without a currency compares yen to dollars."""

    jp = _assess([100.0] * 30)
    us = _assess([100.0] * 30, market="US", symbol="AAPL")
    assert jp.turnover_currency == "JPY"
    assert us.turnover_currency == "USD"


# -------------------------------------------------------------------- concepts


def _blank(**overrides) -> Stage2Assessment:
    base = {
        "security_id": "sec-1",
        "native_symbol": "13010",
        "market_code": "JP",
        "as_of_date": date(2026, 9, 16),
        "series_basis": "SPLIT_ADJUSTED_TO_AS_OF",
    }
    base.update(overrides)
    return Stage2Assessment(**base)


def test_seller_exhaustion_needs_all_three_of_its_measurements():
    fired, _ = match_concepts(
        _blank(consecutive_down_days=4, down_day_volume_decay=0.5, lower_wick_ratio=0.6)
    )
    assert "SELLER_EXHAUSTION" in fired

    # Take away the wick and the concept no longer describes what is happening.
    fired, _ = match_concepts(_blank(consecutive_down_days=4, down_day_volume_decay=0.5))
    assert "SELLER_EXHAUSTION" not in fired


def test_a_tested_level_is_not_a_failed_breakout():
    """closed_back_below is the whole distinction, on a closing basis."""

    fired, _ = match_concepts(_blank(breakout_level=1250.0, bars_above_breakout=2, closed_back_below=False))
    assert "FAILED_BREAKOUT" not in fired

    fired, evidence = match_concepts(
        _blank(breakout_level=1250.0, bars_above_breakout=2, closed_back_below=True, failure_volume_ratio=1.9)
    )
    assert "FAILED_BREAKOUT" in fired
    assert evidence["FAILED_BREAKOUT"]["failure_volume_ratio"] == 1.9


def test_a_pullback_on_rising_volume_is_not_healthy():
    healthy = _blank(pullback_depth_pct=8.4, pullback_volume_contraction=0.45, holding_above_ma20=True)
    distribution = _blank(pullback_depth_pct=8.4, pullback_volume_contraction=1.25, holding_above_ma20=True)

    assert "HEALTHY_PULLBACK" in match_concepts(healthy)[0]
    assert "HEALTHY_PULLBACK" not in match_concepts(distribution)[0]


def test_vwap_reclaim_does_not_fire_on_a_thin_session():
    """The concept's own negative context, encoded rather than written down only."""

    thin = _blank(distance_from_anchored_vwap_pct=0.3, relative_volume_20d=0.33)
    active = _blank(distance_from_anchored_vwap_pct=0.3, relative_volume_20d=1.4)

    assert "VWAP_RECLAIM" not in match_concepts(thin)[0]
    assert "VWAP_RECLAIM" in match_concepts(active)[0]


def test_every_fired_concept_carries_the_numbers_it_fired_on():
    fired, evidence = match_concepts(
        _blank(consecutive_down_days=4, down_day_volume_decay=0.5, lower_wick_ratio=0.6)
    )
    assert set(fired) == set(evidence)
    assert evidence["SELLER_EXHAUSTION"]["down_day_volume_decay"] == 0.5


def test_missing_measurements_fire_nothing():
    assert match_concepts(_blank())[0] == []


# ------------------------------------------------------------------- obstacles


def test_a_prior_high_is_recorded_as_an_obstacle():
    closes = [100, 105, 130, 108, 102, 100, 99, 100, 101, 100]
    report = find_obstacles(_series(closes), security_id="sec-1")

    assert report.obstacles
    levels = [obstacle.price_level for obstacle in report.obstacles]
    assert max(levels) > 100
    assert report.nearest is not None


def test_a_high_reached_by_a_surge_is_named_as_such():
    # 100 -> 140 in five sessions, then back down: the peak is a surge high.
    closes = [100, 104, 112, 126, 140, 120, 108, 102, 100, 99, 100, 101]
    report = find_obstacles(_series(closes), security_id="sec-1", surge_threshold_pct=20.0)
    kinds = {obstacle.kind for obstacle in report.obstacles}
    assert ObstacleKind.PRIOR_SURGE_HIGH in kinds

    surge = next(o for o in report.obstacles if o.kind is ObstacleKind.PRIOR_SURGE_HIGH)
    assert surge.established_on is not None
    assert "misread as a target" in surge.strength_note


def test_obstacles_refuse_to_be_read_as_upside():
    """The rule this module exists for, with somewhere for the mistake to land."""

    report = find_obstacles(_series([100, 105, 130, 108, 102, 100, 99, 100]), security_id="sec-1")
    with pytest.raises(ObstacleMisuse, match="resistance, not upside"):
        report.upside_from_obstacles()


def test_an_obstacle_can_be_marked_weakened_but_only_with_evidence():
    report = find_obstacles(_series([100, 105, 130, 108, 102, 100, 99, 100]), security_id="sec-1")
    obstacle = report.obstacles[0]

    with pytest.raises(ValueError, match="evidence written down"):
        mark_weakened(obstacle, "   ")

    weakened = mark_weakened(obstacle, "traded through on 3x average volume and held for six sessions")
    assert weakened.weakening_evidence.startswith("traded through")
    # The level itself is unchanged: weakening is an annotation, not a discount.
    assert weakened.price_level == obstacle.price_level


def test_no_obstacle_structure_from_a_handful_of_bars():
    report = find_obstacles(_series([100, 101, 102]), security_id="sec-1")
    assert report.obstacles == []
    assert any("fewer than five bars" in note for note in report.notes)


def test_a_round_number_is_optional_and_labelled_weak():
    report = find_obstacles(
        _series([100, 105, 130, 108, 102, 100, 99, 100]),
        security_id="sec-1",
        round_number_step=50.0,
    )
    round_levels = [o for o in report.obstacles if o.kind is ObstacleKind.ROUND_NUMBER]
    assert len(round_levels) == 1
    assert "weak on its own" in round_levels[0].strength_note


def test_obstacles_are_all_above_the_current_price():
    series = _series([100, 105, 130, 108, 102, 100, 99, 100, 101, 100])
    close = float(series.bars[-1].close)
    report = find_obstacles(series, security_id="sec-1")
    assert all(obstacle.price_level > close for obstacle in report.obstacles)
