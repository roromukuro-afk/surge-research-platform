"""Champion and challenger, compared on the same folds.

Two rules, both about comparability rather than about which model is better:

* a challenger is compared on the **same folds** as the champion. Different
  folds is different data, and a model evaluated on an easier period is not a
  better model;
* a tie is not a win. A challenger that matches the champion is not an
  improvement, and promoting on a tie makes the champion drift with every run.
"""

from __future__ import annotations

from dataclasses import dataclass, field

CHAMPION_VERSION = "champion-challenger-1.0.0"


class ComparisonError(RuntimeError):
    """A comparison that would not mean anything."""


@dataclass(frozen=True)
class FoldScore:
    fold_index: int
    #: Whatever the primary metric is. Named neutrally because the metric itself
    #: is not settled and naming it "accuracy" here would settle it by accident.
    score: float
    labels_in_test: int


@dataclass
class Comparison:
    champion_version: str | None
    challenger_version: str
    champion_scores: list[FoldScore] = field(default_factory=list)
    challenger_scores: list[FoldScore] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.champion_version is None:
            return
        champion_folds = [s.fold_index for s in self.champion_scores]
        challenger_folds = [s.fold_index for s in self.challenger_scores]
        if sorted(champion_folds) != sorted(challenger_folds):
            raise ComparisonError(
                "the two models were evaluated on different folds. A model scored on an easier "
                f"period is not a better model: champion {sorted(champion_folds)}, challenger "
                f"{sorted(challenger_folds)}"
            )

    @property
    def champion_mean(self) -> float | None:
        return _mean(self.champion_scores)

    @property
    def challenger_mean(self) -> float | None:
        return _mean(self.challenger_scores)

    @property
    def challenger_wins(self) -> bool | None:
        """Strictly better, or it does not win.

        None when there is no champion to compare against: an unopposed
        challenger has not beaten anything, and saying it has would make the
        first model into a champion by default.
        """

        if self.champion_version is None or self.champion_mean is None:
            return None
        if self.challenger_mean is None:
            return None
        return self.challenger_mean > self.champion_mean

    @property
    def folds_evaluated(self) -> int:
        return len(self.challenger_scores)

    @property
    def summary(self) -> dict:
        return {
            "champion": self.champion_version,
            "challenger": self.challenger_version,
            "champion_mean": self.champion_mean,
            "challenger_mean": self.challenger_mean,
            "challenger_wins": self.challenger_wins,
            "folds": self.folds_evaluated,
        }


def _mean(scores: list[FoldScore]) -> float | None:
    if not scores:
        return None
    return sum(s.score for s in scores) / len(scores)


__all__ = ["CHAMPION_VERSION", "Comparison", "ComparisonError", "FoldScore"]
