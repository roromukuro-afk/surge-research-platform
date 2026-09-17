"""What a model may be trained on.

A dataset is built through a versioned policy or it is not built. The policy
records what it admitted *and what it rejected and why*, because a dataset that
lists only its contents cannot be audited for what it dropped.

One rule is not configurable: a target with fewer than three classes is refused.
Two classes here is almost always "did it rise 20%" wearing a different name, and
that label teaches a model to predict price moves rather than to predict this
system's decisions being right. The rest of the policy - the confidence floor,
the review requirement, which labels count - is versioned and may change; this
one is a constraint.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from surge.labels.models import (
    MISS_LABELS,
    MODEL_TRAINABLE_MISSES,
    InterpretiveJudgement,
    InterpretiveLabel,
    LabelError,
    ObservationContext,
    ReviewStatus,
)

MINIMUM_TARGET_CLASSES = 3


class DatasetPurpose(StrEnum):
    """What a built dataset is for, because the two have different obligations.

    ``PRODUCTION_TRAINING`` is the default on purpose. A dataset that does not
    say what it is for is treated as one a model will be trained on, and so it
    must carry the input side of every example it admits. The alternative
    default - assume research, require nothing - would let an unlineaged set
    become a training set simply by nobody having said otherwise.
    """

    PRODUCTION_TRAINING = "PRODUCTION_TRAINING"
    RESEARCH_ONLY = "RESEARCH_ONLY"

    @property
    def requires_lineage(self) -> bool:
        return self is DatasetPurpose.PRODUCTION_TRAINING


#: Kept as a label, excluded from a predictive target. It means the price rose
#: and the entry thesis does not account for it, so training on it teaches a
#: model to take credit for outcomes its own reasoning did not anticipate. A
#: SPECIAL_PURPOSE policy may admit it by saying so.
NOT_IN_A_PREDICTIVE_TARGET = frozenset({InterpretiveLabel.PRICE_SUCCESS_EXOGENOUS})

#: Refused as a training target however it is spelled. Each of these is a single
#: binary derived from the price path alone.
FORBIDDEN_TARGETS = frozenset({"hit_10", "hit_20", "hit_30", "failure_line_hit"})


@dataclass(frozen=True)
class AdmissionPolicy:
    """Mirrors ``labels.admission_policies``."""

    policy_version: str
    description: str
    admitted_labels: frozenset[InterpretiveLabel]
    min_confidence: float | None = None
    required_review_status: frozenset[ReviewStatus] = frozenset()
    #: A policy built for something other than training a predictive model -
    #: error analysis, say. It may admit labels the default one refuses, and it
    #: has to say why rather than simply setting a flag.
    special_purpose_reason: str | None = None

    @property
    def is_special_purpose(self) -> bool:
        return self.special_purpose_reason is not None

    def __post_init__(self) -> None:
        if len(self.admitted_labels) < MINIMUM_TARGET_CLASSES:
            raise LabelError(
                f"policy {self.policy_version} admits {len(self.admitted_labels)} label class(es). "
                f"A target with fewer than {MINIMUM_TARGET_CLASSES} is almost always 'did it rise "
                "20%' under another name, which teaches a model to predict price moves rather than "
                "to predict this system's decisions being right"
            )
        exogenous = self.admitted_labels & NOT_IN_A_PREDICTIVE_TARGET
        if exogenous and not self.is_special_purpose:
            raise LabelError(
                f"policy {self.policy_version} admits "
                f"{sorted(label.value for label in exogenous)}. That label records a rise the entry "
                "thesis does not explain, so a predictive model trained on it learns to take credit "
                "for luck. A policy that wants it must say what it is for"
            )

        # Three of the four miss labels are not prediction-model failures, and
        # training on them teaches a model to answer for a collector outage.
        wrong_misses = {
            label
            for label in self.admitted_labels
            if label in MISS_LABELS and label not in MODEL_TRAINABLE_MISSES
        }
        if wrong_misses and not self.is_special_purpose:
            raise LabelError(
                f"policy {self.policy_version} admits {sorted(m.value for m in wrong_misses)}. "
                "A pipeline outage, an unforeseeable move and a signal that arrived too late are "
                "not failures of the prediction model, and training on them would teach it to "
                "answer for things it could not have done differently"
            )


@dataclass(frozen=True)
class Rejection:
    label_id: str
    label: InterpretiveLabel
    reason: str


@dataclass
class DatasetManifest:
    """The record of one build, including everything it left out."""

    name: str
    policy_version: str
    knowledge_cutoff: datetime
    purpose: DatasetPurpose = DatasetPurpose.PRODUCTION_TRAINING
    admitted: list[InterpretiveJudgement] = field(default_factory=list)
    rejected: list[Rejection] = field(default_factory=list)
    git_sha: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def rejection_reasons(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for rejection in self.rejected:
            counts[rejection.reason] = counts.get(rejection.reason, 0) + 1
        return counts

    @property
    def class_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for judgement in self.admitted:
            counts[judgement.label.value] = counts.get(judgement.label.value, 0) + 1
        return counts

    @property
    def summary(self) -> dict:
        return {
            "name": self.name,
            "policy_version": self.policy_version,
            "purpose": self.purpose.value,
            "admitted": len(self.admitted),
            "rejected": len(self.rejected),
            "classes": self.class_counts,
            "rejection_reasons": self.rejection_reasons,
        }


def build(
    judgements: Sequence[InterpretiveJudgement],
    *,
    policy: AdmissionPolicy,
    name: str,
    knowledge_cutoff: datetime,
    target_field: str = "label",
    git_sha: str | None = None,
    contexts: Mapping[str, ObservationContext] | None = None,
    purpose: DatasetPurpose = DatasetPurpose.PRODUCTION_TRAINING,
) -> DatasetManifest:
    """Apply a policy and record both sides of it.

    ``contexts`` is the input side of each example, keyed by
    :attr:`ObjectiveLabel.lineage_key`. A production training set admits nothing
    without one: teacher data is input snapshot, decision and outcome, and two
    thirds of that is not teacher data. The label itself is still kept - the
    outcome happened, and dropping it would bias the record of what happened -
    it simply does not enter the dataset.
    """

    if target_field in FORBIDDEN_TARGETS:
        raise LabelError(
            f"{target_field!r} cannot be a training target. It is a single measurement of the price "
            "path, and a model trained on it learns to predict moves rather than to predict this "
            "system's decisions being right - which is the question that has a failure line in it"
        )

    manifest = DatasetManifest(
        name=name,
        policy_version=policy.policy_version,
        purpose=purpose,
        knowledge_cutoff=knowledge_cutoff,
        git_sha=git_sha,
    )
    if purpose.requires_lineage and contexts is None:
        raise LabelError(
            f"dataset {name!r} is a {purpose.value} set and was given no observation contexts. "
            "Teacher data is input snapshot + decision + outcome; a set built from the last two "
            "teaches a model from decisions whose inputs were never written down. Pass the "
            "contexts, or build it as RESEARCH_ONLY and say so"
        )

    for index, judgement in enumerate(judgements):
        label_id = judgement.input_sha256[:16] + f":{index}"
        reason = _rejection_reason(judgement, policy, knowledge_cutoff)
        if reason is None and purpose.requires_lineage:
            reason = _lineage_reason(judgement, contexts or {})
        if reason is None:
            manifest.admitted.append(judgement)
        else:
            manifest.rejected.append(
                Rejection(label_id=label_id, label=judgement.label, reason=reason)
            )

    if not purpose.requires_lineage:
        manifest.notes.append(
            "built as RESEARCH_ONLY: input lineage was not required, so this set must not be used "
            "to train a production model"
        )

    classes = set(manifest.class_counts)
    if manifest.admitted and len(classes) < MINIMUM_TARGET_CLASSES:
        manifest.notes.append(
            f"only {len(classes)} class(es) survived the policy ({sorted(classes)}). The dataset is "
            "built and recorded, and it is not usable as a training target at this size - a "
            "near-binary target is the failure this policy exists to prevent"
        )

    return manifest


def _lineage_reason(
    judgement: InterpretiveJudgement, contexts: Mapping[str, ObservationContext]
) -> str | None:
    """Why this example's inputs are not recorded well enough to train on."""

    objective = judgement.objective
    context = contexts.get(objective.lineage_key)
    if context is None:
        return "no observation context: the inputs this decision was made from were never recorded"

    if context.information_cutoff_at > judgement.information_cutoff_at:
        # The snapshot saw more than the judgement claims to have seen.
        return (
            "the observation context's information cutoff is after the judgement's, so the "
            "snapshot includes things the decision could not have known"
        )

    gaps = context.lineage_gaps(objective.observation_kind)
    if gaps:
        return "incomplete input lineage: " + "; ".join(gaps)
    return None


def _rejection_reason(
    judgement: InterpretiveJudgement, policy: AdmissionPolicy, knowledge_cutoff: datetime
) -> str | None:
    if judgement.label not in policy.admitted_labels:
        return "label is not admitted by this policy"
    if policy.required_review_status and judgement.human_review_status not in policy.required_review_status:
        return f"review status is {judgement.human_review_status.value}"
    if policy.min_confidence is not None:
        if judgement.confidence is None:
            return "no confidence recorded and the policy requires a floor"
        if judgement.confidence < policy.min_confidence:
            return f"confidence below {policy.min_confidence}"
    if judgement.information_cutoff_at > knowledge_cutoff:
        # The judgement saw more than the dataset claims to know.
        return "the judgement's information cutoff is after the dataset's knowledge cutoff"
    if not judgement.objective.is_resolved:
        return "the underlying path was not resolved"
    return None


__all__ = [
    "FORBIDDEN_TARGETS",
    "MINIMUM_TARGET_CLASSES",
    "NOT_IN_A_PREDICTIVE_TARGET",
    "AdmissionPolicy",
    "DatasetManifest",
    "DatasetPurpose",
    "Rejection",
    "build",
]
