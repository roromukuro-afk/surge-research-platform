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
    #: A readable name for this observation. Nothing parses it; it exists so a
    #: person can find the same example again after an identity rule changes.
    observation_key: str | None = None
    episode_id: str | None = None
    #: Identity for a SETUP_NOT_ENTERED observation. Two setups on one security
    #: on one day are two examples, not one.
    setup_id: str | None = None
    entry_attempt_id: str | None = None
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
    def identity(self) -> tuple:
        """What makes this observation distinct, by kind.

        The three kinds are identified by different things, which is why the
        database uses three partial unique indexes rather than one key. "Same
        security, same day" is only an identity for a security nobody surfaced.
        """

        if self.observation_kind is ObservationKind.PREDICTED:
            return ("PREDICTED", self.episode_id, self.label_version)
        if self.observation_kind is ObservationKind.SETUP_NOT_ENTERED:
            return ("SETUP_NOT_ENTERED", self.setup_id, self.label_version)
        return ("ELIGIBLE_ONLY", self.security_id, self.as_of_date, self.label_version)

    @property
    def lineage_key(self) -> str:
        """How a context is matched to this observation.

        The readable key when there is one, otherwise the identity tuple. Both
        are stable across a rebuild of the same day, which is what a match has to
        be: a context joined by row order would attach the wrong input snapshot
        to the wrong example and nothing would ever say so.
        """

        return self.observation_key or "|".join(str(part) for part in self.identity)

    @property
    def is_resolved(self) -> bool:
        return self.path_resolution not in (
            None,
            "AMBIGUOUS_PATH",
            "UNRESOLVED_MISSING_DATA",
        ) and self.primary_episode_outcome != "CORPORATE_ACTION_SUSPECTED"


@dataclass(frozen=True)
class ObservationContext:
    """What the system was looking at when it decided.

    Teacher data is three things: the input snapshot, the decision, and the
    outcome. With only the last two, the input has to be reconstructed later
    from whatever the tables hold then - and a reconstruction quietly includes
    everything that arrived after the decision, which is exactly the thing the
    whole availability model exists to prevent.
    """

    objective_id: str
    information_cutoff_at: datetime
    production_run_id: str | None = None
    universe_run_id: str | None = None
    market_data_run_id: str | None = None
    fx_run_id: str | None = None
    feature_version: str | None = None
    feature_snapshot: dict | None = None
    feature_snapshot_ref: str | None = None
    technical_candidate_ref: str | None = None
    material_candidate_ref: str | None = None
    stage2_assessment_ref: str | None = None
    stage3_output_id: str | None = None
    input_bundle_sha256: str | None = None
    setup_id: str | None = None
    entry_attempt_id: str | None = None
    episode_id: str | None = None
    pipeline_coverage_ref: str | None = None
    collector_coverage_ref: str | None = None
    #: What the collectors had by the cutoff. An example formed on 40% coverage
    #: and one formed on 100% are different examples.
    coverage_snapshot: dict | None = None
    #: Why there is no feature version, when there legitimately is not - an
    #: observation recorded before the feature engine ran, say. Stated rather
    #: than inferred from the null, because "no features" and "nobody wrote down
    #: which features" are different examples and only one is usable.
    no_feature_reason: str | None = None
    verification_status: str = "IMPLEMENTED_NOT_LIVE_VERIFIED"

    @property
    def is_reproducible(self) -> bool:
        """Whether this example could actually be re-derived.

        Deliberately strict. A context that names no run and carries no bundle
        hash records that a decision happened, not what it was made from.
        """

        return bool(self.input_bundle_sha256 and self.production_run_id and self.feature_version)

    def lineage_gaps(self, observation_kind: ObservationKind) -> tuple[str, ...]:
        """What is missing before this example may be trained on.

        Not the same question as :attr:`is_reproducible`. A label with gaps here
        is still stored - the outcome happened and dropping it would bias the
        record - and it is not admitted to a training dataset, because a model
        cannot be taught from a decision whose inputs were never written down.

        The requirements differ by kind because the kinds are different objects.
        A PREDICTED example has an episode and an attempt behind it; an
        ELIGIBLE_ONLY example has neither and never should, so demanding them
        would reject the entire population of securities nobody surfaced - which
        is exactly the part of the teacher set that teaches a model about misses.
        """

        gaps: list[str] = []

        if not self.verification_status:
            gaps.append("no verification_status")
        if self.coverage_snapshot is None:
            gaps.append(
                "no coverage_snapshot: an example formed on partial collection is not the same "
                "example as one formed on complete collection"
            )
        if not self.feature_version and not self.no_feature_reason:
            gaps.append(
                "no feature_version and no stated reason for its absence"
            )
        if self.feature_version and not (self.feature_snapshot or self.feature_snapshot_ref):
            gaps.append(
                f"feature_version {self.feature_version} names a version but no snapshot or "
                "reference, so the values themselves cannot be recovered"
            )
        for name in ("production_run_id", "universe_run_id", "market_data_run_id"):
            if not getattr(self, name):
                gaps.append(f"no {name}")

        if observation_kind is ObservationKind.PREDICTED:
            if not self.episode_id:
                gaps.append("PREDICTED with no episode_id")
            if not self.entry_attempt_id:
                gaps.append("PREDICTED with no entry_attempt_id")
            if not (self.stage3_output_id or self.input_bundle_sha256):
                gaps.append(
                    "PREDICTED with no decision reference: neither a stage 3 output nor an input "
                    "bundle hash, so what the decision was made from is unrecorded"
                )
        elif observation_kind is ObservationKind.SETUP_NOT_ENTERED:
            if not self.setup_id:
                gaps.append("SETUP_NOT_ENTERED with no setup_id")
        # ELIGIBLE_ONLY needs the universe, feature and coverage lineage above
        # and nothing more. It has no decision behind it by definition.

        return tuple(gaps)

    def is_complete_for(self, observation_kind: ObservationKind) -> bool:
        return not self.lineage_gaps(observation_kind)


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
