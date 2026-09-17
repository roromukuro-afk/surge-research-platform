"""The teacher vocabulary.

The objective layer is not a label. It is a set of measurements, and the
distinction matters because "rose 20% within twenty sessions" is trivially
computable, looks exactly like ground truth, and teaches a model to predict price
moves rather than to predict *this system's decisions being right*. Those are
different questions and only the second one has a failure line in it.

What a model is trained on is an interpretive label: a judgement, with a
confidence, a review status, the evidence it rested on, and a cutoff saying what
it was allowed to see.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

LABEL_VERSION = "objective-labels-1.0.0"
LABELER_VERSION = "rule-based-labeler-1.0.0"


class ObservationKind(StrEnum):
    """How this row came to be in the teacher set.

    All three are in it. A set consisting only of ``PREDICTED`` would teach a
    model what this system already believed, and every miss would be invisible
    to it.
    """

    PREDICTED = "PREDICTED"
    SETUP_NOT_ENTERED = "SETUP_NOT_ENTERED"
    ELIGIBLE_ONLY = "ELIGIBLE_ONLY"


class InterpretiveLabel(StrEnum):
    PREDICTIVE_SUCCESS = "PREDICTIVE_SUCCESS"
    STATE_CONFIRMED_SUCCESS = "STATE_CONFIRMED_SUCCESS"
    PRICE_SUCCESS_EXOGENOUS = "PRICE_SUCCESS_EXOGENOUS"
    FALSE_POSITIVE = "FALSE_POSITIVE"
    FAILED_BEFORE_TARGET = "FAILED_BEFORE_TARGET"
    PRICED_IN_ERROR = "PRICED_IN_ERROR"
    REACHABLE_ZONE_ERROR = "REACHABLE_ZONE_ERROR"
    DISTRIBUTION_ERROR = "DISTRIBUTION_ERROR"
    FALSE_PULLBACK = "FALSE_PULLBACK"
    ACTIONABLE_FALSE_NEGATIVE = "ACTIONABLE_FALSE_NEGATIVE"
    PIPELINE_MISSED_ACTIONABLE_SIGNAL = "PIPELINE_MISSED_ACTIONABLE_SIGNAL"
    OUT_OF_SCOPE_SHOCK = "OUT_OF_SCOPE_SHOCK"
    OUT_OF_SCOPE_LATE = "OUT_OF_SCOPE_LATE"


SUCCESS_LABELS = frozenset(
    {
        InterpretiveLabel.PREDICTIVE_SUCCESS,
        InterpretiveLabel.STATE_CONFIRMED_SUCCESS,
        InterpretiveLabel.PRICE_SUCCESS_EXOGENOUS,
    }
)

#: The four that describe a security nobody entered. Three of them are not
#: prediction-model failures at all.
MISS_LABELS = frozenset(
    {
        InterpretiveLabel.ACTIONABLE_FALSE_NEGATIVE,
        InterpretiveLabel.PIPELINE_MISSED_ACTIONABLE_SIGNAL,
        InterpretiveLabel.OUT_OF_SCOPE_SHOCK,
        InterpretiveLabel.OUT_OF_SCOPE_LATE,
    }
)

#: The only miss that belongs in prediction-model training. The other three are
#: a collection problem, an unforeseeable move, and a timing problem.
MODEL_TRAINABLE_MISSES = frozenset({InterpretiveLabel.ACTIONABLE_FALSE_NEGATIVE})


class ReviewStatus(StrEnum):
    UNREVIEWED = "UNREVIEWED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    AMENDED = "AMENDED"


class LabelError(RuntimeError):
    """A label that could not honestly be produced."""


@dataclass(frozen=True)
class ObjectiveLabel:
    """Measured facts about one security's path from one date."""

    security_id: str
    as_of_date: date
    observation_kind: ObservationKind
    label_version: str = LABEL_VERSION
    episode_id: str | None = None
    reference_price: Decimal | None = None
    reference_currency: str | None = None
    path_resolution: str | None = None
    resolution_granularity: str | None = None
    resolved_session_index: int | None = None
    hit_10: bool | None = None
    hit_20: bool | None = None
    hit_30: bool | None = None
    days_to_20: int | None = None
    mfe: Decimal | None = None
    mae: Decimal | None = None
    failure_line_hit: bool | None = None
    #: Null, not false, when the path could not be resolved. "We could not tell"
    #: and "it did not happen" are different facts and only one is evidence.
    hit_20_before_failure: bool | None = None
    failure_before_20: bool | None = None
    primary_episode_outcome: str | None = None
    counterfactual_later_target_hit: bool | None = None

    @property
    def is_resolved(self) -> bool:
        return self.path_resolution not in (
            None,
            "AMBIGUOUS_PATH",
            "UNRESOLVED_MISSING_DATA",
        ) and self.primary_episode_outcome != "CORPORATE_ACTION_SUSPECTED"


@dataclass(frozen=True)
class InterpretiveJudgement:
    """One hypothesis about why the path went the way it did."""

    objective: ObjectiveLabel
    label: InterpretiveLabel
    labeler_model_version: str
    information_cutoff_at: datetime
    evidence: dict
    input_sha256: str
    confidence: float | None = None
    prompt_version: str | None = None
    human_review_status: ReviewStatus = ReviewStatus.UNREVIEWED
    supersedes_label_id: str | None = None

    @property
    def is_a_model_failure(self) -> bool:
        """Whether this label says the prediction model got it wrong.

        Three of the four miss labels answer no. A collector outage and an
        unforeseeable move are not the model's failures, and counting them as
        such would make the model look worse while hiding the real problem.
        """

        if self.label in MISS_LABELS:
            return self.label in MODEL_TRAINABLE_MISSES
        return self.label not in SUCCESS_LABELS


@dataclass
class LabelSet:
    """What one labelling pass produced."""

    objective: list[ObjectiveLabel] = field(default_factory=list)
    interpretive: list[InterpretiveJudgement] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def summary(self) -> dict:
        by_kind: dict[str, int] = {}
        for label in self.objective:
            by_kind[label.observation_kind.value] = by_kind.get(label.observation_kind.value, 0) + 1
        by_label: dict[str, int] = {}
        for judgement in self.interpretive:
            by_label[judgement.label.value] = by_label.get(judgement.label.value, 0) + 1
        return {
            "objective": len(self.objective),
            "by_observation_kind": by_kind,
            "interpretive": len(self.interpretive),
            "by_label": by_label,
        }
