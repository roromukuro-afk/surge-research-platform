"""Turning an outcome into measured facts.

Every number here comes from the comparable path and from
``entry_reference_price``. None of it is a training target; see
:mod:`surge.labels.models` for why that distinction is load-bearing.

The one thing this module does that looks like a choice is leaving
``hit_20_before_failure`` and ``failure_before_20`` null when the path could not
be resolved. That is not a missing value to be filled in later. "We could not
tell which came first" and "it did not happen" are different facts, and only one
of them is evidence.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from decimal import Decimal

from surge.labels.models import (
    LABEL_VERSION,
    ObjectiveLabel,
    ObservationKind,
)
from surge.outcome.models import OutcomeReport, PathResolution, Session

#: The thresholds the spec asks for. +20% is the one the project scores on; the
#: other two exist so a near miss and a runaway are distinguishable in research.
THRESHOLDS = {"hit_10": Decimal("1.10"), "hit_20": Decimal("1.20"), "hit_30": Decimal("1.30")}

_UNRESOLVED = {PathResolution.AMBIGUOUS_PATH, PathResolution.UNRESOLVED_MISSING_DATA}


def from_outcome(
    report: OutcomeReport,
    *,
    security_id: str,
    as_of_date: date,
    sessions: Sequence[Session],
    entry_at,
    observation_kind: ObservationKind = ObservationKind.PREDICTED,
) -> ObjectiveLabel:
    """Measure one episode."""

    entry = report.entry_reference_price
    highs = _highs(sessions, entry_at)

    hits = {
        name: (max(highs) >= entry * multiple) if highs else None
        for name, multiple in THRESHOLDS.items()
    }

    days_to_20 = None
    for session, high in _session_highs(sessions, entry_at):
        if high is not None and high >= entry * THRESHOLDS["hit_20"]:
            days_to_20 = session.index
            break

    resolution = report.primary.path_resolution
    unresolved = resolution in _UNRESOLVED or (
        report.primary.verdict.value == "CORPORATE_ACTION_SUSPECTED"
    )

    if unresolved:
        # Both order questions are unanswerable. Neither gets a false.
        before_failure = None
        failure_first = None
    else:
        before_failure = resolution is PathResolution.TARGET_FIRST
        failure_first = resolution is PathResolution.FAILURE_FIRST

    return ObjectiveLabel(
        security_id=security_id,
        as_of_date=as_of_date,
        observation_kind=observation_kind,
        episode_id=report.episode_id if observation_kind is ObservationKind.PREDICTED else None,
        reference_price=entry,
        reference_currency=report.currency,
        path_resolution=resolution.value if resolution else None,
        resolution_granularity=(
            report.primary.granularity.value if report.primary.granularity else None
        ),
        resolved_session_index=report.primary.resolved_session_index,
        hit_10=hits["hit_10"],
        hit_20=hits["hit_20"],
        hit_30=hits["hit_30"],
        days_to_20=days_to_20,
        mfe=report.counterfactual.mfe,
        mae=report.counterfactual.mae,
        failure_line_hit=(
            None if unresolved else resolution is PathResolution.FAILURE_FIRST
        ),
        hit_20_before_failure=before_failure,
        failure_before_20=failure_first,
        primary_episode_outcome=report.primary.verdict.value,
        counterfactual_later_target_hit=report.counterfactual.later_target_hit,
        label_version=LABEL_VERSION,
    )


def for_a_security_never_entered(
    *,
    security_id: str,
    as_of_date: date,
    reference_price: Decimal,
    currency: str,
    sessions: Sequence[Session],
    entry_at,
    observation_kind: ObservationKind = ObservationKind.ELIGIBLE_ONLY,
) -> ObjectiveLabel:
    """Measure a security nobody predicted on.

    These are most of the teacher set and are the reason it can contain a miss at
    all. Without them the population is exactly the set of things this system
    already believed, and a model trained on it can only learn to agree.
    """

    if observation_kind is ObservationKind.PREDICTED:
        raise ValueError("this is the path for securities with no episode")

    highs = _highs(sessions, entry_at)
    hits = {
        name: (max(highs) >= reference_price * multiple) if highs else None
        for name, multiple in THRESHOLDS.items()
    }
    days_to_20 = None
    for session, high in _session_highs(sessions, entry_at):
        if high is not None and high >= reference_price * THRESHOLDS["hit_20"]:
            days_to_20 = session.index
            break

    lows = [s.low for s in sessions if s.low is not None]
    return ObjectiveLabel(
        security_id=security_id,
        as_of_date=as_of_date,
        observation_kind=observation_kind,
        reference_price=reference_price,
        reference_currency=currency,
        hit_10=hits["hit_10"],
        hit_20=hits["hit_20"],
        hit_30=hits["hit_30"],
        days_to_20=days_to_20,
        mfe=(max(highs) - reference_price) / reference_price if highs else None,
        mae=(min(lows) - reference_price) / reference_price if lows else None,
        # There is no prediction here, so there is no failure line and no path to
        # resolve. Leaving these null is the point: a security nobody entered has
        # no "did it fail first" answer.
        label_version=LABEL_VERSION,
    )


def _session_highs(sessions: Sequence[Session], entry_at):
    for session in sessions:
        if session.index == 0:
            after = [t.price for t in session.trades if t.at > entry_at]
            after += [b.high for b in session.intraday if b.starts_at >= entry_at]
            yield session, (max(after) if after else None)
        else:
            yield session, session.high


def _highs(sessions: Sequence[Session], entry_at) -> list[Decimal]:
    return [high for _, high in _session_highs(sessions, entry_at) if high is not None]


__all__ = ["THRESHOLDS", "for_a_security_never_entered", "from_outcome"]
