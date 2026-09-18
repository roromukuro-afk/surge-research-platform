"""The vocabulary of Phase 8.

Three prices, and they answer three different questions. The reason they are
three fields rather than one is that collapsing them is the single most
flattering mistake this system could make:

``signal_reference_price``
    What the price was when the setup was noticed. Audit only.
``decision_price``
    What the model was looking at when it decided. Reproduces the decision, and
    is the first of the two 3,000 yen checks.
``entry_reference_price``
    What could actually have been bought *after* the decision finished. The only
    price the scoring may use, and therefore the only one that is not optional.

The last one is not a refinement of the first two. A prediction scored against
the price that prompted it is scored against a price nobody could have traded.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum

#: CLAUDE.md 1-4 and the implementation instructions. Not configurable: a
#: threshold that can be raised is not a threshold.
PRICE_LIMIT_JPY = Decimal("3000")
TARGET_MULTIPLE = Decimal("1.20")
PRIMARY_HORIZON_SESSIONS = 20

ENTRY_RULE_VERSION = "entry-decision-1.0.0"


class AnalysisKind(StrEnum):
    """Mirrors ``prod.analysis_kind``."""

    EOD = "EOD"
    POST_CLOSE_MATERIAL = "POST_CLOSE_MATERIAL"
    WATCH_MONITOR = "WATCH_MONITOR"
    ENTRY_DECISION = "ENTRY_DECISION"
    REANALYSIS = "REANALYSIS"


#: The two kinds that run while the market is open. Only these may produce ENTRY:
#: "entry at the current price" has no referent when there is no current price.
INTRADAY_KINDS = frozenset({AnalysisKind.ENTRY_DECISION, AnalysisKind.REANALYSIS})


class DecisionState(StrEnum):
    """Mirrors ``prod.decision_state`` - the seven states of CLAUDE.md 1-4."""

    TECHNICAL_SETUP_EOD = "TECHNICAL_SETUP_EOD"
    POST_CLOSE_CATALYST_SETUP = "POST_CLOSE_CATALYST_SETUP"
    ENTRY = "ENTRY"
    WATCH_BREAKOUT = "WATCH_BREAKOUT"
    WATCH_PULLBACK = "WATCH_PULLBACK"
    WATCH_OTHER = "WATCH_OTHER"
    REJECT = "REJECT"


class WatchState(StrEnum):
    ARMED = "ARMED"
    TRIGGER_HIT = "TRIGGER_HIT"
    IN_REANALYSIS = "IN_REANALYSIS"
    ENTERED = "ENTERED"
    REJECTED = "REJECTED"
    REARMED = "REARMED"
    EXPIRED = "EXPIRED"


class EntryAttemptStatus(StrEnum):
    """What one intraday decision came to.

    Six of the seven produce no prediction. That ratio is the point: an entry
    ledger that only recorded successful entries would make any hit rate
    computed from it meaningless.
    """

    PREDICTION_CREATED = "PREDICTION_CREATED"
    ENTRY_ABORTED_PRICE_LIMIT = "ENTRY_ABORTED_PRICE_LIMIT"
    REJECTED_HARD_FILTER_AT_DECISION = "REJECTED_HARD_FILTER_AT_DECISION"
    REJECTED_BY_ANALYSIS = "REJECTED_BY_ANALYSIS"
    REJECTED_NOT_IN_UNIVERSE = "REJECTED_NOT_IN_UNIVERSE"
    REAFFIRMED_EXISTING_EPISODE = "REAFFIRMED_EXISTING_EPISODE"
    NO_ENTRY_REFERENCE_PRICE = "NO_ENTRY_REFERENCE_PRICE"


class EpisodeStatus(StrEnum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"


class EpisodeCloseReason(StrEnum):
    TARGET_HIT = "TARGET_HIT"
    INITIAL_FAILURE_HIT = "INITIAL_FAILURE_HIT"
    THESIS_INVALIDATED = "THESIS_INVALIDATED"
    HORIZON_EXPIRED = "HORIZON_EXPIRED"
    AMBIGUOUS_PATH = "AMBIGUOUS_PATH"
    UNRESOLVED_MISSING_DATA = "UNRESOLVED_MISSING_DATA"
    CORPORATE_ACTION_SUSPECTED = "CORPORATE_ACTION_SUSPECTED"


class TransitionKind(StrEnum):
    EPISODE_OPENED = "EPISODE_OPENED"
    REAFFIRMED = "REAFFIRMED"
    RISK_LINE_HIT = "RISK_LINE_HIT"
    RISK_LINE_MOVED = "RISK_LINE_MOVED"
    THESIS_INVALIDATED = "THESIS_INVALIDATED"
    TARGET_HIT = "TARGET_HIT"
    INITIAL_FAILURE_HIT = "INITIAL_FAILURE_HIT"
    HORIZON_EXPIRED = "HORIZON_EXPIRED"
    EPISODE_CLOSED = "EPISODE_CLOSED"


class VerificationStatus(StrEnum):
    LIVE_VERIFIED = "LIVE_VERIFIED"
    IMPLEMENTED_NOT_LIVE_VERIFIED = "IMPLEMENTED_NOT_LIVE_VERIFIED"


class EntryError(RuntimeError):
    """A condition under which no decision may be recorded at all.

    Distinct from a rejection. A rejection is a finding about a security; these
    are findings about the call itself, and turning one into a status would put
    a row in the ledger describing a decision that never happened.
    """


class MissingPriceError(EntryError):
    """No decision price. There is no decision to record."""


class StandInProviderError(EntryError):
    """A deterministic stand-in cannot produce a formal prediction."""


class FxTimingError(EntryError):
    """A rate observed after the moment it is being used to convert."""


@dataclass(frozen=True)
class ObservedPrice:
    """A price, when it was seen, and what it is worth in yen.

    ``fx_rate`` is 1 for a yen-denominated security. For anything else the rate
    must have been observed at or before the price it converts - a later rate is
    information from after the decision, however small the difference.
    """

    amount: Decimal
    currency: str
    observed_at: datetime
    fx_rate: Decimal = Decimal(1)
    fx_observed_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.amount <= 0:
            raise ValueError(f"a price must be positive, got {self.amount}")
        if self.currency == "JPY" and self.fx_rate != 1:
            raise ValueError("a yen price does not get converted")

    @property
    def jpy(self) -> Decimal:
        return self.amount * self.fx_rate

    def check_fx_not_after(self, moment: datetime, *, label: str) -> None:
        if self.currency == "JPY":
            return
        if self.fx_observed_at is None:
            raise FxTimingError(
                f"{label}: a {self.currency} price needs an FX rate with an observation time; "
                "without one there is no way to tell whether the rate predates the decision"
            )
        if self.fx_observed_at > moment:
            raise FxTimingError(
                f"{label}: the FX rate was observed at {self.fx_observed_at.isoformat()}, after "
                f"{moment.isoformat()}. Converting with it would use a rate the decision could "
                "not have had"
            )


@dataclass(frozen=True)
class UniverseVerdict:
    """What the authoritative universe said about this security."""

    decision: str
    reason_code: str | None = None

    @property
    def may_predict(self) -> bool:
        # UNRESOLVED is not a synonym for included (CLAUDE.md 1-10b). It is also
        # not a synonym for excluded: the security stays in the research record
        # and in the price and FX fetch set, and only formal prediction is closed.
        return self.decision == "INCLUDED"


@dataclass(frozen=True)
class AnalysisVerdict:
    """The intraday analysis that is asking for an entry."""

    kind: AnalysisKind
    state: DecisionState
    provider_id: str
    provider_kind: str
    model_id: str | None = None
    prompt_sha256: str | None = None
    bundle_sha256: str | None = None
    canonical_prompt_sha256: str | None = None
    rationale: str | None = None

    @property
    def is_a_stand_in(self) -> bool:
        return self.provider_kind == "DETERMINISTIC_MOCK"


@dataclass(frozen=True)
class OpenEpisode:
    """An episode already running for this security under this thesis."""

    episode_id: str
    security_id: str
    thesis_key: str
    entry_price_observed_at: datetime
    opened_at: datetime


@dataclass(frozen=True)
class EntryRequest:
    """Everything one intraday entry decision needs.

    The entry price is optional and the decision price is not, which is the
    asymmetry of D-31: a decision without a price to judge never happened, while
    a decision whose entry price could not be observed did happen and has to be
    recorded as such.
    """

    security_id: str
    thesis_key: str
    analysis: AnalysisVerdict
    universe: UniverseVerdict
    decision_cutoff_at: datetime
    decision_completed_at: datetime
    decision_price: ObservedPrice | None
    entry_price: ObservedPrice | None = None
    entry_price_method: str | None = None
    initial_failure_line: Decimal | None = None
    setup_ids: tuple[str, ...] = ()
    watch_id: str | None = None
    open_episode: OpenEpisode | None = None
    run_id: str | None = None
    verification: VerificationStatus = VerificationStatus.IMPLEMENTED_NOT_LIVE_VERIFIED


@dataclass(frozen=True)
class Prediction:
    """The claim. Once made, never edited."""

    security_id: str
    thesis_key: str
    analysis_kind: AnalysisKind
    entry_reference_price: Decimal
    entry_price_observed_at: datetime
    entry_price_currency: str
    entry_price_jpy: Decimal
    decision_price: Decimal
    decision_price_observed_at: datetime
    decision_price_jpy: Decimal
    initial_failure_line: Decimal
    target_price: Decimal
    data_cutoff: datetime
    provider_id: str
    provider_kind: str
    universe_decision: str
    rule_version: str = ENTRY_RULE_VERSION
    #: Carried from the attempt. The database requires the two to agree, so a
    #: prediction cannot be attributed to a different run than the decision.
    run_id: str | None = None
    entry_price_method: str | None = None
    model_id: str | None = None
    prompt_sha256: str | None = None
    bundle_sha256: str | None = None
    canonical_prompt_sha256: str | None = None
    source_setup_ids: tuple[str, ...] = ()
    verification: VerificationStatus = VerificationStatus.IMPLEMENTED_NOT_LIVE_VERIFIED
    state: DecisionState = DecisionState.ENTRY


@dataclass(frozen=True)
class EntryAttempt:
    """The ledger row. Written whatever the outcome."""

    security_id: str
    status: EntryAttemptStatus
    analysis_kind: AnalysisKind
    decision_cutoff_at: datetime
    decision_completed_at: datetime
    decision_price: ObservedPrice | None = None
    entry_price: ObservedPrice | None = None
    entry_price_method: str | None = None
    universe_decision: str | None = None
    universe_reason_code: str | None = None
    thesis_key: str | None = None
    reject_reason: str | None = None
    provider_id: str | None = None
    setup_id: str | None = None
    watch_id: str | None = None
    run_id: str | None = None
    verification: VerificationStatus = VerificationStatus.IMPLEMENTED_NOT_LIVE_VERIFIED


@dataclass(frozen=True)
class EntryOutcome:
    """What the decision produced: always an attempt, sometimes a prediction."""

    attempt: EntryAttempt
    prediction: Prediction | None = None
    #: Set when an already-open episode absorbed this decision.
    reaffirmed_episode_id: str | None = None
    notes: tuple[str, ...] = ()

    @property
    def status(self) -> EntryAttemptStatus:
        return self.attempt.status

    @property
    def created_a_prediction(self) -> bool:
        return self.prediction is not None


@dataclass
class Episode:
    """One security, one thesis, from the first entry to the close."""

    episode_id: str
    security_id: str
    thesis_key: str
    entry_price_observed_at: datetime
    opened_at: datetime
    status: EpisodeStatus = EpisodeStatus.OPEN
    closed_at: datetime | None = None
    close_reason: EpisodeCloseReason | None = None
    horizon_sessions: int = PRIMARY_HORIZON_SESSIONS
    #: The same value the attempt and the prediction carry, not a constant.
    #: It used to be hard-coded at the point of writing, which meant a
    #: LIVE_VERIFIED prediction could sit inside an episode nobody had verified
    #: - and the episode is the unit scoring counts, so the whole claim would
    #: have been counted as unverified evidence or as verified, depending on
    #: which row anyone happened to read.
    verification: VerificationStatus = VerificationStatus.IMPLEMENTED_NOT_LIVE_VERIFIED
    transitions: list[tuple[TransitionKind, datetime, str | None]] = field(default_factory=list)

    def record(self, kind: TransitionKind, at: datetime, note: str | None = None) -> None:
        self.transitions.append((kind, at, note))

    def close(self, reason: EpisodeCloseReason, at: datetime) -> None:
        if self.status is EpisodeStatus.CLOSED:
            raise EntryError(f"episode {self.episode_id} is already closed")
        self.status = EpisodeStatus.CLOSED
        self.close_reason = reason
        self.closed_at = at
        self.record(TransitionKind.EPISODE_CLOSED, at, reason.value)


@dataclass(frozen=True)
class SessionCalendarUnknown(Exception):
    """Raised when a session index is asked for beyond what has been observed.

    There is no verified trading calendar yet, so sessions are counted from the
    sessions that actually traded rather than predicted from a holiday table. A
    question about a session that has not happened has no answer, and guessing
    one would put a fabricated date on a horizon boundary.
    """

    requested_index: int
    observed_sessions: int

    def __str__(self) -> str:  # pragma: no cover - message only
        return (
            f"S{self.requested_index} has not been observed yet; only {self.observed_sessions} "
            "sessions are known. No verified trading calendar is available to extrapolate from"
        )


#: The scale prices are stored at (``numeric(18, 6)``). The target is rounded to
#: it here so that the value Python computes is the value the database stores,
#: and the constraint can be an equality rather than a tolerance.
PRICE_SCALE = Decimal("0.000001")


def target_for(entry_reference_price: Decimal) -> Decimal:
    """+20% of the entry price, rounded to the stored scale.

    Of the entry price and nothing else: CLAUDE.md 1-4 gives the threshold one
    basis, and computing it from the signal or decision price would move the
    target by however far the price travelled while the decision was being made.

    ROUND_HALF_UP to match Postgres ``round(numeric, 6)``, which rounds half away
    from zero. The database checks ``target_price = round(entry * 1.20, 6)``
    exactly; a tolerance would be a place for a wrong number to sit undetected,
    and two numerics do not need one.
    """

    return (entry_reference_price * TARGET_MULTIPLE).quantize(
        PRICE_SCALE, rounding=ROUND_HALF_UP
    )


def sessions_from(entry_at: datetime, sessions: list[date]) -> dict[int, date]:
    """Map S0, S1, ... onto the trading sessions that were actually observed.

    S0 is the session in which the entry price was observed; S1 is the next
    session that traded. Counting from observed sessions rather than from a
    calendar means a holiday cannot be mistaken for a session, and it means the
    map simply ends where observation ends instead of inventing future dates.
    """

    entry_date = entry_at.date()
    ordered = sorted(set(sessions))
    on_or_after = [d for d in ordered if d >= entry_date]
    return {index: day for index, day in enumerate(on_or_after)}
