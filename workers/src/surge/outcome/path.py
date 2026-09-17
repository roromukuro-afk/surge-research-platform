"""The path resolution ladder.

The question is not "did it reach +20%" and not "did it hit the failure line".
It is **which one happened first**, and on a session that touched both, the
honest answer is sometimes that we cannot tell.

Five rungs, in order, each only reached when the one above it could not decide:

1. the session open has already crossed one of them;
2. the daily bar touched only one;
3. the finest intraday bars, in time order;
4. the trades inside the one bar that touched both;
5. nothing touched either by S20's close.

Below rung 4 there are two different failures and they are not the same finding:

``AMBIGUOUS_PATH``
    Finer data does not exist for this market. The order is unknowable and will
    stay unknowable, so the episode is closed unresolved.
``UNRESOLVED_MISSING_DATA``
    Finer data should exist and did not arrive. That is a fetch problem, it is
    fixable, and recording it as ambiguity would hide a bug behind a shrug.

Neither is counted as a success or a failure anywhere (CLAUDE.md 1-9). The one
thing this module must never do is pick the convenient side of a tie.
"""

from __future__ import annotations

from decimal import Decimal

from surge.outcome.models import (
    Granularity,
    IntradayBar,
    PathResolution,
    Resolution,
    Session,
)

UNDECIDED = Resolution(resolution=None)


def _crosses_target(price: Decimal | None, target: Decimal) -> bool:
    return price is not None and price >= target


def _crosses_failure(price: Decimal | None, failure: Decimal) -> bool:
    return price is not None and price <= failure


def resolve_session(
    session: Session,
    *,
    target: Decimal,
    failure: Decimal,
    from_time=None,
) -> Resolution:
    """Decide one session, or decline to.

    ``from_time`` restricts the session to what happened after a moment - used
    for S0, where only the part of the session after the entry price was observed
    may count. The daily bar is unusable there, because it contains the move that
    produced the setup in the first place.
    """

    if failure >= target:
        # Not a data problem; a malformed claim. The caller built a prediction
        # whose failure line is at or above its target.
        raise ValueError(f"failure line {failure} is not below target {target}")

    if from_time is not None:
        return _resolve_partial_session(session, target=target, failure=failure, from_time=from_time)

    # --- 1. The open has already crossed.
    if _crosses_target(session.open, target):
        return Resolution(
            PathResolution.TARGET_FIRST,
            Granularity.SESSION_OPEN,
            session.index,
            detail=f"opened at {session.open}, at or above the target {target}",
        )
    if _crosses_failure(session.open, failure):
        return Resolution(
            PathResolution.FAILURE_FIRST,
            Granularity.SESSION_OPEN,
            session.index,
            detail=f"opened at {session.open}, at or below the failure line {failure}",
        )

    if not session.is_complete:
        return Resolution(
            PathResolution.UNRESOLVED_MISSING_DATA,
            session_index=session.index,
            detail="the daily bar is incomplete; a session with no high or low cannot be read",
        )

    touched_target = _crosses_target(session.high, target)
    touched_failure = _crosses_failure(session.low, failure)

    # --- 2. The daily bar touched one, or neither.
    if not touched_target and not touched_failure:
        return UNDECIDED
    if touched_target and not touched_failure:
        return Resolution(
            PathResolution.TARGET_FIRST,
            Granularity.DAY,
            session.index,
            detail=f"high {session.high} reached the target and the low never reached {failure}",
        )
    if touched_failure and not touched_target:
        return Resolution(
            PathResolution.FAILURE_FIRST,
            Granularity.DAY,
            session.index,
            detail=f"low {session.low} reached the failure line and the high never reached {target}",
        )

    # --- 3 and 4. Both in one session. Now the order matters.
    return _resolve_both_touched(session, target=target, failure=failure)


def _resolve_partial_session(
    session: Session, *, target: Decimal, failure: Decimal, from_time
) -> Resolution:
    """S0: only what happened after the entry price was observed.

    The daily bar is deliberately not consulted. It spans the whole session,
    including the move that produced the entry, and reading a high from it would
    credit the prediction with a price that had already passed.
    """

    trades = [t for t in session.trades if t.at > from_time]
    if trades:
        return _from_trades(trades, target=target, failure=failure, session_index=session.index)

    bars = [b for b in session.intraday if b.starts_at >= from_time]
    if bars:
        return _from_bars(bars, target=target, failure=failure, session_index=session.index)

    if not session.intraday_exists_for_this_market and not session.trades_exist_for_this_market:
        return Resolution(
            PathResolution.AMBIGUOUS_PATH,
            Granularity.INTRADAY_BAR,
            session.index,
            detail=(
                "no intraday data exists for this market, so what happened after the entry within "
                "the entry session cannot be ordered"
            ),
        )
    return Resolution(
        PathResolution.UNRESOLVED_MISSING_DATA,
        session_index=session.index,
        detail=(
            "the entry session has no intraday data after the entry moment, and this market "
            "publishes it; the daily bar is not a substitute because it contains the move that "
            "produced the entry"
        ),
    )


