"""Phase 8: watch, entry, prediction, episode.

Status: IMPLEMENTED_NOT_LIVE_VERIFIED. No live intraday price provider is
settled (D-102 / D-103 / D-06b), so nothing here has run against a real price.
The interfaces, the guards and the pipeline are complete; the missing piece is
the data, and every guard is written so that missing data fails loudly rather
than producing a prediction with a hole in it.
"""

from surge.entry.decision import decide
from surge.entry.episode import EpisodeBook, Horizon
from surge.entry.models import (
    AnalysisKind,
    DecisionState,
    EntryAttemptStatus,
    EntryRequest,
    VerificationStatus,
    WatchState,
)
from surge.entry.watch import Watch

__all__ = [
    "AnalysisKind",
    "DecisionState",
    "EntryAttemptStatus",
    "EntryRequest",
    "EpisodeBook",
    "Horizon",
    "VerificationStatus",
    "Watch",
    "WatchState",
    "decide",
]
