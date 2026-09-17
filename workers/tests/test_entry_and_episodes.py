"""Phase 8: the rules that only matter when they are inconvenient.

Every test here is about a case where the permissive answer is easier and
produces a better-looking record. That is the whole point: none of these rules
costs anything when the data is clean.

Covers RF-06 (one episode, not several successes), RF-07 (three prices kept
apart), RF-08 (no prediction from an end-of-day pass), RF-09 (FX time), RF-11
(the 3,000 yen boundary), RF-19 (the limit checked twice), RF-20 (the horizon
runs from entry and does not reset) and RF-21 (two failure lines).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from surge.entry.decision import decide
from surge.entry.episode import EpisodeBook, Horizon
from surge.entry.models import (
    PRICE_LIMIT_JPY,
    AnalysisKind,
    AnalysisVerdict,
    DecisionState,
    EntryAttemptStatus,
    EntryError,
    EntryRequest,
    EpisodeCloseReason,
    FxTimingError,
    MissingPriceError,
    ObservedPrice,
    OpenEpisode,
    SessionCalendarUnknown,
    StandInProviderError,
    UniverseVerdict,
    WatchState,
)
from surge.entry.watch import IllegalTransition, Watch, reached_entry_legally

CUTOFF = datetime(2026, 9, 17, 2, 0, tzinfo=UTC)
COMPLETED = datetime(2026, 9, 17, 2, 5, tzinfo=UTC)
ENTRY_AT = datetime(2026, 9, 17, 2, 6, tzinfo=UTC)

REAL_PROVIDER = AnalysisVerdict(
    kind=AnalysisKind.ENTRY_DECISION,
    state=DecisionState.ENTRY,
    provider_id="some_hosted_model",
    provider_kind="HOSTED_LLM",
    model_id="m-1",
)


def _price(amount, *, at, currency="JPY", fx=Decimal(1), fx_at=None) -> ObservedPrice:
    return ObservedPrice(
        amount=Decimal(amount),
        currency=currency,
        observed_at=at,
        fx_rate=fx,
        fx_observed_at=fx_at,
    )


def _request(**overrides) -> EntryRequest:
    base = {
        "security_id": "sec-1",
        "thesis_key": "breakout-on-order-win",
        "analysis": REAL_PROVIDER,
        "universe": UniverseVerdict("INCLUDED", "TARGET_MARKET_COMMON_STOCK"),
        "decision_cutoff_at": CUTOFF,
        "decision_completed_at": COMPLETED,
        "decision_price": _price("1000", at=CUTOFF),
        "entry_price": _price("1010", at=ENTRY_AT),
        "entry_price_method": "first trade after the decision completed",
        "initial_failure_line": Decimal("940"),
    }
    base.update(overrides)
    return EntryRequest(**base)


# --------------------------------------------------------------- the happy path


def test_a_clean_decision_produces_one_prediction_and_one_ledger_row():
    outcome = decide(_request())

    assert outcome.status is EntryAttemptStatus.PREDICTION_CREATED
    assert outcome.created_a_prediction
    assert outcome.prediction.entry_reference_price == Decimal("1010")


def test_the_target_is_twenty_percent_of_the_entry_price_not_the_decision_price():
    """RF-07. The price that prompted the decision is not the price anyone paid.

    Computing the target from decision_price would move the threshold by however
    far the stock travelled while the analysis ran - in the favourable direction,
    every time the analysis was right about the direction.
    """

    outcome = decide(_request())

    assert outcome.prediction.target_price == Decimal("1010") * Decimal("1.20")
    assert outcome.prediction.target_price != Decimal("1000") * Decimal("1.20")


def test_all_three_prices_survive_into_the_record():
    """RF-07. Two of them are audit-only, and they still have to be there."""

    outcome = decide(_request())
    prediction = outcome.prediction

    assert prediction.decision_price == Decimal("1000")
    assert prediction.entry_reference_price == Decimal("1010")
    assert prediction.decision_price_observed_at == CUTOFF
    assert prediction.entry_price_observed_at == ENTRY_AT


# ----------------------------------------------------- the three prohibitions


def test_a_deterministic_stand_in_cannot_produce_a_prediction():
    """The mock exists so the pipeline runs without a paid model. Its verdicts
    are evidence about the plumbing and none at all about a security."""

    mock = AnalysisVerdict(
        kind=AnalysisKind.ENTRY_DECISION,
        state=DecisionState.ENTRY,
        provider_id="deterministic_mock",
        provider_kind="DETERMINISTIC_MOCK",
    )
    with pytest.raises(StandInProviderError, match="stand-in"):
        decide(_request(analysis=mock))


def test_a_missing_entry_price_is_recorded_and_never_filled_in():
    """D-31. The decision happened; the tradeable price did not arrive.

    The tempting move is to fall back on the decision price, which is both
    available and wrong: it is the price that prompted the decision, not one that
    could have been acted on afterwards.
    """

    outcome = decide(_request(entry_price=None))

    assert outcome.status is EntryAttemptStatus.NO_ENTRY_REFERENCE_PRICE
    assert outcome.prediction is None
    assert outcome.attempt.entry_price is None


def test_a_missing_decision_price_is_not_a_decision_at_all():
    with pytest.raises(MissingPriceError):
        decide(_request(decision_price=None))


@pytest.mark.parametrize("decision", ["UNRESOLVED", "EXCLUDED"])
def test_only_an_included_security_becomes_a_prediction(decision):
    outcome = decide(_request(universe=UniverseVerdict(decision, "ETF")))

    assert outcome.status is EntryAttemptStatus.REJECTED_NOT_IN_UNIVERSE
    assert outcome.prediction is None


def test_unresolved_is_rejected_for_prediction_without_being_called_excluded():
    """CLAUDE.md 1-10b. The distinction survives into the wording."""

    outcome = decide(_request(universe=UniverseVerdict("UNRESOLVED", None)))

    assert outcome.status is EntryAttemptStatus.REJECTED_NOT_IN_UNIVERSE
    assert any("not excluded" in note for note in outcome.notes)


# ------------------------------------------------- RF-08: no entry from an EOD


@pytest.mark.parametrize("kind", [AnalysisKind.EOD, AnalysisKind.POST_CLOSE_MATERIAL])
def test_an_end_of_day_pass_cannot_enter(kind):
    analysis = AnalysisVerdict(
        kind=kind,
        state=DecisionState.ENTRY,
        provider_id="some_hosted_model",
        provider_kind="HOSTED_LLM",
    )
    with pytest.raises(EntryError, match="closed market"):
        decide(_request(analysis=analysis))


def test_an_analysis_that_did_not_say_entry_is_recorded_as_such():
    analysis = AnalysisVerdict(
        kind=AnalysisKind.ENTRY_DECISION,
        state=DecisionState.WATCH_BREAKOUT,
        provider_id="some_hosted_model",
        provider_kind="HOSTED_LLM",
    )
    outcome = decide(_request(analysis=analysis))

    assert outcome.status is EntryAttemptStatus.REJECTED_BY_ANALYSIS
    assert outcome.prediction is None


# ------------------------------------------- RF-11 / RF-19: the limit, twice


def test_the_limit_is_checked_against_the_decision_price():
    outcome = decide(_request(decision_price=_price("3001", at=CUTOFF)))

    assert outcome.status is EntryAttemptStatus.REJECTED_HARD_FILTER_AT_DECISION
    assert outcome.prediction is None


def test_exactly_three_thousand_is_inside_the_limit():
    """RF-11. The rule says 3,000 yen or less."""

    outcome = decide(
        _request(
            decision_price=_price("3000", at=CUTOFF),
            entry_price=_price("3000", at=ENTRY_AT),
            initial_failure_line=Decimal("2800"),
        )
    )

    assert outcome.status is EntryAttemptStatus.PREDICTION_CREATED
    assert outcome.prediction.entry_price_jpy == PRICE_LIMIT_JPY


def test_passing_at_the_decision_and_failing_at_entry_aborts_without_an_episode():
    """RF-19. The case the second check exists for.

    Everything about this decision was correct up to the last moment; the price
    then moved above the limit before it could be taken. Recording it as an entry
    would be recording a trade the rule forbids.
    """

    outcome = decide(
        _request(
            decision_price=_price("2990", at=CUTOFF),
            entry_price=_price("3005", at=ENTRY_AT),
            initial_failure_line=Decimal("2800"),
        )
    )

    assert outcome.status is EntryAttemptStatus.ENTRY_ABORTED_PRICE_LIMIT
    assert outcome.prediction is None
    assert outcome.attempt.entry_price.jpy == Decimal("3005")
    assert any("new decision" in note for note in outcome.notes)


def test_a_us_security_is_measured_against_the_limit_in_yen():
    """CLAUDE.md 1-9. Eligibility is a yen question even though the outcome is not."""

    fx_at = CUTOFF - timedelta(minutes=30)
    outcome = decide(
        _request(
            decision_price=_price("19", at=CUTOFF, currency="USD", fx=Decimal("150"), fx_at=fx_at),
            entry_price=_price(
                "19.5",
                at=ENTRY_AT,
                currency="USD",
                fx=Decimal("150"),
                fx_at=ENTRY_AT - timedelta(minutes=1),
            ),
            initial_failure_line=Decimal("18"),
        )
    )

    assert outcome.status is EntryAttemptStatus.PREDICTION_CREATED
    # The prediction is carried in USD; only the eligibility column is yen.
    assert outcome.prediction.entry_reference_price == Decimal("19.5")
    assert outcome.prediction.entry_price_currency == "USD"
    assert outcome.prediction.entry_price_jpy == Decimal("19.5") * Decimal("150")


def test_a_us_security_over_the_limit_in_yen_is_rejected_though_cheap_in_dollars():
    fx_at = CUTOFF - timedelta(minutes=30)
    outcome = decide(
        _request(
            decision_price=_price("21", at=CUTOFF, currency="USD", fx=Decimal("150"), fx_at=fx_at),
        )
    )

    assert outcome.status is EntryAttemptStatus.REJECTED_HARD_FILTER_AT_DECISION


# ------------------------------------------------------- RF-09: FX timing


def test_an_fx_rate_from_after_the_cutoff_is_refused():
    outcome_fx = CUTOFF + timedelta(minutes=1)
    with pytest.raises(FxTimingError, match="after"):
        decide(
            _request(
                decision_price=_price(
                    "19", at=CUTOFF, currency="USD", fx=Decimal("150"), fx_at=outcome_fx
                )
            )
        )


def test_a_foreign_price_without_an_fx_time_is_refused():
    with pytest.raises(FxTimingError, match="observation time"):
        decide(
            _request(
                decision_price=_price("19", at=CUTOFF, currency="USD", fx=Decimal("150")),
            )
        )


def test_an_entry_price_observed_before_the_decision_finished_is_refused():
    """A price the analysis could have seen is not one it could have acted on."""

    with pytest.raises(EntryError, match="before the decision finished"):
        decide(_request(entry_price=_price("1010", at=COMPLETED - timedelta(seconds=1))))


# ----------------------------------------------- RF-06: one episode, one claim


def test_a_second_entry_on_the_same_thesis_is_a_reaffirmation():
    open_episode = OpenEpisode(
        episode_id="ep-1",
        security_id="sec-1",
        thesis_key="breakout-on-order-win",
        entry_price_observed_at=ENTRY_AT - timedelta(days=3),
        opened_at=ENTRY_AT - timedelta(days=3),
    )
    outcome = decide(_request(open_episode=open_episode))

    assert outcome.status is EntryAttemptStatus.REAFFIRMED_EXISTING_EPISODE
    assert outcome.prediction is None
    assert outcome.reaffirmed_episode_id == "ep-1"


def test_an_open_episode_under_a_different_thesis_is_an_error_not_a_reaffirmation():
    open_episode = OpenEpisode(
        episode_id="ep-1",
        security_id="sec-1",
        thesis_key="something-else",
        entry_price_observed_at=ENTRY_AT - timedelta(days=3),
        opened_at=ENTRY_AT - timedelta(days=3),
    )
    with pytest.raises(EntryError, match="different thesis"):
        decide(_request(open_episode=open_episode))


def test_the_book_refuses_to_open_a_second_episode_on_the_same_thesis():
    book = EpisodeBook()
    prediction = decide(_request()).prediction
    book.open_episode(prediction, episode_id="ep-1", at=ENTRY_AT)

    with pytest.raises(EntryError, match="already open"):
        book.open_episode(prediction, episode_id="ep-2", at=ENTRY_AT + timedelta(days=1))


def test_a_reaffirmation_does_not_move_the_horizon():
    """RF-20. The horizon runs from the original entry, whatever happens later."""

    book = EpisodeBook()
    prediction = decide(_request()).prediction
    episode = book.open_episode(prediction, episode_id="ep-1", at=ENTRY_AT)
    started_at = episode.entry_price_observed_at

    book.reaffirm(episode, at=ENTRY_AT + timedelta(days=4))

    assert episode.entry_price_observed_at == started_at
    assert episode.horizon_sessions == 20


# -------------------------------------------------- RF-21: two failure lines


def test_the_risk_line_starts_equal_to_the_failure_line_and_then_diverges():
    book = EpisodeBook()
    prediction = decide(_request()).prediction
    episode = book.open_episode(prediction, episode_id="ep-1", at=ENTRY_AT)

    assert book.current_risk_line(episode) == prediction.initial_failure_line

    book.move_risk_line(
        episode, to=Decimal("980"), reason="trailing after a move up", at=ENTRY_AT + timedelta(days=2)
    )

    assert book.current_risk_line(episode) == Decimal("980")
    assert prediction.initial_failure_line == Decimal("940")


def test_touching_the_risk_line_does_not_close_the_episode():
    """The movable line is for risk management research. Letting it end an
    episode would make the fixed line decorative."""

    book = EpisodeBook()
    prediction = decide(_request()).prediction
    episode = book.open_episode(prediction, episode_id="ep-1", at=ENTRY_AT)
    book.move_risk_line(episode, to=Decimal("980"), reason="trailing", at=ENTRY_AT)

    book.risk_line_hit(episode, at=ENTRY_AT + timedelta(days=1), price=Decimal("979"))

    assert episode.status.value == "OPEN"
    assert episode.close_reason is None


def test_a_prediction_is_frozen_and_its_failure_line_cannot_be_reassigned():
    prediction = decide(_request()).prediction
    with pytest.raises(Exception):  # noqa: B017 - frozen dataclass raises FrozenInstanceError
        prediction.initial_failure_line = Decimal("900")


def test_a_failure_line_at_or_above_the_entry_price_is_refused():
    with pytest.raises(EntryError, match="not below the entry price"):
        decide(_request(initial_failure_line=Decimal("1010")))


# --------------------------------------------------------- RF-20: the horizon


def _sessions(start: date, count: int) -> list[date]:
    """Weekday sessions. Synthetic, and not a claim about any real calendar."""

    out: list[date] = []
    day = start
    while len(out) < count:
        if day.weekday() < 5:
            out.append(day)
        day += timedelta(days=1)
    return out


def test_s0_is_the_entry_session_and_s20_is_the_twentieth_after_it():
    sessions = _sessions(date(2026, 9, 17), 30)
    horizon = Horizon(ENTRY_AT, sessions)

    assert horizon.session(0) == date(2026, 9, 17)
    assert horizon.session(1) == date(2026, 9, 18)
    assert horizon.last_session == sessions[20]


def test_sessions_before_the_entry_are_not_counted():
    sessions = _sessions(date(2026, 9, 1), 40)
    horizon = Horizon(ENTRY_AT, sessions)

    assert horizon.session(0) == date(2026, 9, 17)


def test_a_session_that_has_not_happened_raises_instead_of_being_guessed():
    """There is no verified trading calendar (D-135), so a future session index
    has no answer. Extrapolating one would put a fabricated date on a horizon
    boundary that decides whether a prediction expired."""

    horizon = Horizon(ENTRY_AT, _sessions(date(2026, 9, 17), 5))

    assert horizon.last_session is None
    assert not horizon.is_complete
    with pytest.raises(SessionCalendarUnknown):
        horizon.session(20)


def test_the_horizon_expires_only_after_s20():
    sessions = _sessions(date(2026, 9, 17), 30)
    horizon = Horizon(ENTRY_AT, sessions)

    assert not horizon.has_expired_by(sessions[20])
    assert horizon.has_expired_by(sessions[21])


# ------------------------------------------------------------- the watch machine


def _watch() -> Watch:
    return Watch(
        watch_id="w-1",
        security_id="sec-1",
        setup_id="setup-1",
        trigger_description="breaks the prior session high on rising volume",
        opened_at=datetime(2026, 9, 16, 6, 0, tzinfo=UTC),
    )


def test_a_trigger_does_not_enter_by_itself():
    """CLAUDE.md 1-5, and the single most tempting shortcut in the system."""

    watch = _watch()
    watch.trigger(at=CUTOFF, price=Decimal("1005"))

    with pytest.raises(IllegalTransition, match="not a decision to enter"):
        watch.enter(at=COMPLETED)


def test_the_legal_path_to_entry_runs_through_a_reanalysis():
    watch = _watch()
    watch.trigger(at=CUTOFF, price=Decimal("1005"))
    watch.begin_reanalysis(at=CUTOFF + timedelta(minutes=1))
    watch.enter(at=COMPLETED)

    assert watch.state is WatchState.ENTERED
    assert reached_entry_legally(watch.history)


def test_a_reanalysis_may_say_no_and_the_watch_can_be_rearmed():
    watch = _watch()
    watch.trigger(at=CUTOFF, price=Decimal("1005"))
    watch.begin_reanalysis(at=CUTOFF + timedelta(minutes=1))
    watch.rearm(at=COMPLETED, note="volume did not confirm")
    watch.trigger(at=COMPLETED + timedelta(hours=1), price=Decimal("1020"))

    assert watch.state is WatchState.TRIGGER_HIT
    assert len(watch.history) == 5


def test_a_terminal_watch_does_not_move_again():
    watch = _watch()
    watch.trigger(at=CUTOFF)
    watch.begin_reanalysis(at=CUTOFF + timedelta(minutes=1))
    watch.reject(at=COMPLETED)

    with pytest.raises(IllegalTransition, match="terminal"):
        watch.trigger(at=COMPLETED + timedelta(hours=1))


def test_entering_requires_the_transition_to_carry_a_reanalysis():
    watch = _watch()
    watch.trigger(at=CUTOFF)
    watch.begin_reanalysis(at=CUTOFF + timedelta(minutes=1))

    with pytest.raises(IllegalTransition, match="requires a REANALYSIS"):
        watch.move(WatchState.ENTERED, at=COMPLETED, analysis_kind=AnalysisKind.WATCH_MONITOR)


def test_stored_history_that_skipped_the_reanalysis_is_detectable():
    """For auditing rows written by some other route than the machine."""

    from surge.entry.watch import Transition

    forged = [
        Transition(None, WatchState.ARMED, CUTOFF),
        Transition(WatchState.ARMED, WatchState.TRIGGER_HIT, CUTOFF),
        Transition(
            WatchState.TRIGGER_HIT,
            WatchState.ENTERED,
            COMPLETED,
            analysis_kind=AnalysisKind.REANALYSIS,
        ),
    ]

    assert not reached_entry_legally(forged)


# ------------------------------------------------------------------ episodes


def test_an_episode_closes_once_and_records_why():
    book = EpisodeBook()
    prediction = decide(_request()).prediction
    episode = book.open_episode(prediction, episode_id="ep-1", at=ENTRY_AT)

    book.close(episode, reason=EpisodeCloseReason.TARGET_HIT, at=ENTRY_AT + timedelta(days=5))

    assert episode.close_reason is EpisodeCloseReason.TARGET_HIT
    with pytest.raises(EntryError, match="already closed"):
        book.close(episode, reason=EpisodeCloseReason.HORIZON_EXPIRED, at=ENTRY_AT)


def test_a_closed_episode_cannot_be_reaffirmed():
    book = EpisodeBook()
    prediction = decide(_request()).prediction
    episode = book.open_episode(prediction, episode_id="ep-1", at=ENTRY_AT)
    book.close(episode, reason=EpisodeCloseReason.THESIS_INVALIDATED, at=ENTRY_AT)

    with pytest.raises(EntryError, match="closed"):
        book.reaffirm(episode, at=ENTRY_AT + timedelta(days=1))


def test_everything_produced_here_is_marked_not_live_verified():
    """No live intraday price provider is settled. The record has to say so."""

    outcome = decide(_request())

    assert outcome.prediction.verification.value == "IMPLEMENTED_NOT_LIVE_VERIFIED"
    assert outcome.attempt.verification.value == "IMPLEMENTED_NOT_LIVE_VERIFIED"
