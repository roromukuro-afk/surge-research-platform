"""Phase 9: which line was reached first, and when we cannot tell.

RF-10 (the resolution ladder), RF-18 (corporate actions), RF-22 (US outcomes in
USD) and RF-24 (the two layers after a thesis is invalidated).

The tests that matter most here are the ones where the answer is "unresolved".
Every other outcome has an obvious right answer; a session that touched both
lines has two plausible ones, and the whole design is about not choosing the
flattering one.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from surge.outcome.engine import evaluate
from surge.outcome.models import (
    Granularity,
    IntradayBar,
    OutcomeError,
    PathResolution,
    PrimaryVerdict,
    Session,
    SplitAction,
    Trade,
)

ENTRY = Decimal("1000")
TARGET = Decimal("1200")
FAILURE = Decimal("940")
ENTRY_AT = datetime(2026, 9, 17, 2, 0, tzinfo=UTC)
START = date(2026, 9, 17)


def _day(index: int) -> date:
    """Synthetic weekday sessions. Not a claim about any real calendar."""

    day = START
    seen = 0
    while True:
        if day.weekday() < 5:
            if seen == index:
                return day
            seen += 1
        day += timedelta(days=1)


def _session(index, *, o=1000, h=1010, low=990, c=1000, currency="JPY", **kw) -> Session:
    return Session(
        index=index,
        trade_date=_day(index),
        open=Decimal(str(o)),
        high=Decimal(str(h)),
        low=Decimal(str(low)),
        close=Decimal(str(c)),
        currency=currency,
        **kw,
    )


def _entry_session(**kw) -> Session:
    """S0. Its daily bar spans the pre-entry move, so the engine may not use it.

    Given intraday data after the entry moment by default, because without any
    the correct answer is UNRESOLVED_MISSING_DATA and every test would start there.
    """

    defaults = {
        "intraday": (
            IntradayBar(
                starts_at=ENTRY_AT + timedelta(minutes=1),
                ends_at=ENTRY_AT + timedelta(minutes=2),
                open=Decimal("1000"),
                high=Decimal("1005"),
                low=Decimal("998"),
                close=Decimal("1002"),
            ),
        )
    }
    defaults.update(kw)
    return _session(0, **defaults)


def _run(sessions, **kw):
    return evaluate(
        episode_id="ep-1",
        entry_reference_price=ENTRY,
        target_price=TARGET,
        initial_failure_line=FAILURE,
        entry_price_observed_at=ENTRY_AT,
        currency=kw.pop("currency", "JPY"),
        sessions=sessions,
        **kw,
    )


# ------------------------------------------------------------- the simple cases


def test_a_day_that_reaches_only_the_target_is_a_target_hit():
    report = _run([_entry_session(), _session(1, h=1250)])

    assert report.primary.verdict is PrimaryVerdict.TARGET_HIT
    assert report.primary.granularity is Granularity.DAY
    assert report.primary.resolved_session_index == 1


def test_a_day_that_reaches_only_the_failure_line_is_a_failure():
    report = _run([_entry_session(), _session(1, low=900)])

    assert report.primary.verdict is PrimaryVerdict.INITIAL_FAILURE_HIT
    assert report.primary.granularity is Granularity.DAY


def test_an_open_already_above_the_target_resolves_at_the_open():
    report = _run([_entry_session(), _session(1, o=1300, h=1350, low=1290, c=1310)])

    assert report.primary.verdict is PrimaryVerdict.TARGET_HIT
    assert report.primary.granularity is Granularity.SESSION_OPEN


def test_an_open_already_below_the_failure_line_resolves_at_the_open():
    """A gap down through the line is a failure at the open, not a day-level
    guess about what happened inside the session."""

    report = _run([_entry_session(), _session(1, o=900, h=950, low=880, c=910)])

    assert report.primary.verdict is PrimaryVerdict.INITIAL_FAILURE_HIT
    assert report.primary.granularity is Granularity.SESSION_OPEN


def test_the_first_session_to_resolve_wins():
    report = _run([_entry_session(), _session(1), _session(2, low=900), _session(3, h=1300)])

    assert report.primary.verdict is PrimaryVerdict.INITIAL_FAILURE_HIT
    assert report.primary.resolved_session_index == 2


# ------------------------------------------------- both lines in one session


def _both_in_one_day(**kw):
    return _session(1, o=1000, h=1250, low=900, c=1100, **kw)


def test_intraday_bars_order_a_day_that_touched_both():
    bars = (
        IntradayBar(
            starts_at=datetime(2026, 9, 18, 1, 0, tzinfo=UTC),
            ends_at=datetime(2026, 9, 18, 1, 1, tzinfo=UTC),
            open=Decimal("1000"),
            high=Decimal("1010"),
            low=Decimal("890"),
            close=Decimal("900"),
        ),
        IntradayBar(
            starts_at=datetime(2026, 9, 18, 2, 0, tzinfo=UTC),
            ends_at=datetime(2026, 9, 18, 2, 1, tzinfo=UTC),
            open=Decimal("1100"),
            high=Decimal("1250"),
            low=Decimal("1090"),
            close=Decimal("1240"),
        ),
    )
    report = _run([_entry_session(), _both_in_one_day(intraday=bars)])

    assert report.primary.verdict is PrimaryVerdict.INITIAL_FAILURE_HIT
    assert report.primary.granularity is Granularity.INTRADAY_BAR


def _one_bar_both(**kw):
    bar = IntradayBar(
        starts_at=datetime(2026, 9, 18, 1, 0, tzinfo=UTC),
        ends_at=datetime(2026, 9, 18, 1, 1, tzinfo=UTC),
        open=Decimal("1000"),
        high=Decimal("1250"),
        low=Decimal("900"),
        close=Decimal("1100"),
    )
    return _both_in_one_day(intraday=(bar,), **kw)


def test_trades_order_a_bar_that_touched_both():
    trades = (
        Trade(at=datetime(2026, 9, 18, 1, 0, 10, tzinfo=UTC), price=Decimal("1250")),
        Trade(at=datetime(2026, 9, 18, 1, 0, 20, tzinfo=UTC), price=Decimal("900")),
    )
    report = _run([_entry_session(), _one_bar_both(trades=trades)])

    assert report.primary.verdict is PrimaryVerdict.TARGET_HIT
    assert report.primary.granularity is Granularity.TRADE


def test_trades_at_the_same_instant_reaching_both_are_ambiguous():
    """The provider's own record cannot order them, so neither can we."""

    same = datetime(2026, 9, 18, 1, 0, 10, tzinfo=UTC)
    trades = (
        Trade(at=same, price=Decimal("1250")),
        Trade(at=same, price=Decimal("900")),
    )
    report = _run([_entry_session(), _one_bar_both(trades=trades)])

    assert report.primary.verdict is PrimaryVerdict.AMBIGUOUS_PATH
    assert report.primary.granularity is Granularity.TRADE


