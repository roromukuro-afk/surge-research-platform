"""Restating a price series into the share count it had at entry.

The raw, unadjusted price is what was traded and is what we store. It is not
what an outcome can be measured against: a two-for-one split halves the price
overnight, and a failure line sitting below that gap would be "hit" by the split
itself.

So every session from S0 forward is multiplied by the cumulative share ratio of
the actions that went ex between entry and that session::

    comparable_price = raw_price * R,  R = product of ratios with entry < ex_date <= d

The target and the failure line stay where they were at entry. That is the point:
they are expressed in entry-time shares, and moving the series to meet them is
correct while moving them to meet the series would be rewriting the claim.

Cash dividends are absent by construction. A dividend changes total return, not
the share count, and CLAUDE.md 1-9 does not add it to the +20% target.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from surge.outcome.models import (
    SUSPECT_RATIOS,
    SUSPECT_TOLERANCE,
    IntradayBar,
    OutcomeError,
    Session,
    SplitAction,
    Trade,
)


@dataclass(frozen=True)
class SuspectedAction:
    """A gap that looks like a split nobody recorded."""

    trade_date: date
    previous_close: Decimal
    session_open: Decimal
    implied_ratio: Decimal
    nearest_known_ratio: Decimal

    def __str__(self) -> str:  # pragma: no cover - message only
        return (
            f"{self.trade_date}: the open is {self.implied_ratio:.4f}x the previous close, which is "
            f"within {SUSPECT_TOLERANCE:%} of a {self.nearest_known_ratio} share-count change, and "
            "no corporate action is recorded for that date"
        )


def cumulative_ratio(actions: Sequence[SplitAction], *, entry_date: date, through: date) -> Decimal:
    """Shares now per share at entry, for everything that went ex in between.

    The window is ``entry_date < ex_date <= through``: an action that went ex on
    the entry date itself is already in the entry price.
    """

    ratio = Decimal(1)
    for action in actions:
        if action.ratio <= 0:
            raise OutcomeError(
                f"corporate action {action.action_id} has ratio {action.ratio}; a share-count "
                "ratio must be positive, and a zero or negative one is an unreadable record "
                "rather than a number to work around"
            )
        if entry_date < action.ex_date <= through:
            ratio *= action.ratio
    return ratio


def restate(session: Session, ratio: Decimal) -> Session:
    """One session's prices in entry-time shares."""

    if ratio == 1:
        return session

    def scale(value: Decimal | None) -> Decimal | None:
        return None if value is None else value * ratio

    return Session(
        index=session.index,
        trade_date=session.trade_date,
        open=scale(session.open),
        high=scale(session.high),
        low=scale(session.low),
        close=scale(session.close),
        currency=session.currency,
        intraday=tuple(
            IntradayBar(
                starts_at=bar.starts_at,
                ends_at=bar.ends_at,
                open=bar.open * ratio,
                high=bar.high * ratio,
                low=bar.low * ratio,
                close=bar.close * ratio,
            )
            for bar in session.intraday
        ),
        trades=tuple(Trade(at=trade.at, price=trade.price * ratio) for trade in session.trades),
        intraday_exists_for_this_market=session.intraday_exists_for_this_market,
        trades_exist_for_this_market=session.trades_exist_for_this_market,
    )


def comparable_path(
    sessions: Sequence[Session],
    actions: Sequence[SplitAction],
    *,
    entry_date: date,
) -> tuple[list[Session], list[str]]:
    """Restate every session and report which actions were applied."""

    applied: list[str] = []
    out: list[Session] = []
    for session in sessions:
        ratio = cumulative_ratio(actions, entry_date=entry_date, through=session.trade_date)
        out.append(restate(session, ratio))
    for action in actions:
        if entry_date < action.ex_date <= (sessions[-1].trade_date if sessions else entry_date):
            applied.append(action.action_id)
    return out, applied


def suspect_unrecorded_action(
    sessions: Sequence[Session], actions: Sequence[SplitAction]
) -> SuspectedAction | None:
    """A discontinuity consistent with a split, on a date with no recorded action.

    Deliberately biased toward suspicion. The cost of a false suspicion is an
    outcome left unresolved and looked at by a person; the cost of missing a real
    one is a split recorded as a failure, which is a wrong number nobody would
    ever question. A 50% overnight fall can of course be real news - and the
    answer to that is to record the absence of a corporate action, not to guess.
    """

    recorded = {action.ex_date for action in actions}
    previous: Session | None = None
    for session in sessions:
        if previous is not None and previous.close and session.open and previous.close > 0:
            implied = session.open / previous.close
            for known in SUSPECT_RATIOS:
                if abs(implied - known) / known <= SUSPECT_TOLERANCE:
                    if session.trade_date in recorded:
                        break
                    return SuspectedAction(
                        trade_date=session.trade_date,
                        previous_close=previous.close,
                        session_open=session.open,
                        implied_ratio=implied,
                        nearest_known_ratio=known,
                    )
        previous = session
    return None
