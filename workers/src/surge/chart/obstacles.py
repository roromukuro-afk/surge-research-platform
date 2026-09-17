"""Price obstacles: what stands between here and higher.

The project rule this module exists to obey is narrow and easy to break by
accident. A prior high may be used as *resistance* - supply overhang, trapped
holders, a level that has to be traded through. It may never be used as a reason
the price will rise. "It was 2,000 yen last year" is not upside; it is a fact
about the past that tells you where sellers are waiting.

The second half of the rule matters as much: overhang expires. New material,
volume acceptance above a level, a breakout that held - any of these can drain a
level of its force. So nothing here applies a monotonic penalty for old highs,
and every obstacle carries a field for the evidence that it has weakened.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import StrEnum

from surge.market.series import ComparableSeries

OBSTACLE_VERSION = "obstacles-1.0.0"


class ObstacleKind(StrEnum):
    PRIOR_SURGE_HIGH = "PRIOR_SURGE_HIGH"
    SUPPLY_OVERHANG = "SUPPLY_OVERHANG"
    HORIZONTAL_RESISTANCE = "HORIZONTAL_RESISTANCE"
    MOVING_AVERAGE = "MOVING_AVERAGE"
    ROUND_NUMBER = "ROUND_NUMBER"
    GAP_EDGE = "GAP_EDGE"
    VWAP_ANCHOR = "VWAP_ANCHOR"


class ObstacleMisuse(RuntimeError):
    """Raised when an obstacle is being used as a reason to expect a rise."""


@dataclass(frozen=True)
class PriceObstacle:
    security_id: str
    as_of_date: date
    kind: ObstacleKind
    price_level: float
    distance_pct: float | None = None
    established_on: date | None = None
    volume_at_level: float | None = None
    touch_count: int | None = None
    strength_note: str | None = None
    weakening_evidence: str | None = None

    def as_dict(self) -> dict:
        return {
            "kind": self.kind.value,
            "price_level": self.price_level,
            "distance_pct": self.distance_pct,
            "established_on": self.established_on.isoformat() if self.established_on else None,
            "volume_at_level": self.volume_at_level,
            "touch_count": self.touch_count,
            "weakening_evidence": self.weakening_evidence,
        }


@dataclass
class ObstacleReport:
    security_id: str
    as_of_date: date
    obstacles: list[PriceObstacle] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def nearest(self) -> PriceObstacle | None:
        priced = [o for o in self.obstacles if o.distance_pct is not None]
        return min(priced, key=lambda o: o.distance_pct) if priced else None

    def upside_from_obstacles(self):
        """Always raises. Kept as a method so the mistake has somewhere to land.

        Code that wants "how far can this go" will reach for the list of levels
        above the price, because they are right there and look like targets.
        They are not. A reachable zone is built from current material, supply and
        volume structure - never from the fact that a level once existed.
        """

        raise ObstacleMisuse(
            "price obstacles are resistance, not upside. A prior high is where sellers are waiting, "
            "not evidence the price will return to it. Build the reachable zone from current material, "
            "supply and volume structure instead (CLAUDE.md 1-11)."
        )


def _f(value: Decimal | float | None) -> float | None:
    return None if value is None else float(value)


def find_obstacles(
    series: ComparableSeries,
    *,
    security_id: str,
    surge_threshold_pct: float = 20.0,
    round_number_step: float | None = None,
) -> ObstacleReport:
    """Collect the levels above the current price, with what makes each one hard.

    ``surge_threshold_pct`` decides which highs count as surge highs: a peak that
    stood more than this far above the price a few sessions earlier. The point of
    naming them separately is that they are the ones most likely to be misread as
    targets.
    """

    report = ObstacleReport(security_id=security_id, as_of_date=series.as_of)
    bars = series.bars
    if len(bars) < 5:
        report.notes.append("fewer than five bars; no obstacle structure to read")
        return report

    close = _f(bars[-1].close)
    if close is None or close <= 0:
        report.notes.append("no usable close on the as-of bar")
        return report

    for index in range(2, len(bars) - 2):
        current = bars[index]
        high = _f(current.high)
        if high is None or high <= close:
            continue
        neighbours = [_f(bars[offset].high) for offset in range(index - 2, index + 3) if offset != index]
        if any(value is None for value in neighbours) or high < max(neighbours):
            continue

        run_up_base = _f(bars[max(0, index - 5)].close)
        surged = (
            run_up_base is not None
            and run_up_base > 0
            and (high / run_up_base - 1.0) * 100.0 >= surge_threshold_pct
        )
        volume_at_level = _f(current.volume)

        # Volume is what turns a price into supply. A high with no volume behind
        # it is a number on a chart, so it is recorded as plain resistance.
        if surged:
            kind = ObstacleKind.PRIOR_SURGE_HIGH
        elif volume_at_level and _average_volume(bars) and volume_at_level > _average_volume(bars) * 1.5:
            kind = ObstacleKind.SUPPLY_OVERHANG
        else:
            kind = ObstacleKind.HORIZONTAL_RESISTANCE

        report.obstacles.append(
            PriceObstacle(
                security_id=security_id,
                as_of_date=series.as_of,
                kind=kind,
                price_level=high,
                distance_pct=(high / close - 1.0) * 100.0,
                established_on=current.trade_date,
                volume_at_level=volume_at_level,
                touch_count=_touches(bars, high),
                strength_note=(
                    "a surge high: the level most likely to be misread as a target, which is why it is "
                    "recorded here and nowhere else"
                    if surged
                    else None
                ),
            )
        )

    if round_number_step:
        step = round_number_step
        level = (int(close / step) + 1) * step
        report.obstacles.append(
            PriceObstacle(
                security_id=security_id,
                as_of_date=series.as_of,
                kind=ObstacleKind.ROUND_NUMBER,
                price_level=level,
                distance_pct=(level / close - 1.0) * 100.0,
                strength_note="round number; weak on its own and worth noting only when it coincides with another level",
            )
        )

    report.obstacles.sort(key=lambda obstacle: obstacle.price_level)
    return report


def _average_volume(bars) -> float | None:
    values = [_f(bar.volume) for bar in bars[-20:]]
    values = [value for value in values if value is not None]
    return (sum(values) / len(values)) if values else None


def _touches(bars, level: float, *, tolerance_pct: float = 1.0) -> int:
    touches = 0
    for bar in bars:
        high, low = _f(bar.high), _f(bar.low)
        if high is None or low is None:
            continue
        if low <= level * (1 + tolerance_pct / 100) and high >= level * (1 - tolerance_pct / 100):
            touches += 1
    return touches


def mark_weakened(obstacle: PriceObstacle, evidence: str) -> PriceObstacle:
    """Record that an obstacle has lost force, and why.

    Overhang expires. A level traded through on heavy volume, or repriced by new
    material, no longer holds the sellers it used to. The project forbids a rule
    that mechanically lowers the reachable zone for every old high, and this is
    how the exception is expressed: as evidence attached to the level, not as a
    decay constant applied to all of them.
    """

    if not evidence.strip():
        raise ValueError("weakening an obstacle needs the evidence written down")
    return PriceObstacle(
        security_id=obstacle.security_id,
        as_of_date=obstacle.as_of_date,
        kind=obstacle.kind,
        price_level=obstacle.price_level,
        distance_pct=obstacle.distance_pct,
        established_on=obstacle.established_on,
        volume_at_level=obstacle.volume_at_level,
        touch_count=obstacle.touch_count,
        strength_note=obstacle.strength_note,
        weakening_evidence=evidence,
    )
