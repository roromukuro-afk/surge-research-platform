"""Stage 2: measure the chart, then say which concepts the measurements match.

The order in that sentence is the design. Numbers are the record; the concept
names are a handle for humans and are re-derivable from the numbers. Storing the
label alone would be storing a conclusion whose evidence had been thrown away.

Every measurement that could not be taken is named in ``measurement_gaps``. A
column that is null because the data was missing and a column that is null
because we never computed it look identical otherwise, and only one of those is
a problem worth chasing.

Nothing here reaches past the as-of date: it is handed a
:class:`~surge.market.series.ComparableSeries` that has already been cut to the
knowledge cutoff, and it never looks at ``bars[-1]`` expecting tomorrow.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from surge.features.indicators import atr, realized_volatility, relative_volume, sma
from surge.market.series import ComparableBar, ComparableSeries

STAGE2_VERSION = "stage2-1.0.0"
CONCEPT_VERSION = "chart-kb-1.0.0"

#: Minimum bars before the longer-window measurements mean anything.
WARMUP_BARS = 25


def _f(value: Decimal | float | None) -> float | None:
    return None if value is None else float(value)


def _pct(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator in (None, 0):
        return None
    return (numerator / denominator - 1.0) * 100.0


@dataclass
class Stage2Assessment:
    """Measurements for one security on one date, plus what they matched."""

    security_id: str
    native_symbol: str
    market_code: str
    as_of_date: date
    series_basis: str
    assessment_version: str = STAGE2_VERSION
    concept_version: str = CONCEPT_VERSION

    close: float | None = None
    prior_close: float | None = None
    session_range_pct: float | None = None
    close_position_in_range: float | None = None
    gap_pct: float | None = None

    volume: float | None = None
    relative_volume_20d: float | None = None
    turnover: float | None = None
    turnover_currency: str | None = None
    volume_trend_5d: float | None = None
    up_volume_ratio_5d: float | None = None

    nearest_support: float | None = None
    nearest_support_distance_pct: float | None = None
    nearest_support_touches: int | None = None
    nearest_resistance: float | None = None
    nearest_resistance_distance_pct: float | None = None
    nearest_resistance_touches: int | None = None

    session_vwap: float | None = None
    distance_from_session_vwap_pct: float | None = None
    anchored_vwap: float | None = None
    anchored_vwap_anchor_date: date | None = None
    distance_from_anchored_vwap_pct: float | None = None

    atr_14: float | None = None
    atr_pct: float | None = None
    realized_vol_20d: float | None = None
    volatility_expansion_ratio: float | None = None

    down_day_volume_decay: float | None = None
    lower_wick_ratio: float | None = None
    capitulation_volume_ratio: float | None = None
    consecutive_down_days: int | None = None

    breakout_level: float | None = None
    bars_above_breakout: int | None = None
    closed_back_below: bool | None = None
    failure_volume_ratio: float | None = None

    pullback_depth_pct: float | None = None
    pullback_volume_contraction: float | None = None
    holding_above_ma20: bool | None = None
    pullback_bars: int | None = None

    overhead_volume_ratio: float | None = None
    overhead_levels: int | None = None
    distance_to_heaviest_overhead_pct: float | None = None

    concepts_fired: list[str] = field(default_factory=list)
    concept_evidence: dict = field(default_factory=dict)
    intraday_available: bool = False
    measurement_gaps: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "security_id": self.security_id,
            "as_of_date": self.as_of_date.isoformat(),
            "concepts_fired": list(self.concepts_fired),
            "measurement_gaps": list(self.measurement_gaps),
            "intraday_available": self.intraday_available,
        }


def _levels(bars: list[ComparableBar], *, tolerance_pct: float = 1.0) -> list[tuple[float, int, float]]:
    """Cluster swing highs and lows into levels, with touch counts and volume.

    A level is a price several sessions reacted to, not any price that appeared
    once. Touch count is the thing that distinguishes the two, so it travels with
    the level everywhere - including into the obstacles table, where a prior high
    with no volume behind it is explicitly not an overhang.
    """

    pivots: list[tuple[float, float]] = []
    for index in range(1, len(bars) - 1):
        previous, current, following = bars[index - 1], bars[index], bars[index + 1]
        high, low = _f(current.high), _f(current.low)
        volume = _f(current.volume) or 0.0
        if high is not None and _f(previous.high) is not None and _f(following.high) is not None:
            if high >= _f(previous.high) and high >= _f(following.high):
                pivots.append((high, volume))
        if low is not None and _f(previous.low) is not None and _f(following.low) is not None:
            if low <= _f(previous.low) and low <= _f(following.low):
                pivots.append((low, volume))

    clusters: list[list[tuple[float, float]]] = []
    for price, volume in sorted(pivots):
        if clusters and abs(price / clusters[-1][0][0] - 1.0) * 100.0 <= tolerance_pct:
            clusters[-1].append((price, volume))
        else:
            clusters.append([(price, volume)])

    return [
        (
            sum(price for price, _ in cluster) / len(cluster),
            len(cluster),
            sum(volume for _, volume in cluster),
        )
        for cluster in clusters
    ]


def assess(
    series: ComparableSeries,
    *,
    security_id: str,
    intraday_bars=None,
) -> Stage2Assessment:
    """Measure one security as of the series' own as-of date.

    ``intraday_bars`` is optional and, when absent, session VWAP is computed from
    the daily bar's typical price. That is a coarser thing wearing the same
    column name, so ``intraday_available`` records which one it was rather than
    letting the two be confused later.
    """

    bars = series.bars
    assessment = Stage2Assessment(
        security_id=security_id,
        native_symbol=series.native_symbol,
        market_code=series.market_code,
        as_of_date=series.as_of,
        series_basis=series.basis,
        intraday_available=bool(intraday_bars),
    )
    assessment.notes.extend(series.notes)

    if not bars:
        assessment.measurement_gaps.append("no bars at or before the as-of date")
        return assessment

    if len(bars) < WARMUP_BARS:
        assessment.measurement_gaps.append(
            f"only {len(bars)} bars, fewer than the {WARMUP_BARS} the longer windows need"
        )

    last = bars[-1]
    closes = [_f(bar.close) for bar in bars]
    highs = [_f(bar.high) for bar in bars]
    lows = [_f(bar.low) for bar in bars]
    volumes = [_f(bar.volume) for bar in bars]

    assessment.close = closes[-1]
    assessment.prior_close = closes[-2] if len(closes) > 1 else None
    assessment.turnover = _f(last.turnover)
    assessment.turnover_currency = getattr(last.source, "currency", None)
    assessment.volume = volumes[-1]

    high, low, close = highs[-1], lows[-1], closes[-1]
    if None not in (high, low, close) and low > 0:
        span = high - low
        assessment.session_range_pct = (span / low) * 100.0
        assessment.close_position_in_range = ((close - low) / span) if span > 0 else 0.5
        wick = min(close, _f(last.open) or close) - low
        assessment.lower_wick_ratio = (wick / span) if span > 0 else 0.0
    else:
        assessment.measurement_gaps.append("session range: the last bar is missing a high, low or close")

    if len(bars) > 1:
        assessment.gap_pct = _pct(_f(last.open), closes[-2])

    # --- volume -------------------------------------------------------------
    if not series.volume_comparable:
        assessment.measurement_gaps.append(
            "volume could not be restated to the as-of share count, so every volume "
            "measurement is withheld rather than computed on an incomparable series"
        )
    else:
        assessment.relative_volume_20d = relative_volume(volumes, 20)
        recent = sma(volumes[-5:], 5)
        older = sma(volumes[-10:-5], 5) if len(volumes) >= 10 else None
        if recent is not None and older not in (None, 0):
            assessment.volume_trend_5d = recent / older - 1.0
        up_volume = sum(
            (volumes[i] or 0.0)
            for i in range(max(1, len(bars) - 5), len(bars))
            if closes[i] is not None and closes[i - 1] is not None and closes[i] > closes[i - 1]
        )
        total_volume = sum((volumes[i] or 0.0) for i in range(max(1, len(bars) - 5), len(bars)))
        if total_volume > 0:
            assessment.up_volume_ratio_5d = up_volume / total_volume

    # --- volatility ---------------------------------------------------------
    clean_highs = [h for h in highs if h is not None]
    clean_lows = [low_value for low_value in lows if low_value is not None]
    clean_closes = [c for c in closes if c is not None]
    if len(clean_closes) >= 15 and len(clean_highs) == len(clean_lows) == len(clean_closes):
        assessment.atr_14 = atr(clean_highs, clean_lows, clean_closes, 14)
        if assessment.atr_14 is not None and close:
            assessment.atr_pct = assessment.atr_14 / close * 100.0
        assessment.realized_vol_20d = realized_volatility(clean_closes, 20)
        short_atr = atr(clean_highs[-8:], clean_lows[-8:], clean_closes[-8:], 5)
        if short_atr is not None and assessment.atr_14:
            assessment.volatility_expansion_ratio = short_atr / assessment.atr_14
    else:
        assessment.measurement_gaps.append("volatility: not enough complete high/low/close bars")

    # --- support and resistance --------------------------------------------
    levels = _levels(bars)
    if levels and close is not None:
        below = [level for level in levels if level[0] < close]
        above = [level for level in levels if level[0] > close]
        if below:
            price, touches, _volume = max(below, key=lambda level: level[0])
            assessment.nearest_support = price
            assessment.nearest_support_touches = touches
            assessment.nearest_support_distance_pct = (close / price - 1.0) * 100.0
        if above:
            price, touches, _volume = min(above, key=lambda level: level[0])
            assessment.nearest_resistance = price
            assessment.nearest_resistance_touches = touches
            assessment.nearest_resistance_distance_pct = (price / close - 1.0) * 100.0
    else:
        assessment.measurement_gaps.append("support and resistance: no pivot clusters found")

    # --- VWAP ---------------------------------------------------------------
    if intraday_bars:
        weighted = sum((b["price"] * b["volume"]) for b in intraday_bars)
        traded = sum(b["volume"] for b in intraday_bars)
        assessment.session_vwap = (weighted / traded) if traded else None
    elif None not in (high, low, close):
        # The typical price is the honest daily stand-in for a session VWAP. It
        # is not the same number, which is what intraday_available records.
        assessment.session_vwap = (high + low + close) / 3.0
        assessment.notes.append("session VWAP derived from the daily typical price; no intraday data")
    if assessment.session_vwap and close:
        assessment.distance_from_session_vwap_pct = (close / assessment.session_vwap - 1.0) * 100.0

    anchor_index = _anchor_index(bars)
    if anchor_index is not None and series.volume_comparable:
        window = bars[anchor_index:]
        weighted = sum(
            ((_f(b.high) or 0) + (_f(b.low) or 0) + (_f(b.close) or 0)) / 3.0 * (_f(b.volume) or 0.0)
            for b in window
        )
        traded = sum((_f(b.volume) or 0.0) for b in window)
        if traded > 0:
            assessment.anchored_vwap = weighted / traded
            assessment.anchored_vwap_anchor_date = window[0].trade_date
            if close:
                assessment.distance_from_anchored_vwap_pct = (close / assessment.anchored_vwap - 1.0) * 100.0
    else:
        assessment.measurement_gaps.append("anchored VWAP: no usable anchor, or volume is not comparable")

    _measure_exhaustion(assessment, bars, closes, volumes)
    _measure_breakout(assessment, bars, closes, highs, volumes, levels)
    _measure_pullback(assessment, bars, closes, highs, volumes)
    _measure_overhang(assessment, levels, close, volumes)

    assessment.concepts_fired, assessment.concept_evidence = match_concepts(assessment)
    return assessment


def _anchor_index(bars: list[ComparableBar]) -> int | None:
    """The heaviest-volume session in the recent window, as the VWAP anchor.

    A crude proxy for "the event that started this move". Named rather than
    hidden: a better anchor is an actual event date from Phase 5, and when that
    is wired in this fallback should give way to it.
    """

    window = bars[-60:]
    if len(window) < 5:
        return None
    volumes = [(_f(bar.volume) or 0.0, index) for index, bar in enumerate(window)]
    if not any(volume for volume, _ in volumes):
        return None
    _, position = max(volumes)
    return len(bars) - len(window) + position


def _measure_exhaustion(assessment, bars, closes, volumes) -> None:
    down_runs = 0
    for index in range(len(closes) - 1, 0, -1):
        if closes[index] is None or closes[index - 1] is None or closes[index] >= closes[index - 1]:
            break
        down_runs += 1
    assessment.consecutive_down_days = down_runs

    if down_runs >= 2:
        run = volumes[len(closes) - down_runs : len(closes)]
        first, last_volume = run[0], run[-1]
        if first:
            assessment.down_day_volume_decay = last_volume / first if last_volume is not None else None

    average = sma([v for v in volumes[:-1] if v is not None], 20)
    if average and volumes[-1]:
        assessment.capitulation_volume_ratio = volumes[-1] / average


def _measure_breakout(assessment, bars, closes, highs, volumes, levels) -> None:
    """Did we clear a level and lose it?

    ``closed_back_below`` is on a closing basis on purpose. A breakout that
    trades below the level intraday and closes above it has been tested, not
    broken, and conflating the two would make the concept fire constantly.
    """

    if not levels or closes[-1] is None:
        return
    lookback = closes[-20:]
    if len(lookback) < 5:
        return
    peak = max(c for c in lookback if c is not None)
    crossed = [level for level in levels if level[0] < peak and level[0] > closes[-1]]
    if not crossed:
        return

    level_price, _touches, _volume = max(crossed, key=lambda level: level[0])
    assessment.breakout_level = level_price
    above = 0
    for close in reversed(lookback):
        if close is None:
            break
        if close > level_price:
            above += 1
        elif above:
            break
    assessment.bars_above_breakout = above
    assessment.closed_back_below = closes[-1] < level_price

    average = sma([v for v in volumes[:-1] if v is not None], 20)
    if average and volumes[-1]:
        assessment.failure_volume_ratio = volumes[-1] / average


def _measure_pullback(assessment, bars, closes, highs, volumes) -> None:
    if len(closes) < 10 or closes[-1] is None:
        return
    window_highs = [h for h in highs[-20:] if h is not None]
    if not window_highs:
        return
    peak = max(window_highs)
    if peak <= 0:
        return
    assessment.pullback_depth_pct = (1.0 - closes[-1] / peak) * 100.0

    peak_index = max(
        (index for index in range(len(highs) - 20, len(highs)) if index >= 0 and highs[index] == peak),
        default=None,
    )
    if peak_index is not None:
        assessment.pullback_bars = len(highs) - 1 - peak_index
        advance = [v for v in volumes[max(0, peak_index - 10) : peak_index + 1] if v is not None]
        retreat = [v for v in volumes[peak_index + 1 :] if v is not None]
        if advance and retreat:
            advance_mean = sum(advance) / len(advance)
            if advance_mean:
                assessment.pullback_volume_contraction = (sum(retreat) / len(retreat)) / advance_mean

    ma20 = sma([c for c in closes if c is not None], 20)
    if ma20 is not None:
        assessment.holding_above_ma20 = closes[-1] >= ma20


def _measure_overhang(assessment, levels, close, volumes) -> None:
    if not levels or close is None:
        return
    above = [level for level in levels if level[0] > close]
    assessment.overhead_levels = len(above)
    if not above:
        return
    heaviest = max(above, key=lambda level: level[2])
    assessment.distance_to_heaviest_overhead_pct = (heaviest[0] / close - 1.0) * 100.0
    average = sma([v for v in volumes if v is not None], 20)
    if average:
        assessment.overhead_volume_ratio = heaviest[2] / average


def match_concepts(assessment: Stage2Assessment) -> tuple[list[str], dict]:
    """Which concepts the measurements match, and on what numbers.

    Derived from the assessment, never stored instead of it. Every entry carries
    the measurements it rested on, so the label can be checked against the
    concept's own definition rather than taken on trust.
    """

    fired: list[str] = []
    evidence: dict = {}

    def fire(concept: str, **measurements) -> None:
        fired.append(concept)
        evidence[concept] = measurements

    if (
        assessment.consecutive_down_days is not None
        and assessment.consecutive_down_days >= 3
        and assessment.down_day_volume_decay is not None
        and assessment.down_day_volume_decay < 0.7
        and assessment.lower_wick_ratio is not None
        and assessment.lower_wick_ratio >= 0.4
    ):
        fire(
            "SELLER_EXHAUSTION",
            consecutive_down_days=assessment.consecutive_down_days,
            down_day_volume_decay=assessment.down_day_volume_decay,
            lower_wick_ratio=assessment.lower_wick_ratio,
            capitulation_volume_ratio=assessment.capitulation_volume_ratio,
        )

    if assessment.closed_back_below and (assessment.bars_above_breakout or 0) >= 1:
        fire(
            "FAILED_BREAKOUT",
            breakout_level=assessment.breakout_level,
            bars_above_breakout=assessment.bars_above_breakout,
            closed_back_below=assessment.closed_back_below,
            failure_volume_ratio=assessment.failure_volume_ratio,
        )

    if (
        assessment.pullback_depth_pct is not None
        and 0 < assessment.pullback_depth_pct <= 15
        and assessment.pullback_volume_contraction is not None
        and assessment.pullback_volume_contraction < 0.8
        and assessment.holding_above_ma20
    ):
        fire(
            "HEALTHY_PULLBACK",
            pullback_depth_pct=assessment.pullback_depth_pct,
            pullback_volume_contraction=assessment.pullback_volume_contraction,
            holding_above_ma20=assessment.holding_above_ma20,
            pullback_bars=assessment.pullback_bars,
        )

    if (
        assessment.overhead_volume_ratio is not None
        and assessment.overhead_volume_ratio >= 1.5
        and assessment.distance_to_heaviest_overhead_pct is not None
    ):
        fire(
            "SUPPLY_OVERHANG",
            overhead_volume_ratio=assessment.overhead_volume_ratio,
            overhead_levels=assessment.overhead_levels,
            distance_to_heaviest_overhead_pct=assessment.distance_to_heaviest_overhead_pct,
        )

    if (
        assessment.relative_volume_20d is not None
        and assessment.relative_volume_20d < 0.6
        and assessment.session_range_pct is not None
        and assessment.atr_pct is not None
        and assessment.session_range_pct < assessment.atr_pct
    ):
        fire(
            "VOLUME_DRY_UP",
            relative_volume_20d=assessment.relative_volume_20d,
            volume_trend_5d=assessment.volume_trend_5d,
            session_range_pct=assessment.session_range_pct,
            atr_pct=assessment.atr_pct,
        )

    if (
        assessment.distance_from_anchored_vwap_pct is not None
        and assessment.distance_from_anchored_vwap_pct > 0
        and assessment.relative_volume_20d is not None
        # The concept's own negative context: a reclaim on a thin session is
        # arithmetic, not information. Encoding that here is the point of
        # requiring a negative context on every concept.
        and assessment.relative_volume_20d >= 1.0
    ):
        fire(
            "VWAP_RECLAIM",
            anchored_vwap=assessment.anchored_vwap,
            distance_from_anchored_vwap_pct=assessment.distance_from_anchored_vwap_pct,
            anchored_vwap_anchor_date=(
                assessment.anchored_vwap_anchor_date.isoformat()
                if assessment.anchored_vwap_anchor_date
                else None
            ),
            relative_volume_20d=assessment.relative_volume_20d,
        )

    return fired, evidence
