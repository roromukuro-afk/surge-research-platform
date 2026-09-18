"""The confirmed close of one session: the price an after-close prediction starts from.

Predictions are made after the market has closed (D-261), and the price they
refer to is that session's confirmed close - ``signal_reference_price`` in the
vocabulary of CLAUDE.md 1-4, "分析基準価格" in Canonical v5.1. It is *before* the
decision by construction, and that is correct here: the requirement is not
"after the decision" but "the session's final close, read after the session
ended".

So the one rule this module enforces is about *when the close was read*: a
close is confirmed only if it was fetched after the session's regular end plus
the feed's publication delay. Read any earlier and it may be a mid-session
price wearing a close's name - the chart endpoints return today's bar with the
current price in it until the session is over.

Real-time polling plays no part in producing a prediction.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal


class SessionCloseUnavailable(RuntimeError):
    """No close could be read for this session."""


class SessionCloseNotConfirmed(SessionCloseUnavailable):
    """A value came back, but not yet a *confirmed* close.

    The session has not ended, or the feed's delay since the end has not
    passed, or the closing print has not been published yet.
    """


@dataclass(frozen=True)
class SessionClose:
    """One session's close, with where it came from and when it was read."""

    market_code: str
    symbol: str
    session_date: date
    close: Decimal
    currency: str
    #: The regular session's end, UTC.
    session_closed_at: datetime
    #: When this process finished reading it, UTC.
    fetched_at: datetime
    provider: str
    feed: str
    #: How the provider describes the series (consolidated tape, daily chart...).
    basis: str
    #: The publication delay the feed has after the session end.
    publication_delay: timedelta = timedelta(0)
    evidence: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        for name in ("session_closed_at", "fetched_at"):
            if getattr(self, name).tzinfo is None:
                raise ValueError(f"{name} must carry a zone; a naive time cannot be ordered")
        if self.close <= 0:
            raise ValueError(f"a close must be positive, got {self.close}")

    @property
    def confirmed_from(self) -> datetime:
        return self.session_closed_at + self.publication_delay

    @property
    def recorded_evidence(self) -> tuple[str, ...]:
        return (
            *self.evidence,
            f"PROVIDER={self.provider}",
            f"FEED={self.feed}",
            f"BASIS={self.basis}",
            f"SESSION_DATE={self.session_date.isoformat()}",
            f"SESSION_CLOSED_AT={self.session_closed_at.isoformat()}",
            f"FETCHED_AT={self.fetched_at.isoformat()}",
            f"PUBLICATION_DELAY_SECONDS={int(self.publication_delay.total_seconds())}",
        )


def assert_close_is_confirmed(close: SessionClose) -> None:
    """Read after the session ended and after the feed's delay, or refused."""

    if close.fetched_at < close.confirmed_from:
        raise SessionCloseNotConfirmed(
            f"the {close.session_date.isoformat()} close of {close.symbol} was read at "
            f"{close.fetched_at.isoformat()}, before {close.confirmed_from.isoformat()} (the session "
            f"end plus the feed's {int(close.publication_delay.total_seconds() // 60)} minute delay). "
            "Until then it may be a mid-session price, not the close"
        )


__all__ = [
    "SessionClose",
    "SessionCloseNotConfirmed",
    "SessionCloseUnavailable",
    "assert_close_is_confirmed",
]
