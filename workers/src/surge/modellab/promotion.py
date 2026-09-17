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

PROMOTION_GATE_VERSION = "promotion-gate-1.1.0"


@dataclass(frozen=True)
class PromotionPolicy:
    """The numbers a promotion has to clear. Mirrors ``research.promotion_policies``.

    These are a **provisional** policy, and the flag is part of the data rather
    than a comment. Nobody has measured what is sufficient here - there is no
    teacher data to measure with - so the numbers are round on purpose. A
    threshold hard-coded in a module reads as settled long after everyone has
    forgotten it was a guess, and "it reached 200 labels" is not the same
    statement as "it has enough data".

    What replaces them is an examination of sample size, class balance,
    variance, walk-forward stability and calibration quality. What does not
    replace them is a challenger that came close.
    """

    policy_version: str
    minimum_labels: int
    minimum_classes: int
    minimum_positive_class: int
    minimum_folds: int
    basis: str
    provisional: bool = True
    requires_live_verified_labels: bool = True
    requires_calibration: bool = True
    requires_human_approval: bool = True

    def __post_init__(self) -> None:
        if self.minimum_classes < 3:
            raise ValueError(
                "fewer than three classes is a near-binary target, which is the failure the "
                "admission policy exists to prevent"
            )


PROVISIONAL_POLICY = PromotionPolicy(
    policy_version="promotion-provisional-1.0.0",
    minimum_labels=200,
    minimum_classes=3,
    minimum_positive_class=30,
    minimum_folds=3,
    provisional=True,
    basis=(
        "Round numbers chosen to be cautious, not derived from anything. There is no teacher data "
        "yet, so there is no basis for a tuned threshold - and a precise-looking minimum would imply "
        "there is."
    ),
)

#: Kept as names so existing callers and tests read the same way. They are the
#: provisional policy's values, not constants of the problem.
MINIMUM_LABELS = PROVISIONAL_POLICY.minimum_labels
MINIMUM_CLASSES = PROVISIONAL_POLICY.minimum_classes
MINIMUM_FOLDS = PROVISIONAL_POLICY.minimum_folds
MINIMUM_POSITIVE_CLASS = PROVISIONAL_POLICY.minimum_positive_class


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
    policy_version: str = PROVISIONAL_POLICY.policy_version
    #: Whether the thresholds this decision used were a measured sufficiency or
    #: a cautious guess. Carried into the audit log so a future reader can tell.
    policy_was_provisional: bool = True

    @property
    def summary(self) -> dict:
        return {
            "promoted": self.promoted,
            "reasons": list(self.reasons),
            "gate": self.version,
            "policy": self.policy_version,
            "policy_was_provisional": self.policy_was_provisional,
        }


def evaluate(
    evidence: Evidence, policy: PromotionPolicy = PROVISIONAL_POLICY
) -> GateDecision:
    """Decide whether a challenger may become the champion.

    Every failing condition is collected rather than short-circuited, because a
    caller who fixes the first reason and re-runs should see the second one
    immediately rather than one at a time.
    """

    reasons: list[str] = []

    if policy.requires_live_verified_labels and not evidence.labels_are_live_verified:
        reasons.append(
            "the teacher labels did not come from episodes that ran against real prices. A model "
            "trained on fixtures has learned the fixtures"
        )

    if evidence.admitted_labels < policy.minimum_labels:
        reasons.append(
            f"{evidence.admitted_labels} admitted label(s), fewer than the {policy.minimum_labels} "
            f"policy {policy.policy_version} asks for"
            + (
                ". That number is provisional: it is a cautious guess, not a measured sufficiency, "
                "so clearing it is not the same as having enough data"
                if policy.provisional
                else ""
            )
        )

    classes = {name for name, count in evidence.class_counts.items() if count > 0}
    if len(classes) < policy.minimum_classes:
        reasons.append(
            f"only {len(classes)} label class(es) present. Fewer than {policy.minimum_classes} is a "
            "near-binary target, which is the failure the admission policy exists to prevent"
        )

    thin = {
        name: count
        for name, count in evidence.class_counts.items()
        if 0 < count < policy.minimum_positive_class
    }
    if thin:
        reasons.append(
            f"class(es) {sorted(thin)} have fewer than {policy.minimum_positive_class} examples; an "
            "accuracy figure computed over them is noise with a decimal point"
        )

    if evidence.folds_evaluated < policy.minimum_folds:
        reasons.append(
            f"{evidence.folds_evaluated} walk-forward fold(s) evaluated, fewer than "
            f"{policy.minimum_folds}. One fold is one period, and one period is an anecdote"
        )

    if evidence.challenger_beat_champion is None:
        reasons.append("no champion comparison was run")
    elif evidence.challenger_beat_champion is False:
        reasons.append(
            f"the challenger did not beat champion {evidence.champion_version or '(unnamed)'}"
        )

    if policy.requires_calibration and not evidence.calibration_exists:
        reasons.append(
            "no calibration exists. CLAUDE.md 1-17: a probability is not displayed until there is "
            "one, and a model reporting 0.73 without it is reporting its opinion of itself"
        )

    if policy.requires_human_approval and evidence.human_approved_by is None:
        reasons.append("no human approval recorded")

    return GateDecision(
        promoted=not reasons,
        reasons=reasons,
        policy_version=policy.policy_version,
        policy_was_provisional=policy.provisional,
    )


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
    "PROVISIONAL_POLICY",
    "Evidence",
    "GateDecision",
    "PromotionPolicy",
    "describe_current_state",
    "evaluate",
]
