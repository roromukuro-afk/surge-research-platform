"""Phase 9: the outcome engine.

Two layers, never mixed. The primary outcome answers "was this prediction
right"; the counterfactual answers "what did the price do anyway". A +20% move
after a thesis was invalidated is the second question, not the first.

Status: IMPLEMENTED_NOT_LIVE_VERIFIED. No price provider is settled, so the
engine has run against fixtures only.
"""

from surge.outcome.engine import evaluate
from surge.outcome.models import (
    Granularity,
    IntradayBar,
    OutcomeReport,
    PathResolution,
    PrimaryVerdict,
    Session,
    SplitAction,
    Trade,
)

__all__ = [
    "Granularity",
    "IntradayBar",
    "OutcomeReport",
    "PathResolution",
    "PrimaryVerdict",
    "Session",
    "SplitAction",
    "Trade",
    "evaluate",
]
