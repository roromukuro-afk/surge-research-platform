"""The episode outcome engine.

Walks S0 to S20 over the comparable path and produces both layers in one pass.
The layers diverge at exactly one point: the primary stops when the episode
stops, and the counterfactual does not.

That divergence is the whole reason for the second layer. An episode whose
thesis was invalidated on S4 and which then reached +20% on S12 has a primary
outcome of THESIS_INVALIDATED and a counterfactual ``later_target_hit``. Those
are two true statements about the same episode, and collapsing them into one
would make the wrong call look right.

No FX anywhere. Everything is in the security's own currency, which is how a
+17% move in USD is prevented from becoming a +20% success because the yen moved
(CLAUDE.md 1-9).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal

from surge.outcome.comparable import comparable_path, suspect_unrecorded_action
from surge.outcome.models import (
    OUTCOME_ENGINE_VERSION,
    CounterfactualOutcome,
    OutcomeError,
    OutcomeReport,
    PathResolution,
    PendingReason,
    PrimaryOutcome,
    PrimaryVerdict,
    Session,
    SplitAction,
)
from surge.outcome.path import resolve_session

PRIMARY_HORIZON_SESSIONS = 20

#: S0 through S20 inclusive. The horizon is complete only when all of them
#: have been observed; HORIZON_EXPIRED means "neither line, by S20" and
#: cannot be said before S20 has happened.
SESSIONS_IN_HORIZON = PRIMARY_HORIZON_SESSIONS + 1

_VERDICT_FOR = {
    PathResolution.TARGET_FIRST: PrimaryVerdict.TARGET_HIT,
    PathResolution.FAILURE_FIRST: PrimaryVerdict.INITIAL_FAILURE_HIT,
    PathResolution.NEITHER_BY_HORIZON: PrimaryVerdict.HORIZON_EXPIRED,
    PathResolution.AMBIGUOUS_PATH: PrimaryVerdict.AMBIGUOUS_PATH,
    PathResolution.UNRESOLVED_MISSING_DATA: PrimaryVerdict.UNRESOLVED_MISSING_DATA,
}


def evaluate(
    *,
    episode_id: str,
    entry_reference_price: Decimal,
    target_price: Decimal,
    initial_failure_line: Decimal,
    entry_price_observed_at: datetime,
    currency: str,
    sessions: Sequence[Session],
    actions: Sequence[SplitAction] = (),
    thesis_invalidated_at: datetime | None = None,
    thesis_invalidated_session: int | None = None,
) -> OutcomeReport:
    """Resolve one episode.

    ``sessions`` are the raw, unadjusted sessions from S0 onward, in order. Only
    the first 21 (S0..S20) are consulted: the primary horizon ends at S20's close
    and S21 is not waited for.
    """

    if not sessions:
        raise OutcomeError(
            f"episode {episode_id}: no sessions. An outcome cannot be computed from nothing, and "
            "an empty result would be indistinguishable from 'neither line was reached'"
        )
    if initial_failure_line >= target_price:
        raise OutcomeError(
            f"episode {episode_id}: failure line {initial_failure_line} is not below target "
            f"{target_price}"
        )
    wrong_currency = {s.currency for s in sessions} - {currency}
    if wrong_currency:
        raise OutcomeError(
            f"episode {episode_id}: sessions are in {sorted(wrong_currency)} but the entry price is "
            f"in {currency}. The outcome is computed in the security's own currency and this module "
            "has no FX input, so a mixed series is a data error rather than something to convert"
        )

    # S0..S20 inclusive. Twenty-one sessions, not twenty-two.
    window = list(sessions)[: PRIMARY_HORIZON_SESSIONS + 1]
    entry_date = window[0].trade_date

    path, applied = comparable_path(window, actions, entry_date=entry_date)
    notes: list[str] = []
    if applied:
        notes.append(
            f"{len(applied)} share-count action(s) applied; the series was restated into entry-time "
            "shares and the target and failure line were left where they were"
        )

    suspected = suspect_unrecorded_action(path, actions)
    if suspected is not None:
        # Not finalised, and not given a verdict either. A split scored as a
        # failure is a wrong number nobody would go back and question, and a
        # CORPORATE_ACTION_SUSPECTED written as a *close reason* would close the
        # episode on it. This stays pending until a person looks.
        return OutcomeReport(
            episode_id=episode_id,
            entry_reference_price=entry_reference_price,
            target_price=target_price,
            initial_failure_line=initial_failure_line,
            currency=currency,
            primary=None,
            pending_reason=PendingReason.CORPORATE_ACTION_SUSPECTED,
            counterfactual=CounterfactualOutcome(sessions_observed=len(path)),
            corporate_action_ids_applied=tuple(applied),
            notes=[*notes, "outcome not finalised: " + str(suspected)],
        )

    primary_resolution = None
    primary_stop_index: int | None = None

    # The thesis can be invalidated before anything is reached. It stops the
    # primary layer where it happened and does not touch the counterfactual.
    invalidation_index = thesis_invalidated_session
    if invalidation_index is None and thesis_invalidated_at is not None:
        invalidation_index = next(
            (s.index for s in path if s.trade_date >= thesis_invalidated_at.date()), None
        )

    mfe: Decimal | None = None
    mae: Decimal | None = None
    counterfactual_resolution = None
    later_hit_at: datetime | None = None
    later_hit_index: int | None = None

    for session in path:
        from_time = entry_price_observed_at if session.index == 0 else None
        decision = resolve_session(
            session, target=target_price, failure=initial_failure_line, from_time=from_time
        )

        excursions = _excursions(
            session, entry_reference_price, from_time=from_time, target=target_price
        )
        if excursions.high is not None:
            mfe = excursions.high if mfe is None else max(mfe, excursions.high)
        if excursions.low is not None:
            mae = excursions.low if mae is None else min(mae, excursions.low)

        if (
            primary_resolution is None
            and invalidation_index is not None
            and session.index > invalidation_index
        ):
            primary_resolution = "INVALIDATED"
            primary_stop_index = invalidation_index

        if decision.decided:
            if primary_resolution is None:
                if invalidation_index is not None and session.index > invalidation_index:
                    primary_resolution = "INVALIDATED"
                    primary_stop_index = invalidation_index
                else:
                    primary_resolution = decision
                    primary_stop_index = session.index
            # The counterfactual keeps the *first* resolution it sees, which for
            # an episode that ran to its own end is the same one.
            if counterfactual_resolution is None:
                counterfactual_resolution = decision

        # Whether the price ever got to the target is a separate question from
        # which line it reached first, and the research layer wants it even when
        # the order could not be established. An ambiguous session that touched
        # the target still touched the target.
        if later_hit_index is None and _touched_target(
            session, target_price, from_time=from_time
        ):
            later_hit_index = session.index
            if decision.resolution is PathResolution.TARGET_FIRST:
                later_hit_at = decision.at

    observed = len(path)
    horizon_is_complete = observed >= SESSIONS_IN_HORIZON
    pending_reason = None

    if primary_resolution is None and not horizon_is_complete:
        # Nothing has been reached and the window is not over. There is no
        # verdict to give, and HORIZON_EXPIRED would be a finished-looking
        # record of an unfinished episode.
        primary = None
        pending_reason = PendingReason.HORIZON_INCOMPLETE
        notes.append(
            f"{observed} of the {SESSIONS_IN_HORIZON} sessions (S0..S{PRIMARY_HORIZON_SESSIONS}) have "
            "been observed and neither line has been reached. The outcome is pending, not expired"
        )
    elif primary_resolution is None:
        primary = PrimaryOutcome(
            verdict=PrimaryVerdict.HORIZON_EXPIRED,
            path_resolution=PathResolution.NEITHER_BY_HORIZON,
            resolved_session_index=PRIMARY_HORIZON_SESSIONS,
            detail=f"neither line was reached by S{PRIMARY_HORIZON_SESSIONS}'s close",
        )
    elif primary_resolution == "INVALIDATED":
        primary = PrimaryOutcome(
            verdict=PrimaryVerdict.THESIS_INVALIDATED,
            resolved_session_index=primary_stop_index,
            resolved_at=thesis_invalidated_at,
            detail=(
                "the thesis was invalidated before either line was reached; anything the price did "
                "afterwards belongs to the counterfactual layer"
            ),
        )
    else:
        # Reached. Final as soon as it happens - S20 is not waited for.
        primary = PrimaryOutcome(
            verdict=_VERDICT_FOR[primary_resolution.resolution],
            path_resolution=primary_resolution.resolution,
            granularity=primary_resolution.granularity,
            resolved_session_index=primary_resolution.session_index,
            resolved_at=primary_resolution.at,
            detail=primary_resolution.detail,
        )

    # Three-valued. False only where the whole window was observed and the price
    # was never seen to reach the target; None while that is still unknown.
    if later_hit_index is not None:
        later_target_hit = True
    elif horizon_is_complete:
        later_target_hit = False
    else:
        later_target_hit = None

    counterfactual = CounterfactualOutcome(
        path_resolution=(
            counterfactual_resolution.resolution if counterfactual_resolution else None
        ),
        later_target_hit=later_target_hit,
        later_target_hit_at=later_hit_at,
        later_target_hit_session_index=later_hit_index,
        mfe=mfe,
        mae=mae,
        sessions_observed=observed,
    )

    if (
        primary is not None
        and primary.verdict is PrimaryVerdict.THESIS_INVALIDATED
        and counterfactual.later_target_hit
    ):
        notes.append(
            "the price reached the target after the thesis was invalidated. That is recorded as "
            "counterfactual_later_target_hit and does not make the primary outcome a success "
            "(CLAUDE.md 1-6)"
        )

    return OutcomeReport(
        episode_id=episode_id,
        entry_reference_price=entry_reference_price,
        target_price=target_price,
        initial_failure_line=initial_failure_line,
        currency=currency,
        primary=primary,
        pending_reason=pending_reason,
        counterfactual=counterfactual,
        corporate_action_ids_applied=tuple(applied),
        engine_version=OUTCOME_ENGINE_VERSION,
        notes=notes,
    )


class _Excursions:
    __slots__ = ("high", "low")

    def __init__(self, high: Decimal | None, low: Decimal | None) -> None:
        self.high = high
        self.low = low


def _excursions(
    session: Session, entry: Decimal, *, from_time, target: Decimal
) -> _Excursions:
    """Maximum favourable and adverse excursion within one session, as fractions.

    On S0 the daily bar is not used: it spans the whole session including the
    move that produced the entry, and an MFE computed from it would credit the
    prediction with a price that had already gone.
    """

    if from_time is not None:
        prices: list[Decimal] = [t.price for t in session.trades if t.at > from_time]
        for bar in session.intraday:
            if bar.starts_at >= from_time:
                prices.extend([bar.high, bar.low])
        if not prices:
            return _Excursions(None, None)
        high, low = max(prices), min(prices)
    else:
        high, low = session.high, session.low
        if high is None or low is None:
            return _Excursions(None, None)

    return _Excursions((high - entry) / entry, (low - entry) / entry)


def _touched_target(session: Session, target: Decimal, *, from_time) -> bool:
    if from_time is not None:
        if any(t.price >= target for t in session.trades if t.at > from_time):
            return True
        return any(b.high >= target for b in session.intraday if b.starts_at >= from_time)
    return session.high is not None and session.high >= target


__all__ = ["PRIMARY_HORIZON_SESSIONS", "SESSIONS_IN_HORIZON", "evaluate"]
