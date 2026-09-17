"""Phase 11: the model lab frame.

The frame, and nothing that has run. There are no real episodes yet, so there
are no real labels, so nothing can honestly be trained or promoted - and the
promotion gate says so in named reasons rather than by absence.

Writing it now is worth it for one reason: when there is real teacher data, the
same gate passes without being changed, so what it asks for was decided before
anybody wanted a particular answer.
"""

from surge.modellab.champion import Comparison, ComparisonError, FoldScore
from surge.modellab.promotion import Evidence, GateDecision, describe_current_state, evaluate
from surge.modellab.walkforward import Fold, LeakageError, build_folds, split

__all__ = [
    "Comparison",
    "ComparisonError",
    "Evidence",
    "Fold",
    "FoldScore",
    "GateDecision",
    "LeakageError",
    "build_folds",
    "describe_current_state",
    "evaluate",
    "split",
]
