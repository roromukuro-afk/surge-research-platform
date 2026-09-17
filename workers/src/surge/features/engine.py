"""Turning one comparable series into the numeric record Stage 1 reasons over.

The engine is deliberately dumb: it computes, it does not judge. No threshold
lives here, nothing is named "oversold" or "contracted". That separation is what
lets a route's threshold change later and be re-evaluated against features that
were computed before anyone thought of it.

Two refusals are built in. A series whose splits could not be read produces no
features at all - every return spanning the unreadable date would be wrong, and
a partially wrong feature row is worse than none. And a feature whose window is
longer than the history available is None, never a short-window substitute.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date

from surge.features import indicators
from surge.market.series import ComparableSeries

FEATURE_VERSION = "features-1.0.0"

# The longest window any feature uses. Below this the row is still written -
# a security with 40 bars has real 20-day features - but warmup_satisfied is
# false so a consumer knows which nulls are history rather than market.
WARMUP_BARS = 75


class FeatureError(RuntimeError):
    pass


@dataclass(frozen=True)
class DailyFeatures:
    market_code: str
    provider_id: str
    native_symbol: str
    trade_date: date
    feature_version: str
    series_basis: str
    bars_available: int
    warmup_satisfied: bool

    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float | None = None
    volume: float | None = None
    turnover: float | None = None

    ret_1d: float | None = None
    ret_3d: float | None = None
    ret_5d: float | None = None
    ret_10d: float | None = None
    ret_20d: float | None = None

    high_5d: float | None = None
    high_10d: float | None = None
    high_20d: float | None = None
    high_60d: float | None = None
    low_5d: float | None = None
    low_10d: float | None = None
    low_20d: float | None = None
    low_60d: float | None = None
    dist_from_high_5d_pct: float | None = None
    dist_from_high_10d_pct: float | None = None
    dist_from_high_20d_pct: float | None = None
    dist_from_high_60d_pct: float | None = None
    dist_from_low_20d_pct: float | None = None
    days_since_high_20d: int | None = None
    days_since_high_60d: int | None = None

    atr_14: float | None = None
    atr_pct_14: float | None = None
    true_range_pct: float | None = None
    rv_10d: float | None = None
    rv_20d: float | None = None
    rv_20d_annualized: float | None = None

    vol_avg_5d: float | None = None
    vol_avg_20d: float | None = None
    vol_avg_60d: float | None = None
    rvol_5d: float | None = None
    rvol_20d: float | None = None
    turnover_avg_20d: float | None = None
    turnover_rvol: float | None = None

    sma_5: float | None = None
    sma_25: float | None = None
    sma_75: float | None = None
    ema_12: float | None = None
    ema_26: float | None = None
    close_vs_sma25_pct: float | None = None
    sma5_vs_sma25_pct: float | None = None

    rsi_14: float | None = None
    macd: float | None = None
    macd_signal: float | None = None
    macd_hist: float | None = None
    adx_14: float | None = None
    plus_di_14: float | None = None
    minus_di_14: float | None = None

    bb_mid_20: float | None = None
    bb_upper_20: float | None = None
    bb_lower_20: float | None = None
    bb_width_20: float | None = None
    bb_percent_b: float | None = None

    vwap_day: float | None = None
    vwap_source: str | None = None
    gap_pct: float | None = None
    body_pct: float | None = None
    upper_wick_pct: float | None = None
    lower_wick_pct: float | None = None
    close_location_value: float | None = None

    range_5d_pct: float | None = None
    range_20d_pct: float | None = None
    range_contraction_ratio: float | None = None
    breakout_distance_pct: float | None = None
    consecutive_up_days: int | None = None

    resistance_20d: float | None = None
    resistance_60d: float | None = None
    support_20d: float | None = None
    support_60d: float | None = None

    def as_dict(self) -> dict:
        return asdict(self)


def _f(value) -> float | None:
    return None if value is None else float(value)


def _distance_below(reference: float | None, close: float | None) -> float | None:
    """How far below a reference the close sits, as a positive percentage."""

    if reference is None or close is None or reference == 0:
        return None
    return (reference - close) / reference * 100.0


def _range_pct(high: float | None, low: float | None) -> float | None:
    if high is None or low is None or low <= 0:
        return None
    return (high - low) / low * 100.0


def compute_features(series: ComparableSeries) -> DailyFeatures:
    """Compute the feature row for the last bar of ``series``."""

    if series.is_broken:
        raise FeatureError(
            f"{series.native_symbol}: {len(series.unusable_actions)} share-count action(s) could not be "
            "read, so no return spanning them is reliable; refusing to produce features"
        )
    if not series.bars:
        raise FeatureError("cannot compute features from an empty series")

    bars = series.bars
    closes = [_f(b.close) for b in bars]
    highs = [_f(b.high) for b in bars]
    lows = [_f(b.low) for b in bars]
    opens = [_f(b.open) for b in bars]
    volumes = [_f(b.volume) for b in bars]
    turnovers = [_f(b.turnover) for b in bars]

    latest = bars[-1]
    close = _f(latest.close)
    previous_close = closes[-2] if len(closes) >= 2 else None

    high_20d = indicators.rolling_max(highs, 20)
    high_60d = indicators.rolling_max(highs, 60)
    low_20d = indicators.rolling_min(lows, 20)

    atr_14 = indicators.atr(highs, lows, closes, 14)
    macd_value, macd_signal, macd_hist = indicators.macd(closes)
    adx, plus_di, minus_di = indicators.adx_dmi(highs, lows, closes, 14)
    bb_mid, bb_upper, bb_lower, bb_width, bb_percent = indicators.bollinger(closes, 20)
    body, upper_wick, lower_wick, clv = indicators.candle_shape(
        opens[-1], highs[-1], lows[-1], closes[-1]
    )

    sma_25 = indicators.sma(closes, 25)
    sma_5 = indicators.sma(closes, 5)

    # Turnover is money; volume is shares. A provider that gives both lets us
    # state a day's average price. Where it gives only volume, there is no VWAP
    # and none is invented.
    turnover = turnovers[-1]
    volume = volumes[-1]
    vwap = None
    vwap_source = None
    if turnover is not None and volume not in (None, 0):
        vwap = turnover / volume
        vwap_source = "TURNOVER_OVER_VOLUME"

    range_5d = _range_pct(indicators.rolling_max(highs, 5), indicators.rolling_min(lows, 5))
    range_20d = _range_pct(high_20d, low_20d)
    contraction = None
    if range_5d is not None and range_20d not in (None, 0):
        contraction = range_5d / range_20d

    true_range_pct = None
    if previous_close not in (None, 0) and highs[-1] is not None and lows[-1] is not None:
        span = max(
            highs[-1] - lows[-1],
            abs(highs[-1] - previous_close),
            abs(lows[-1] - previous_close),
        )
        true_range_pct = span / previous_close * 100.0

    return DailyFeatures(
        market_code=series.market_code,
        provider_id=series.provider_id,
        native_symbol=series.native_symbol,
        trade_date=latest.trade_date,
        feature_version=FEATURE_VERSION,
        series_basis=series.basis,
        bars_available=len(bars),
        warmup_satisfied=len(bars) >= WARMUP_BARS,

        open=opens[-1], high=highs[-1], low=lows[-1], close=close,
        volume=volume, turnover=turnover,

        ret_1d=indicators.pct_change(closes, 1),
        ret_3d=indicators.pct_change(closes, 3),
        ret_5d=indicators.pct_change(closes, 5),
        ret_10d=indicators.pct_change(closes, 10),
        ret_20d=indicators.pct_change(closes, 20),

        high_5d=indicators.rolling_max(highs, 5),
        high_10d=indicators.rolling_max(highs, 10),
        high_20d=high_20d,
        high_60d=high_60d,
        low_5d=indicators.rolling_min(lows, 5),
        low_10d=indicators.rolling_min(lows, 10),
        low_20d=low_20d,
        low_60d=indicators.rolling_min(lows, 60),
        dist_from_high_5d_pct=_distance_below(indicators.rolling_max(highs, 5), close),
        dist_from_high_10d_pct=_distance_below(indicators.rolling_max(highs, 10), close),
        dist_from_high_20d_pct=_distance_below(high_20d, close),
        dist_from_high_60d_pct=_distance_below(high_60d, close),
        dist_from_low_20d_pct=(
            None if low_20d in (None, 0) or close is None else (close - low_20d) / low_20d * 100.0
        ),
        days_since_high_20d=indicators.bars_since_max(highs, 20),
        days_since_high_60d=indicators.bars_since_max(highs, 60),

        atr_14=atr_14,
        atr_pct_14=(None if atr_14 is None or close in (None, 0) else atr_14 / close * 100.0),
        true_range_pct=true_range_pct,
        rv_10d=indicators.realized_volatility(closes, 10),
        rv_20d=indicators.realized_volatility(closes, 20),
        rv_20d_annualized=indicators.realized_volatility(closes, 20, annualize=True),

        vol_avg_5d=indicators.sma(volumes, 5),
        vol_avg_20d=indicators.sma(volumes, 20),
        vol_avg_60d=indicators.sma(volumes, 60),
        rvol_5d=indicators.relative_volume(volumes, 5),
        rvol_20d=indicators.relative_volume(volumes, 20),
        turnover_avg_20d=indicators.sma(turnovers, 20),
        turnover_rvol=indicators.relative_volume(turnovers, 20),

        sma_5=sma_5,
        sma_25=sma_25,
        sma_75=indicators.sma(closes, 75),
        ema_12=indicators.ema(closes, 12),
        ema_26=indicators.ema(closes, 26),
        close_vs_sma25_pct=(
            None if sma_25 in (None, 0) or close is None else (close - sma_25) / sma_25 * 100.0
        ),
        sma5_vs_sma25_pct=(
            None if sma_25 in (None, 0) or sma_5 is None else (sma_5 - sma_25) / sma_25 * 100.0
        ),

        rsi_14=indicators.rsi(closes, 14),
        macd=macd_value, macd_signal=macd_signal, macd_hist=macd_hist,
        adx_14=adx, plus_di_14=plus_di, minus_di_14=minus_di,

        bb_mid_20=bb_mid, bb_upper_20=bb_upper, bb_lower_20=bb_lower,
        bb_width_20=bb_width, bb_percent_b=bb_percent,

        vwap_day=vwap,
        vwap_source=vwap_source,
        gap_pct=(
            None
            if previous_close in (None, 0) or opens[-1] is None
            else (opens[-1] - previous_close) / previous_close * 100.0
        ),
        body_pct=body, upper_wick_pct=upper_wick, lower_wick_pct=lower_wick,
        close_location_value=clv,

        range_5d_pct=range_5d,
        range_20d_pct=range_20d,
        range_contraction_ratio=contraction,
        breakout_distance_pct=_distance_below(high_20d, close),
        consecutive_up_days=indicators.consecutive_up_days(closes),

        resistance_20d=high_20d,
        resistance_60d=high_60d,
        support_20d=low_20d,
        support_60d=indicators.rolling_min(lows, 60),
    )