def test_a_market_without_trade_data_gives_ambiguous_not_missing():
    """D-09b. Finer data that does not exist is a different finding from finer
    data that failed to arrive, and only the second one is worth chasing."""

    report = _run([_entry_session(), _one_bar_both(trades_exist_for_this_market=False)])

    assert report.primary.verdict is PrimaryVerdict.AMBIGUOUS_PATH
    assert report.primary.granularity is Granularity.INTRADAY_BAR
    assert "unknowable rather than unknown" in report.primary.detail


def test_trade_data_that_should_exist_but_is_missing_is_a_gap_not_ambiguity():
    report = _run([_entry_session(), _one_bar_both()])

    assert report.primary.verdict is PrimaryVerdict.UNRESOLVED_MISSING_DATA
    assert "Fixable" in report.primary.detail


def test_missing_intraday_on_a_market_that_publishes_it_is_a_gap():
    report = _run([_entry_session(), _both_in_one_day()])

    assert report.primary.verdict is PrimaryVerdict.UNRESOLVED_MISSING_DATA
    assert "collection gap" in report.primary.detail


@pytest.mark.parametrize(
    "verdict",
    [PrimaryVerdict.AMBIGUOUS_PATH, PrimaryVerdict.UNRESOLVED_MISSING_DATA],
)
def test_an_unresolved_path_is_neither_a_success_nor_a_failure(verdict):
    report = (
        _run([_entry_session(), _one_bar_both(trades_exist_for_this_market=False)])
        if verdict is PrimaryVerdict.AMBIGUOUS_PATH
        else _run([_entry_session(), _one_bar_both()])
    )

    assert report.primary.verdict is verdict
    assert not report.primary.is_a_success
    assert report.primary.is_unresolved


# ------------------------------------------------------------ the horizon


def test_neither_line_by_s20_expires_the_horizon():
    sessions = [_entry_session()] + [_session(i) for i in range(1, 21)]
    report = _run(sessions)

    assert report.primary.verdict is PrimaryVerdict.HORIZON_EXPIRED
    assert report.primary.path_resolution is PathResolution.NEITHER_BY_HORIZON
    assert report.counterfactual.sessions_observed == 21


