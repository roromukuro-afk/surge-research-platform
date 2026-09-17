"""What the outcome engine reads and what it produces.

Two layers, kept in separate types so they cannot be confused:

``PrimaryOutcome``
    The formal answer. Stops when the episode stops, is judged against
    ``initial_failure_line`` only, and is never revised upward afterwards.
``CounterfactualOutcome``
    The research answer. Runs to S20's close whatever the episode did. A stock
    that reached +20% after its thesis was invalidated belongs here and nowhere
    else: the thesis was wrong, and the stock rising later does not make it right.

Everything is computed in the security's own currency. There is no FX input to
this module at all, which is the enforcement of CLAUDE.md 1-9: a +17% move in USD
cannot become a +20% success because the yen moved.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

OUTCOME_ENGINE_VERSION = "outcome-engine-1.0.0"

#: Session ratios a split or reverse split would produce. Used only to *suspect*
#: an unrecorded action, never to apply one.
SUSPECT_RATIOS = (
    Decimal(2),
    Decimal(3),
    Decimal(4),
    Decimal(5),
    Decimal(10),
    Decimal(1) / Decimal(2),
    Decimal(1) / Decimal(3),
    Decimal(1) / Decimal(4),
    Decimal(1) / Decimal(5),
    Decimal(1) / Decimal(10),
)
SUSPECT_TOLERANCE = Decimal("0.02")


class PathResolution(StrEnum):
    """Mirrors ``prod.path_resolution``."""

    TARGET_FIRST = "TARGET_FIRST"
    FAILURE_FIRST = "FAILURE_FIRST"
    NEITHER_BY_HORIZON = "NEITHER_BY_HORIZON"
    #: Finer data does not exist for this market, so the order is unknowable.
    AMBIGUOUS_PATH = "AMBIGUOUS_PATH"
    #: Finer data should exist and did not arrive. A different problem, and a
    #: fixable one, which is why it is a different value (D-09b).
    UNRESOLVED_MISSING_DATA = "UNRESOLVED_MISSING_DATA"


class Granularity(StrEnum):
    SESSION_OPEN = "SESSION_OPEN"
    DAY = "DAY"
    INTRADAY_BAR = "INTRADAY_BAR"
    TRADE = "TRADE"


class PrimaryVerdict(StrEnum):
    """Mirrors ``prod.episode_close_reason``."""

    TARGET_HIT = "TARGET_HIT"
    INITIAL_FAILURE_HIT = "INITIAL_FAILURE_HIT"
    THESIS_INVALIDATED = "THESIS_INVALIDATED"
    HORIZON_EXPIRED = "HORIZON_EXPIRED"
    AMBIGUOUS_PATH = "AMBIGUOUS_PATH"
    UNRESOLVED_MISSING_DATA = "UNRESOLVED_MISSING_DATA"
    CORPORATE_ACTION_SUSPECTED = "CORPORATE_ACTION_SUSPECTED"


class OutcomeError(RuntimeError):
    """A condition under which no outcome may be recorded."""


@dataclass(frozen=True)
class Trade:
    at: datetime
    price: Decimal


@dataclass(frozen=True)
class IntradayBar:
    """One minute bar (or whatever the finest available interval is)."""

    starts_at: datetime
    ends_at: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal


@dataclass(frozen=True)
class Session:
    """One trading session's raw, unadjusted data.

    ``intraday`` and ``trades`` are what we *have*. Whether finer data exists at
    all is a separate question, and it is answered by the two ``*_exist`` flags
    rather than inferred from an empty list - the difference between "the market
    does not publish this" and "our fetch failed" is exactly the difference
    between AMBIGUOUS_PATH and UNRESOLVED_MISSING_DATA (D-09b).
    """

    index: int
    trade_date: date
    open: Decimal | None
    high: Decimal | None
    low: Decimal | None
    close: Decimal | None
    currency: str
    intraday: tuple[IntradayBar, ...] = ()
    trades: tuple[Trade, ...] = ()
    intraday_exists_for_this_market: bool = True
    trades_exist_for_this_market: bool = True

    @property
    def is_complete(self) -> bool:
        return None not in (self.open, self.high, self.low, self.close)


@dataclass(frozen=True)
class SplitAction:
    """A share-count action, and only a share-count action.

    A cash dividend is not here. It changes total return, not the number of
    shares, and this project does not add dividends to the +20% target
    (CLAUDE.md 1-9).
    """

    ex_date: date
    #: New shares per old share. 2-for-1 is 2; 1-for-10 is 0.1.
    ratio: Decimal
    action_id: str


@dataclass(frozen=True)
class Resolution:
    """How one session ended, if it ended anything."""

    resolution: PathResolution | None
    granularity: Granularity | None = None
    session_index: int | None = None
    at: datetime | None = None
    detail: str | None = None

    @property
    def decided(self) -> bool:
        return self.resolution is not None


@dataclass
class PrimaryOutcome:
    verdict: PrimaryVerdict
    path_resolution: PathResolution | None = None
    granularity: Granularity | None = None
    resolved_session_index: int | None = None
    resolved_at: datetime | None = None
    detail: str | None = None

    @property
    def is_a_success(self) -> bool:
        return self.verdict is PrimaryVerdict.TARGET_HIT

    @property
    def is_unresolved(self) -> bool:
        return self.verdict in (
            PrimaryVerdict.AMBIGUOUS_PATH,
            PrimaryVerdict.UNRESOLVED_MISSING_DATA,
            PrimaryVerdict.CORPORATE_ACTION_SUSPECTED,
        )


@dataclass
class CounterfactualOutcome:
    """Research only. Never the answer to "was this prediction right"."""

    path_resolution: PathResolution | None = None
    later_target_hit: bool = False
    later_target_hit_at: datetime | None = None
    later_target_hit_session_index: int | None = None
    #: Maximum favourable / adverse excursion as a fraction of the entry price,
    #: on the comparable path, over S0..S20.
    mfe: Decimal | None = None
    mae: Decimal | None = None
    sessions_observed: int = 0


@dataclass
class OutcomeReport:
    episode_id: str
    entry_reference_price: Decimal
    target_price: Decimal
    initial_failure_line: Decimal
    currency: str
    primary: PrimaryOutcome
    counterfactual: CounterfactualOutcome
    corporate_action_ids_applied: tuple[str, ...] = ()
    engine_version: str = OUTCOME_ENGINE_VERSION
    notes: list[str] = field(default_factory=list)

    @property
    def closes_the_episode(self) -> bool:
        """Whether this outcome ends the episode.

        An unresolved path does end it - the episode is finished with - but it is
        neither a success nor a failure, and the counts are kept separately so
        that "we could not tell" never quietly becomes one or the other.
        """

        return True

    @property
    def summary(self) -> dict:
        return {
            "primary": self.primary.verdict.value,
            "primary_path": self.primary.path_resolution.value
            if self.primary.path_resolution
            else None,
            "granularity": self.primary.granularity.value if self.primary.granularity else None,
            "resolved_session": self.primary.resolved_session_index,
            "counterfactual_path": self.counterfactual.path_resolution.value
            if self.counterfactual.path_resolution
            else None,
            "later_target_hit": self.counterfactual.later_target_hit,
            "mfe": str(self.counterfactual.mfe) if self.counterfactual.mfe is not None else None,
            "mae": str(self.counterfactual.mae) if self.counterfactual.mae is not None else None,
            "sessions_observed": self.counterfactual.sessions_observed,
            "corporate_actions": list(self.corporate_action_ids_applied),
        }
