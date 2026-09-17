"""Why a +20% move was missed, in three kinds that must not merge.

A security this system never entered rose 20%. That is one observation and three
completely different findings:

``ACTIONABLE_FALSE_NEGATIVE``
    The information was available to the system before the cutoff and the
    screening or the analysis dropped it. This is the prediction model's failure
    and the only one of the three it should be trained on.
``PIPELINE_MISSED_ACTIONABLE_SIGNAL``
    The market had the information before the cutoff; the system did not,
    because a collector was late or broken. This is the *pipeline's* failure.
    Training a model on it teaches the model to blame itself for an outage.
``OUT_OF_SCOPE_SHOCK``
    Nothing reasonable existed beforehand. Nobody's failure.

The fourth, ``OUT_OF_SCOPE_LATE``, is for a signal that did exist and was already
past the point where an entry was possible.

The rule that makes this hard is the one in CLAUDE.md 1-14 and 1-16: **backfilled
information confirms that something existed; it never becomes something the
system knew.** So the two information sets are built separately here and the
function that decides A is given only the first one. The B documents are used to
answer "did this exist at all", and there is a deliberate check that they never
reach the A input.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from surge.labels.models import InterpretiveLabel, LabelError

MISS_CLASSIFIER_VERSION = "miss-classifier-1.0.0"


@dataclass(frozen=True)
class EvidenceDocument:
    """One document, with both of its times and what a reviewer made of it."""

    document_id: str
    source_key: str
    #: The publisher's own claim. It can be wrong, which is why believing it is
    #: a separate, explicit decision.
    source_published_at: datetime | None
    available_to_model_at: datetime
    #: The research judgement: would this, on its own, reasonably have supported
    #: an entry? Supplied by the adjudicator, not derived here.
    would_have_supported_entry: bool
    source_timestamp_trusted: bool = False
    note: str | None = None

    def was_available_by(self, cutoff: datetime) -> bool:
        return self.available_to_model_at <= cutoff

    def was_published_by(self, cutoff: datetime) -> bool:
        return self.source_published_at is not None and self.source_published_at <= cutoff


@dataclass(frozen=True)
class MissVerdict:
    label: InterpretiveLabel
    reason: str
    #: Documents the system actually had. The only ones the model could have used.
    actionable_documents: tuple[str, ...] = ()
    #: Documents that existed but arrived late. Evidence about the pipeline, and
    #: never an input to the question of what the model should have seen.
    late_documents: tuple[str, ...] = ()
    classifier_version: str = MISS_CLASSIFIER_VERSION

    @property
    def is_a_model_failure(self) -> bool:
        return self.label is InterpretiveLabel.ACTIONABLE_FALSE_NEGATIVE


def visible_at_cutoff(
    documents: Sequence[EvidenceDocument], cutoff: datetime
) -> list[EvidenceDocument]:
    """The only documents an as-of judgement may be shown.

    Exported because the adjudicator needs it too: whatever decides
    ``would_have_supported_entry`` for step A must be looking at this list and
    not at the full one.
    """

    return [d for d in documents if d.was_available_by(cutoff)]


def late_but_published(
    documents: Sequence[EvidenceDocument], cutoff: datetime
) -> list[EvidenceDocument]:
    """Published before the cutoff, available to us only after it."""

    return [
        d
        for d in documents
        if d.was_published_by(cutoff) and not d.was_available_by(cutoff)
    ]


def classify(
    documents: Sequence[EvidenceDocument],
    *,
    cutoff: datetime,
    hit_20: bool,
    entry_window_had_already_passed: bool = False,
) -> MissVerdict:
    """Decide which of the four a missed move was.

    The order is the rule. A is asked first and only of what the system had; B
    is asked only when A has already answered no, and only to establish that the
    information existed somewhere.
    """

    if not hit_20:
        raise LabelError(
            "a miss classification describes a security whose price did reach +20%. Asking why "
            "something was missed when it did not move is a question about nothing"
        )

    available = visible_at_cutoff(documents, cutoff)
    late = late_but_published(documents, cutoff)

    # --- A. Could the system have caught it with what it actually had?
    supporting = [d for d in available if d.would_have_supported_entry]
    if supporting:
        return MissVerdict(
            label=InterpretiveLabel.ACTIONABLE_FALSE_NEGATIVE,
            reason=(
                f"{len(supporting)} document(s) were available to the system by "
                f"{cutoff.isoformat()} and would reasonably have supported an entry. The screening "
                "or the analysis dropped it"
            ),
            actionable_documents=tuple(d.document_id for d in supporting),
        )

    # --- B. Did the market have it, while we did not?
    late_supporting = [d for d in late if d.would_have_supported_entry]
    if late_supporting:
        untrusted = [d for d in late_supporting if not d.source_timestamp_trusted]
        caveat = (
            f" {len(untrusted)} of these rely on a publication time the source itself declared, "
            "which can be wrong; the classification is only as good as that claim"
            if untrusted
            else ""
        )
        return MissVerdict(
            label=InterpretiveLabel.PIPELINE_MISSED_ACTIONABLE_SIGNAL,
            reason=(
                f"{len(late_supporting)} document(s) were published before {cutoff.isoformat()} but "
                "did not become available to the system until afterwards. This is a collection "
                "failure and not a prediction-model failure" + caveat
            ),
            late_documents=tuple(d.document_id for d in late_supporting),
        )

    # --- The signal existed and the moment had gone.
    if entry_window_had_already_passed:
        return MissVerdict(
            label=InterpretiveLabel.OUT_OF_SCOPE_LATE,
            reason=(
                "a signal existed but the point at which an entry was reasonably possible had "
                "already passed"
            ),
            actionable_documents=tuple(d.document_id for d in available),
        )

    # --- C. Nothing reasonable existed beforehand.
    return MissVerdict(
        label=InterpretiveLabel.OUT_OF_SCOPE_SHOCK,
        reason=(
            "no document available to the system, and none published before the cutoff, would "
            "reasonably have supported an entry. Not a failure of the model or of the pipeline"
        ),
    )


def assert_no_leakage(shown: Sequence[EvidenceDocument], cutoff: datetime) -> None:
    """Refuse an as-of judgement that was shown something from after the cutoff.

    The whole three-way split collapses if the adjudicator deciding
    ``would_have_supported_entry`` for step A saw a document that arrived late.
    It would find the signal obvious, mark it actionable, and a collection
    outage would be recorded as a model failure.
    """

    leaked = [d for d in shown if not d.was_available_by(cutoff)]
    if leaked:
        raise LabelError(
            f"{len(leaked)} document(s) were shown to an as-of judgement despite becoming available "
            f"after {cutoff.isoformat()}: {[d.document_id for d in leaked]}. Backfilled information "
            "confirms that something existed; it does not become something the system knew "
            "(CLAUDE.md 1-14)"
        )


__all__ = [
    "MISS_CLASSIFIER_VERSION",
    "EvidenceDocument",
    "MissVerdict",
    "assert_no_leakage",
    "classify",
    "late_but_published",
    "visible_at_cutoff",
]