def test_s21_is_not_waited_for():
    """The primary horizon ends at S20's close. A target reached on S21 is
    outside it, and reading one more session would quietly extend the window."""

    sessions = [_entry_session()] + [_session(i) for i in range(1, 21)] + [_session(21, h=1300)]
    report = _run(sessions)

    assert report.primary.verdict is PrimaryVerdict.HORIZON_EXPIRED
    assert report.counterfactual.sessions_observed == 21
    assert not report.counterfactual.later_target_hit


def test_a_short_series_says_it_is_provisional_rather_than_expired():
    report = _run([_entry_session(), _session(1), _session(2)])

    assert report.primary.verdict is PrimaryVerdict.HORIZON_EXPIRED
    assert any("provisional read" in note for note in report.notes)


# --------------------------------------------------- S0, and only after entry


def test_the_entry_session_ignores_what_happened_before_the_entry():
    """The daily bar for S0 contains the move that produced the setup. Reading a
    high from it would credit the prediction with a price that had already gone."""

    before = IntradayBar(
        starts_at=ENTRY_AT - timedelta(minutes=30),
        ends_at=ENTRY_AT - timedelta(minutes=29),
        open=Decimal("1240"),
        high=Decimal("1300"),
        low=Decimal("1230"),
        close=Decimal("1250"),
    )
    after = IntradayBar(
        starts_at=ENTRY_AT + timedelta(minutes=1),
        ends_at=ENTRY_AT + timedelta(minutes=2),
        open=Decimal("1000"),
        high=Decimal("1005"),
        low=Decimal("995"),
        close=Decimal("1000"),
    )
    entry = _session(0, o=1240, h=1300, low=990, c=1000, intraday=(before, after))
    report = _run([entry, _session(1)])

    assert report.primary.verdict is not PrimaryVerdict.TARGET_HIT
    assert not report.counterfactual.later_target_hit


def test_the_entry_session_resolves_from_post_entry_trades():
    entry = _session(
        0,
        o=990,
        h=1300,
        low=980,
        c=1250,
        trades=(Trade(at=ENTRY_AT + timedelta(minutes=5), price=Decimal("1210")),),
    )
    report = _run([entry])

    assert report.primary.verdict is PrimaryVerdict.TARGET_HIT
    assert report.primary.granularity is Granularity.TRADE
    assert report.primary.resolved_session_index == 0


def test_an_entry_session_with_no_intraday_data_is_a_gap():
    report = _run([_session(0)])

    assert report.primary.verdict is PrimaryVerdict.UNRESOLVED_MISSING_DATA
    assert "not a substitute" in report.primary.detail


# --------------------------------------------- RF-24: the two layers diverge


def test_a_target_reached_after_the_thesis_was_invalidated_is_not_a_success():
    """The rule this whole second layer exists for."""

    sessions = [_entry_session(), _session(1), _session(2), _session(3, h=1300)]
    report = _run(sessions, thesis_invalidated_session=1)

    assert report.primary.verdict is PrimaryVerdict.THESIS_INVALIDATED
    assert not report.primary.is_a_success
    assert report.counterfactual.later_target_hit
    assert report.counterfactual.later_target_hit_session_index == 3
    assert any("does not make the primary outcome a success" in n for n in report.notes)


def test_an_invalidation_after_the_target_does_not_undo_the_target():
    sessions = [_entry_session(), _session(1, h=1300), _session(2)]
    report = _run(sessions, thesis_invalidated_session=2)

    assert report.primary.verdict is PrimaryVerdict.TARGET_HIT


def test_the_counterfactual_records_a_touch_even_when_the_order_is_unresolvable():
    report = _run([_entry_session(), _one_bar_both(trades_exist_for_this_market=False)])

    assert report.primary.verdict is PrimaryVerdict.AMBIGUOUS_PATH
    assert report.counterfactual.later_target_hit


def test_mfe_and_mae_are_fractions_of_the_entry_price():
    sessions = [_entry_session(), _session(1, h=1100, low=950)]
    report = _run(sessions)

    assert report.counterfactual.mfe == Decimal("100") / ENTRY
    assert report.counterfactual.mae == Decimal("-50") / ENTRY


# ------------------------------------------------ RF-18: corporate actions


def test_a_split_alone_is_not_a_failure():
    """Two-for-one halves the price overnight. Left raw, the failure line at 940
    is 'hit' by the split itself - which is a corporate action, not a loss."""

    split = SplitAction(ex_date=_day(2), ratio=Decimal(2), action_id="ca-1")
    sessions = [
        _entry_session(),
        _session(1),
        _session(2, o=500, h=510, low=495, c=505),
        _session(3, o=505, h=515, low=500, c=510),
    ]
    report = _run(sessions, actions=[split])

    assert report.primary.verdict is not PrimaryVerdict.INITIAL_FAILURE_HIT
    assert report.corporate_action_ids_applied == ("ca-1",)