def _resolve_both_touched(session: Session, *, target: Decimal, failure: Decimal) -> Resolution:
    if session.intraday:
        decided = _from_bars(
            list(session.intraday), target=target, failure=failure, session_index=session.index
        )
        if decided.decided and decided.resolution not in (
            PathResolution.AMBIGUOUS_PATH,
            PathResolution.UNRESOLVED_MISSING_DATA,
        ):
            return decided
        if decided.resolution is None:
            # The bars exist and neither crossed. That contradicts the daily bar
            # and means the intraday series does not cover the whole session.
            return Resolution(
                PathResolution.UNRESOLVED_MISSING_DATA,
                session_index=session.index,
                detail=(
                    "the daily bar crossed both lines but the intraday bars cross neither; the "
                    "intraday series does not cover the session"
                ),
            )
        # One bar spans both. Fall through to the trades.
        return _resolve_from_trades_or_admit(session, target=target, failure=failure)

    if not session.intraday_exists_for_this_market:
        return _resolve_from_trades_or_admit(session, target=target, failure=failure)

    return Resolution(
        PathResolution.UNRESOLVED_MISSING_DATA,
        session_index=session.index,
        detail=(
            "the session touched both lines and this market publishes intraday bars, but none were "
            "available. This is a collection gap, not an unknowable order"
        ),
    )


def _resolve_from_trades_or_admit(
    session: Session, *, target: Decimal, failure: Decimal
) -> Resolution:
    if session.trades:
        return _from_trades(
            list(session.trades), target=target, failure=failure, session_index=session.index
        )
    if not session.trades_exist_for_this_market:
        return Resolution(
            PathResolution.AMBIGUOUS_PATH,
            Granularity.INTRADAY_BAR,
            session.index,
            detail=(
                "both lines were touched inside one bar and this market does not publish trade "
                "data, so no finer record exists. The order is unknowable rather than unknown"
            ),
        )
    return Resolution(
        PathResolution.UNRESOLVED_MISSING_DATA,
        session_index=session.index,
        detail=(
            "both lines were touched inside one bar and this market publishes trades, but that "
            "window was missing. Fixable, and therefore not ambiguity"
        ),
    )


def _from_bars(
    bars: list[IntradayBar], *, target: Decimal, failure: Decimal, session_index: int
) -> Resolution:
    for bar in sorted(bars, key=lambda b: b.starts_at):
        # A bar that opens already across one line decides it, whatever it does
        # later in the same bar.
        if _crosses_target(bar.open, target):
            return Resolution(
                PathResolution.TARGET_FIRST, Granularity.INTRADAY_BAR, session_index, bar.starts_at,
                f"a bar opened at {bar.open}, at or above the target",
            )
        if _crosses_failure(bar.open, failure):
            return Resolution(
                PathResolution.FAILURE_FIRST, Granularity.INTRADAY_BAR, session_index, bar.starts_at,
                f"a bar opened at {bar.open}, at or below the failure line",
            )
        hit_target = _crosses_target(bar.high, target)
        hit_failure = _crosses_failure(bar.low, failure)
        if hit_target and hit_failure:
            # Both inside one bar. The bars cannot say which came first.
            return Resolution(
                PathResolution.AMBIGUOUS_PATH,
                Granularity.INTRADAY_BAR,
                session_index,
                bar.starts_at,
                "one bar reached both lines",
            )
        if hit_target:
            return Resolution(
                PathResolution.TARGET_FIRST, Granularity.INTRADAY_BAR, session_index, bar.starts_at,
                f"a bar reached {bar.high}, at or above the target",
            )
        if hit_failure:
            return Resolution(
                PathResolution.FAILURE_FIRST, Granularity.INTRADAY_BAR, session_index, bar.starts_at,
                f"a bar reached {bar.low}, at or below the failure line",
            )
    return UNDECIDED


def _from_trades(
    trades: list, *, target: Decimal, failure: Decimal, session_index: int
) -> Resolution:
    ordered = sorted(trades, key=lambda t: t.at)
    for index, trade in enumerate(ordered):
        hit_target = _crosses_target(trade.price, target)
        hit_failure = _crosses_failure(trade.price, failure)
        if not hit_target and not hit_failure:
            continue

        # Everything stamped at the same instant as this one. If that group
        # reaches both lines, the provider's own record cannot order them.
        same_moment = [t for t in ordered[index:] if t.at == trade.at]
        group_target = any(_crosses_target(t.price, target) for t in same_moment)
        group_failure = any(_crosses_failure(t.price, failure) for t in same_moment)
        if group_target and group_failure:
            return Resolution(
                PathResolution.AMBIGUOUS_PATH,
                Granularity.TRADE,
                session_index,
                trade.at,
                f"trades at {trade.at.isoformat()} reached both lines with no order between them",
            )

        return Resolution(
            PathResolution.TARGET_FIRST if hit_target else PathResolution.FAILURE_FIRST,
            Granularity.TRADE,
            session_index,
            trade.at,
            f"first trade to reach either line was {trade.price} at {trade.at.isoformat()}",
        )
    return UNDECIDED
