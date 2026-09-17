"""The gate between a challenger and production, which currently refuses.

This is the frame, and the frame's most important property right now is that it
says no. There are no real episodes yet, so there are no real labels, so nothing
can honestly be trained or promoted. A promotion pipeline that runs happily on
synthetic data and reports a champion is worse than no pipeline: it produces a
model with a version number and an accuracy figure that mean nothing, and both
survive long after the caveat is forgotten.

So the gate takes the *evidence* for a promotion rather than a request for one,
and every reason it refuses is named. When there is real teacher data, the same
gate passes without being changed - which is the point of writing it now.

CLAUDE.md 1-17 applies here too: no probability is displayed until there is
enough teacher data and a calibration. A model that reports 0.73 without one is
reporting its own opinion of itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

PROMOTION_GATE_VERSION = "promotion-gate-1.0.0"

#: Deliberately round numbers rather than a tuned threshold. There is no basis
#: for a tuned one yet, and a precise-looking minimum would imply there is.
MINIMUM_LABELS = 200
MINIMUM_CLASSES = 3
MINIMUM_FOLDS = 3
MINIMUM_POSITIVE_CLASS = 30


@dataclass(frozen=True)
class Evidence:
    """What is actually known about a challenger."""

    model_version: str
    dataset_name: str
    dataset_policy_version: str
    #: Labels that survived the admission policy.
    admitted_labels: int
    class_counts: dict[str, int]
    folds_evaluated: int
    #: Whether the labels came from episodes that ran against real prices.
    labels_are_live_verified: bool
    #: Whether a calibration exists for whatever probability the model reports.
    calibration_exists: bool = False
    human_approved_by: str | None = None
    champion_version: str | None = None
    challenger_beat_champion: bool | None = None
    evaluated_at: datetime | None = None


@dataclass
class GateDecision:
    promoted: bool
    reasons: list[str] = field(default_factory=list)
    version: str = PROMOTION_GATE_VERSION

    @property
    def summary(self) -> dict:
        return {"promoted": self.promoted, "reasons": list(self.reasons), "gate": self.version}


def evaluate(evidence: Evidence) -> GateDecision:
    """Decide whether a challenger may become the champion.

    Every failing condition is collected rather than short-circuited, because a
    caller who fixes the first reason and re-runs should see the second one
    immediately rather than one at a time.
    """

    reasons: list[str] = []

    if not evidence.labels_are_live_verified:
        reasons.append(
            "the teacher labels did not come from episodes that ran against real prices. A model "
            "trained on fixtures has learned the fixtures"
        )

    if evidence.admitted_labels < MINIMUM_LABELS:
        reasons.append(
            f"{evidence.admitted_labels} admitted label(s), fewer than the {MINIMUM_LABELS} this "
            "gate asks for. The number is round because there is no basis for a tuned one yet"
        )

    classes = {name for name, count in evidence.class_counts.items() if count > 0}
    if len(classes) < MINIMUM_CLASSES:
        reasons.append(
            f"only {len(classes)} label class(es) present. Fewer than {MINIMUM_CLASSES} is a "
            "near-binary target, which is the failure the admission policy exists to prevent"
        )

    thin = {
        name: count
        for name, count in evidence.class_counts.items()
        if 0 < count < MINIMUM_POSITIVE_CLASS
    }
    if thin:
        reasons.append(
            f"class(es) {sorted(thin)} have fewer than {MINIMUM_POSITIVE_CLASS} examples; an "
            "accuracy figure computed over them is noise with a decimal point"
        )

    if evidence.folds_evaluated < MINIMUM_FOLDS:
        reasons.append(
            f"{evidence.folds_evaluated} walk-forward fold(s) evaluated, fewer than {MINIMUM_FOLDS}. "
            "One fold is one period, and one period is an anecdote"
        )

    if evidence.challenger_beat_champion is None:
        reasons.append("no champion comparison was run")
    elif evidence.challenger_beat_champion is False:
        reasons.append(
            f"the challenger did not beat champion {evidence.champion_version or '(unnamed)'}"
        )

    if not evidence.calibration_exists:
        reasons.append(
            "no calibration exists. CLAUDE.md 1-17: a probability is not displayed until there is "
            "one, and a model reporting 0.73 without it is reporting its opinion of itself"
        )

    if evidence.human_approved_by is None:
        reasons.append("no human approval recorded")

    return GateDecision(promoted=not reasons, reasons=reasons)


def describe_current_state() -> GateDecision:
    """What the gate says today, with nothing to weigh.

    Written as a function rather than a comment so the answer is testable and
    stays true only for as long as it is.
    """

    return evaluate(
        Evidence(
            model_version="(none)",
            dataset_name="(none)",
            dataset_policy_version="admission-1.0.0",
            admitted_labels=0,
            class_counts={},
            folds_evaluated=0,
            labels_are_live_verified=False,
        )
    )


__all__ = [
    "MINIMUM_CLASSES",
    "MINIMUM_FOLDS",
    "MINIMUM_LABELS",
    "MINIMUM_POSITIVE_CLASS",
    "PROMOTION_GATE_VERSION",
    "Evidence",
    "GateDecision",
    "describe_current_state",
    "evaluate",
]