def test_the_comparable_path_still_reaches_the_target_after_a_split():
    split = SplitAction(ex_date=_day(2), ratio=Decimal(2), action_id="ca-1")
    sessions = [
        _entry_session(),
        _session(1),
        _session(2, o=500, h=510, low=495, c=505),
        # 610 * 2 = 1220, above the 1200 target in entry-time shares.
        _session(3, o=600, h=610, low=595, c=605),
    ]
    report = _run(sessions, actions=[split])

    assert report.primary.verdict is PrimaryVerdict.TARGET_HIT
    assert report.primary.resolved_session_index == 3


def test_a_reverse_split_is_restated_the_same_way():
    reverse = SplitAction(ex_date=_day(2), ratio=Decimal("0.1"), action_id="ca-2")
    sessions = [
        _entry_session(),
        _session(1),
        # 10000 * 0.1 = 1000; nothing has happened economically.
        _session(2, o=10000, h=10100, low=9900, c=10000),
    ]
    report = _run(sessions, actions=[reverse])

    assert report.primary.verdict is PrimaryVerdict.HORIZON_EXPIRED


def test_a_split_shaped_gap_with_no_recorded_action_is_not_finalised():
    """The outcome is left unresolved and looked at by a person. A split scored
    as a failure is a wrong number nobody would go back and question."""

    sessions = [
        _entry_session(),
        _session(1, c=1000),
        _session(2, o=500, h=510, low=495, c=505),
    ]
    report = _run(sessions)

    assert report.primary.verdict is PrimaryVerdict.CORPORATE_ACTION_SUSPECTED
    assert "no corporate action is recorded" in report.primary.detail


def test_a_dividend_sized_gap_is_not_mistaken_for_a_split():
    """Suspicion is for share-count-shaped discontinuities. A 2% drop is a
    dividend or a bad morning, and neither is this engine's business."""

    sessions = [_entry_session(), _session(1, c=1000), _session(2, o=980, h=990, low=975, c=985)]
    report = _run(sessions)

    assert report.primary.verdict is not PrimaryVerdict.CORPORATE_ACTION_SUSPECTED


def test_a_zero_ratio_action_is_refused_rather_than_worked_around():
    bad = SplitAction(ex_date=_day(1), ratio=Decimal(0), action_id="ca-bad")
    with pytest.raises(OutcomeError, match="must be positive"):
        _run([_entry_session(), _session(1)], actions=[bad])


# ------------------------------------------------ RF-22: US outcomes in USD


def test_a_us_outcome_is_computed_in_dollars():
    sessions = [
        _entry_session(currency="USD"),
        _session(1, h=1250, currency="USD"),
    ]
    report = evaluate(
        episode_id="ep-us",
        entry_reference_price=ENTRY,
        target_price=TARGET,
        initial_failure_line=FAILURE,
        entry_price_observed_at=ENTRY_AT,
        currency="USD",
        sessions=sessions,
    )

    assert report.primary.verdict is PrimaryVerdict.TARGET_HIT
    assert report.currency == "USD"


def test_a_mixed_currency_series_is_refused():
    """There is no FX input to this module, which is what stops a +17% move in
    dollars becoming a +20% success because the yen moved."""

    sessions = [_entry_session(currency="USD"), _session(1, currency="JPY")]
    with pytest.raises(OutcomeError, match="no FX input"):
        evaluate(
            episode_id="ep-us",
            entry_reference_price=ENTRY,
            target_price=TARGET,
            initial_failure_line=FAILURE,
            entry_price_observed_at=ENTRY_AT,
            currency="USD",
            sessions=sessions,
        )


# ------------------------------------------------------------- malformed input


def test_no_sessions_is_an_error_not_an_empty_outcome():
    with pytest.raises(OutcomeError, match="no sessions"):
        _run([])


def test_a_failure_line_at_or_above_the_target_is_refused():
    with pytest.raises(OutcomeError, match="not below target"):
        evaluate(
            episode_id="ep-1",
            entry_reference_price=ENTRY,
            target_price=Decimal("1000"),
            initial_failure_line=Decimal("1000"),
            entry_price_observed_at=ENTRY_AT,
            currency="JPY",
            sessions=[_entry_session()],
        )


def test_the_report_summary_keeps_the_two_layers_apart():
    sessions = [_entry_session(), _session(1), _session(2, h=1300)]
    report = _run(sessions, thesis_invalidated_session=1)
    summary = report.summary

    assert summary["primary"] == "THESIS_INVALIDATED"
    assert summary["later_target_hit"] is True
