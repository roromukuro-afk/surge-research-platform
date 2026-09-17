"""Routes A-H: eight independent ways to be interesting.

The routes are ORed. A security reaches Stage 2 if any one of them finds it, and
the answer is the list of routes that did - not a score, not a winner. Requiring
several routes to agree would not be selectivity, it would be an intersection of
eight narrow filters, and it would be empty.

Every route records the measurements it fired on. That is the difference between
a candidate you can re-evaluate when a threshold changes and one you have to
re-run blind. It is also the only way to tell, later, whether a route that
produced nothing was badly tuned or simply looking at a quiet market.

A route never fires on a missing feature. If the number it needs is None - no
history, no turnover from this provider, an unreadable split upstream - the
route declines. Firing on absence is how a screen ends up selecting for thin
data rather than for setups.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from surge.features import indicators
from surge.features.engine import DailyFeatures
from surge.market.series import ComparableSeries

ROUTE_VERSION = "route-1.0.0"


@dataclass(frozen=True)
class RouteDefinition:
    code: str
    name: str
    description: str
    thresholds: dict[str, Any]
    min_bars: int


@dataclass(frozen=True)
class RouteHit:
    code: str
    evidence: dict[str, float | int | str | None]


@dataclass(frozen=True)
class CandidateResult:
    market_code: str
    provider_id: str
    native_symbol: str
    trade_date: date
    route_version: str
    feature_version: str
    discovery_routes: list[str]
    route_evidence: dict[str, dict]
    route_count: int = field(init=False, default=0)

    def __post_init__(self) -> None:
        object.__setattr__(self, "route_count", len(self.discovery_routes))

    @property
    def is_candidate(self) -> bool:
        return self.route_count > 0


# Thresholds mirror screening.route_definitions in the database. The database is
# the source of truth for what a run used; this is the default, and a test
# asserts the two agree so they cannot drift apart unnoticed.
ROUTE_DEFINITIONS: dict[str, RouteDefinition] = {
    "A": RouteDefinition(
        "A", "Breakout proximity",
        "Close is near a recent high with the range still intact - the setup before a break, not after it.",
        {"max_dist_from_high_20d_pct": 3.0, "min_rvol_20d": 1.0}, 25,
    ),
    "B": RouteDefinition(
        "B", "Reclaim / trend reversal",
        "Price crosses back above a medium moving average after trading below it.",
        {"lookback_below_days": 5, "min_close_vs_sma25_pct": 0.0}, 30,
    ),
    "C": RouteDefinition(
        "C", "Volume-leading",
        "Volume expands well ahead of price - interest arriving before the move.",
        {"min_rvol_20d": 2.5, "max_abs_ret_1d_pct": 5.0}, 25,
    ),
    "D": RouteDefinition(
        "D", "Volatility contraction",
        "Recent range is a fraction of the prior range, and the bands have narrowed.",
        {"max_range_contraction_ratio": 0.6, "max_bb_width_20": 0.10}, 30,
    ),
    "E": RouteDefinition(
        "E", "Seller exhaustion / reversal",
        "A long lower wick and a close near the high after a decline.",
        {"min_lower_wick_pct": 40.0, "min_close_location_value": 0.6, "max_ret_5d_pct": -5.0}, 10,
    ),
    "F": RouteDefinition(
        "F", "Small-cap supply-demand elasticity",
        "Small turnover base with a sharp relative expansion - where a modest flow moves the price.",
        # Turnover is money in the security's own currency, so the base has to be
        # per market. A single figure would make a US name look a hundred times
        # smaller than an identical Japanese one.
        {"max_turnover_avg_20d_jpy": 500000000, "max_turnover_avg_20d_usd": 3500000,
         "min_turnover_rvol": 3.0}, 25,
    ),
    "G": RouteDefinition(
        "G", "Early momentum",
        "A fresh multi-day advance from a quiet base rather than an extended run.",
        {"min_ret_5d_pct": 5.0, "max_ret_20d_pct": 30.0, "min_rvol_20d": 1.5}, 25,
    ),
    "H": RouteDefinition(
        "H", "Healthy pullback",
        "An established uptrend giving back part of the move on falling volume.",
        {"min_close_vs_sma25_pct": 0.0, "min_dist_from_high_20d_pct": 3.0,
         "max_dist_from_high_20d_pct": 15.0, "max_rvol_5d": 1.0}, 30,
    ),
}


def _present(*values) -> bool:
    return all(value is not None for value in values)


def route_a(f: DailyFeatures, series: ComparableSeries | None, t: dict) -> RouteHit | None:
    if not _present(f.dist_from_high_20d_pct, f.rvol_20d):
        return None
    if f.dist_from_high_20d_pct <= t["max_dist_from_high_20d_pct"] and f.rvol_20d >= t["min_rvol_20d"]:
        return RouteHit(
            "A",
            {
                "dist_from_high_20d_pct": f.dist_from_high_20d_pct,
                "rvol_20d": f.rvol_20d,
                "high_20d": f.high_20d,
                "close": f.close,
            },
        )
    return None


def route_b(f: DailyFeatures, series: ComparableSeries | None, t: dict) -> RouteHit | None:
    """A reclaim needs a before as well as an after, so this one reads history."""

    if series is None or not _present(f.close_vs_sma25_pct):
        return None
    if f.close_vs_sma25_pct < t["min_close_vs_sma25_pct"]:
        return None

    closes = [None if bar.close is None else float(bar.close) for bar in series.bars]
    lookback = int(t["lookback_below_days"])
    was_below = False
    lowest_gap = None
    for step in range(1, lookback + 1):
        window = closes[: len(closes) - step]
        if len(window) < 25:
            break
        sma_25 = indicators.sma(window, 25)
        prior_close = window[-1]
        if sma_25 in (None, 0) or prior_close is None:
            continue
        gap = (prior_close - sma_25) / sma_25 * 100.0
        lowest_gap = gap if lowest_gap is None else min(lowest_gap, gap)
        if gap < 0:
            was_below = True

    if not was_below:
        return None
    return RouteHit(
        "B",
        {
            "close_vs_sma25_pct": f.close_vs_sma25_pct,
            "lowest_close_vs_sma25_pct_in_lookback": lowest_gap,
            "lookback_below_days": lookback,
        },
    )


def route_c(f: DailyFeatures, series: ComparableSeries | None, t: dict) -> RouteHit | None:
    if not _present(f.rvol_20d, f.ret_1d):
        return None
    if f.rvol_20d >= t["min_rvol_20d"] and abs(f.ret_1d) <= t["max_abs_ret_1d_pct"]:
        return RouteHit(
            "C",
            {"rvol_20d": f.rvol_20d, "ret_1d": f.ret_1d, "vol_avg_20d": f.vol_avg_20d, "volume": f.volume},
        )
    return None


def route_d(f: DailyFeatures, series: ComparableSeries | None, t: dict) -> RouteHit | None:
    if not _present(f.range_contraction_ratio, f.bb_width_20):
        return None
    if (
        f.range_contraction_ratio <= t["max_range_contraction_ratio"]
        and f.bb_width_20 <= t["max_bb_width_20"]
    ):
        return RouteHit(
            "D",
            {
                "range_contraction_ratio": f.range_contraction_ratio,
                "bb_width_20": f.bb_width_20,
                "range_5d_pct": f.range_5d_pct,
                "range_20d_pct": f.range_20d_pct,
                "atr_pct_14": f.atr_pct_14,
            },
        )
    return None


def route_e(f: DailyFeatures, series: ComparableSeries | None, t: dict) -> RouteHit | None:
    if not _present(f.lower_wick_pct, f.close_location_value, f.ret_5d):
        return None
    if (
        f.lower_wick_pct >= t["min_lower_wick_pct"]
        and f.close_location_value >= t["min_close_location_value"]
        and f.ret_5d <= t["max_ret_5d_pct"]
    ):
        return RouteHit(
            "E",
            {
                "lower_wick_pct": f.lower_wick_pct,
                "close_location_value": f.close_location_value,
                "ret_5d": f.ret_5d,
                "rsi_14": f.rsi_14,
            },
        )
    return None


def route_f(f: DailyFeatures, series: ComparableSeries | None, t: dict) -> RouteHit | None:
    if not _present(f.turnover_avg_20d, f.turnover_rvol):
        # A provider that supplies no turnover cannot answer this route. That is
        # a coverage gap, recorded as such, not a reason to guess from volume.
        return None
    key = "max_turnover_avg_20d_jpy" if f.market_code == "JP" else "max_turnover_avg_20d_usd"
    ceiling = t.get(key)
    if ceiling is None:
        return None
    if f.turnover_avg_20d <= ceiling and f.turnover_rvol >= t["min_turnover_rvol"]:
        return RouteHit(
            "F",
            {
                "turnover_avg_20d": f.turnover_avg_20d,
                "turnover_rvol": f.turnover_rvol,
                "turnover": f.turnover,
                "ceiling_applied": ceiling,
                "currency_market": f.market_code,
            },
        )
    return None


def route_g(f: DailyFeatures, series: ComparableSeries | None, t: dict) -> RouteHit | None:
    if not _present(f.ret_5d, f.ret_20d, f.rvol_20d):
        return None
    if (
        f.ret_5d >= t["min_ret_5d_pct"]
        and f.ret_20d <= t["max_ret_20d_pct"]
        and f.rvol_20d >= t["min_rvol_20d"]
    ):
        return RouteHit(
            "G",
            {"ret_5d": f.ret_5d, "ret_20d": f.ret_20d, "rvol_20d": f.rvol_20d, "adx_14": f.adx_14},
        )
    return None


def route_h(f: DailyFeatures, series: ComparableSeries | None, t: dict) -> RouteHit | None:
    if not _present(f.close_vs_sma25_pct, f.dist_from_high_20d_pct, f.rvol_5d):
        return None
    if (
        f.close_vs_sma25_pct >= t["min_close_vs_sma25_pct"]
        and t["min_dist_from_high_20d_pct"] <= f.dist_from_high_20d_pct <= t["max_dist_from_high_20d_pct"]
        and f.rvol_5d <= t["max_rvol_5d"]
    ):
        return RouteHit(
            "H",
            {
                "close_vs_sma25_pct": f.close_vs_sma25_pct,
                "dist_from_high_20d_pct": f.dist_from_high_20d_pct,
                "rvol_5d": f.rvol_5d,
                "sma_25": f.sma_25,
            },
        )
    return None


ROUTE_FUNCTIONS: dict[str, Callable[[DailyFeatures, ComparableSeries | None, dict], RouteHit | None]] = {
    "A": route_a,
    "B": route_b,
    "C": route_c,
    "D": route_d,
    "E": route_e,
    "F": route_f,
    "G": route_g,
    "H": route_h,
}


def evaluate_routes(
    features: DailyFeatures,
    *,
    series: ComparableSeries | None = None,
    definitions: dict[str, RouteDefinition] | None = None,
    route_version: str = ROUTE_VERSION,
) -> CandidateResult:
    """Run every route. The result is the list that fired, which may be empty."""

    definitions = definitions or ROUTE_DEFINITIONS
    fired: list[str] = []
    evidence: dict[str, dict] = {}

    for code in sorted(definitions):
        definition = definitions[code]
        if features.bars_available < definition.min_bars:
            continue
        hit = ROUTE_FUNCTIONS[code](features, series, definition.thresholds)
        if hit is not None:
            fired.append(code)
            evidence[code] = hit.evidence

    return CandidateResult(
        market_code=features.market_code,
        provider_id=features.provider_id,
        native_symbol=features.native_symbol,
        trade_date=features.trade_date,
        route_version=route_version,
        feature_version=features.feature_version,
        discovery_routes=fired,
        route_evidence=evidence,
    )


def route_summary(candidates: list[CandidateResult]) -> dict[str, int]:
    """How many candidates each route produced.

    A route that fires on everything and a route that never fires are both
    broken, and this is where either becomes visible.
    """

    summary = {f"route_{code.lower()}": 0 for code in ROUTE_DEFINITIONS}
    summary["candidates"] = 0
    summary["multi_route"] = 0
    for candidate in candidates:
        if not candidate.is_candidate:
            continue
        summary["candidates"] += 1
        if candidate.route_count > 1:
            summary["multi_route"] += 1
        for code in candidate.discovery_routes:
            summary[f"route_{code.lower()}"] += 1
    return summary
