"""The outcome of one evaluation sample, computed by code and frozen before any answer is read.

Base = the S0 close as traded. The window is S1..S20, T+n being the n-th session
after S0 on the market's calendar (sessions that actually traded, D-142).
Prices after S0 are restated into S0's share count before comparison
(CLAUDE.md 1-9): a split in the window must not read as a crash or a surge.

D-268 is open, so +20% is measured both ways - on the intraday high and on the
close - and neither is the label. ``success_label`` is not computed here and is
not merged with either (D-267).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from surge.evaluation.population import Sample
from surge.evaluation.prices import History

OUTCOME_VERSION = "eval-outcome-1.0.0"
HORIZON = 20
CHECKPOINTS = (1, 3, 5, 10, 20)
TARGET = Decimal("1.20")
OUTCOME_DEFINITION = {
    "version": OUTCOME_VERSION,
    "base": "S0 close as traded (split restatement undone)",
    "window": "S1..S20, T+n = n-th market session after S0",
    "share_basis": "prices after S0 restated to S0's share count",
    "hit_20_high": "some intraday high in S1..S20 >= base x 1.20",
    "hit_20_close": "some close in S1..S20 >= base x 1.20",
    "returns": "close(T+n) / base - 1 for n in 1, 3, 5, 10, 20",
    "max_upside": "max high (and max close) in S1..S20 / base - 1",
    "max_drawdown": "min low (and min close) in S1..S20 / base - 1",
    "missing": "a session without a bar makes the outcome UNRESOLVED_MISSING_DATA; nothing is filled in",
    "label_status": "neither hit definition is the label (D-268); not merged with success_label",
}


@dataclass(frozen=True)
class Outcome:
    sample_id: str
    resolution: str
    base: Decimal
    window: list[str]
    values: dict

    def row(self) -> dict:
        return {"sample_id": self.sample_id, "resolution": self.resolution, "base": str(self.base),
                "window_first": self.window[0] if self.window else None,
                "window_last": self.window[-1] if self.window else None,
                "outcome_version": OUTCOME_VERSION, "teacher_admissible": False, **self.values}


def _ratio(value: Decimal | None, base: Decimal) -> float | None:
    return None if value is None else float(value / base - 1)


def compute_outcome(sample: Sample, history: History, sessions: list[date]) -> Outcome:
    s0 = sample.s0
    if s0 not in sessions:
        raise ValueError(f"S0 {s0} is not a market session")
    position = sessions.index(s0)
    window = sessions[position + 1: position + 1 + HORIZON]
    base = sample.s0_close_as_traded
    if len(window) < HORIZON:
        return Outcome(sample.sample_id, "PENDING", base, [d.isoformat() for d in window], {})

    s0_bar = history.bar_on(s0)
    if s0_bar is None or s0_bar.close != base:
        raise ValueError(f"{sample.code} {s0}: the S0 close in history does not match the sample's base")

    def restated(bar_price: Decimal | None, day: date) -> Decimal | None:
        if bar_price is None:
            return None
        factor = Decimal(1)
        for action in history.actions:
            multiplier = action.share_multiplier
            if multiplier is not None and s0 < action.ex_date <= day:
                factor *= multiplier
        return bar_price * factor

    bars = {b.trade_date: b for b in history.bars}
    highs, lows, closes, missing = [], [], [], []
    for day in window:
        bar = bars.get(day)
        if bar is None:
            missing.append(day.isoformat())
            highs.append(None)
            lows.append(None)
            closes.append(None)
            continue
        highs.append(restated(bar.high, day))
        lows.append(restated(bar.low, day))
        closes.append(restated(bar.close, day))

    target = base * TARGET
    first_high = next((i + 1 for i, h in enumerate(highs) if h is not None and h >= target), None)
    first_close = next((i + 1 for i, c in enumerate(closes) if c is not None and c >= target), None)
    known = lambda xs: [x for x in xs if x is not None]  # noqa: E731
    values = {
        "hit_20_high": first_high is not None,
        "hit_20_close": first_close is not None,
        "first_hit_session_high": first_high,
        "first_hit_session_close": first_close,
        **{f"ret_t{n}": _ratio(closes[n - 1], base) for n in CHECKPOINTS},
        "max_upside_high": _ratio(max(known(highs)), base) if known(highs) else None,
        "max_upside_close": _ratio(max(known(closes)), base) if known(closes) else None,
        "max_drawdown_low": _ratio(min(known(lows)), base) if known(lows) else None,
        "max_drawdown_close": _ratio(min(known(closes)), base) if known(closes) else None,
        "missing_sessions": missing,
    }
    resolution = "UNRESOLVED_MISSING_DATA" if missing else "RESOLVED"
    return Outcome(sample.sample_id, resolution, base, [d.isoformat() for d in window], values)


__all__ = ["CHECKPOINTS", "HORIZON", "OUTCOME_DEFINITION", "OUTCOME_VERSION", "Outcome", "compute_outcome"]
