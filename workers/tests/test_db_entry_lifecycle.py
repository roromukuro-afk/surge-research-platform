"""The Phase 8 guards, checked against the database rather than the code.

The Python layer refuses these things too. That is not duplication for its own
sake: the Python check gives a job a clear error before it does the wrong thing,
and the database check holds when some future path does not come through the
Python at all. These tests are about the second one - every case here bypasses
``surge.entry.decision`` deliberately and writes straight to the table.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

psycopg2 = pytest.importorskip("psycopg2")

DSN = os.environ.get("SURGE_TEST_DATABASE_URL")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not DSN, reason="SURGE_TEST_DATABASE_URL is not set"),
]

CUTOFF = datetime(2026, 9, 17, 2, 0, tzinfo=UTC)
COMPLETED = datetime(2026, 9, 17, 2, 5, tzinfo=UTC)
ENTRY_AT = datetime(2026, 9, 17, 2, 6, tzinfo=UTC)


@pytest.fixture()
def conn():
    connection = psycopg2.connect(DSN)
    connection.autocommit = False
    try:
        yield connection
    finally:
        connection.rollback()
        connection.close()


def _security() -> str:
    return str(uuid.uuid4())


def _attempt(cur, **overrides) -> str:
    params = {
        "security_id": _security(),
        "status": "PREDICTION_CREATED",
        "analysis_kind": "ENTRY_DECISION",
        "decision_cutoff_at": CUTOFF,
        "decision_completed_at": COMPLETED,
        "decision_price": Decimal("1000"),
        "decision_price_observed_at": CUTOFF,
        "decision_price_jpy": Decimal("1000"),
        "entry_reference_price": Decimal("1010"),
        "entry_price_observed_at": ENTRY_AT,
        "entry_price_jpy": Decimal("1010"),
        "universe_decision": "INCLUDED",
    }
    params.update(overrides)
    cur.execute(
        """
        insert into prod.entry_attempts (
          security_id, status, analysis_kind, decision_cutoff_at, decision_completed_at,
          decision_price, decision_price_observed_at, decision_price_jpy,
          entry_reference_price, entry_price_observed_at, entry_price_jpy, universe_decision
        ) values (
          %(security_id)s, %(status)s::prod.entry_attempt_status,
          %(analysis_kind)s::prod.analysis_kind, %(decision_cutoff_at)s, %(decision_completed_at)s,
          %(decision_price)s, %(decision_price_observed_at)s, %(decision_price_jpy)s,
          %(entry_reference_price)s, %(entry_price_observed_at)s, %(entry_price_jpy)s,
          %(universe_decision)s::universe.decision
        ) returning attempt_id, security_id
        """,
        params,
    )
    return cur.fetchone()


def _episode(cur, security_id: str, thesis_key: str = "t-1") -> str:
    cur.execute(
        """
        insert into prod.episodes (security_id, thesis_key, opened_at, entry_price_observed_at)
        values (%s, %s, %s, %s) returning episode_id
        """,
        (security_id, thesis_key, ENTRY_AT, ENTRY_AT),
    )
    return cur.fetchone()[0]


def _prediction(cur, *, episode_id, attempt_id, security_id, **overrides):
    params = {
        "episode_id": episode_id,
        "attempt_id": attempt_id,
        "security_id": security_id,
        "thesis_key": "t-1",
        "analysis_kind": "ENTRY_DECISION",
        "entry_reference_price": Decimal("1010"),
        "entry_price_observed_at": ENTRY_AT,
        "entry_price_currency": "JPY",
        "entry_price_jpy": Decimal("1010"),
        "decision_price": Decimal("1000"),
        "decision_price_observed_at": CUTOFF,
        "decision_price_jpy": Decimal("1000"),
        "initial_failure_line": Decimal("940"),
        "target_price": Decimal("1010") * Decimal("1.20"),
        "data_cutoff": CUTOFF,
        "provider_id": "hosted-1",
        "provider_kind": "HOSTED_LLM",
        "rule_version": "entry-decision-1.0.0",
        "universe_decision": "INCLUDED",
    }
    params.update(overrides)
    cur.execute(
        """
        insert into prod.predictions (
          episode_id, attempt_id, security_id, thesis_key, analysis_kind,
          entry_reference_price, entry_price_observed_at, entry_price_currency, entry_price_jpy,
          decision_price, decision_price_observed_at, decision_price_jpy,
          initial_failure_line, target_price, data_cutoff,
          provider_id, provider_kind, rule_version, universe_decision
        ) values (
          %(episode_id)s, %(attempt_id)s, %(security_id)s, %(thesis_key)s,
          %(analysis_kind)s::prod.analysis_kind,
          %(entry_reference_price)s, %(entry_price_observed_at)s, %(entry_price_currency)s,
          %(entry_price_jpy)s,
          %(decision_price)s, %(decision_price_observed_at)s, %(decision_price_jpy)s,
          %(initial_failure_line)s, %(target_price)s, %(data_cutoff)s,
          %(provider_id)s, %(provider_kind)s, %(rule_version)s,
          %(universe_decision)s::universe.decision
        ) returning prediction_id
        """,
        params,
    )
    return cur.fetchone()[0]


# --------------------------------------------------------------- the happy path


def test_a_complete_entry_writes_an_attempt_an_episode_and_a_prediction(conn):
    with conn.cursor() as cur:
        attempt_id, security_id = _attempt(cur)
        episode_id = _episode(cur, security_id)
        prediction_id = _prediction(
            cur, episode_id=episode_id, attempt_id=attempt_id, security_id=security_id
        )

        cur.execute("select count(*) from prod.predictions where prediction_id = %s", (prediction_id,))
        assert cur.fetchone()[0] == 1


# ------------------------------------------------------- the four hard guards


def test_a_prediction_over_the_limit_is_refused_by_the_database(conn):
    with conn.cursor() as cur:
        attempt_id, security_id = _attempt(
            cur,
            entry_reference_price=Decimal("3005"),
            entry_price_jpy=Decimal("3005"),
            decision_price=Decimal("2990"),
            decision_price_jpy=Decimal("2990"),
        )
        episode_id = _episode(cur, security_id)
        with pytest.raises(psycopg2.errors.CheckViolation, match="entry_under_limit"):
            _prediction(
                cur,
                episode_id=episode_id,
                attempt_id=attempt_id,
                security_id=security_id,
                entry_reference_price=Decimal("3005"),
                entry_price_jpy=Decimal("3005"),
                decision_price=Decimal("2990"),
                decision_price_jpy=Decimal("2990"),
                initial_failure_line=Decimal("2800"),
                target_price=Decimal("3005") * Decimal("1.20"),
            )


def test_a_prediction_from_a_stand_in_provider_is_refused(conn):
    with conn.cursor() as cur:
        attempt_id, security_id = _attempt(cur)
        episode_id = _episode(cur, security_id)
        with pytest.raises(psycopg2.errors.CheckViolation, match="not_from_a_mock"):
            _prediction(
                cur,
                episode_id=episode_id,
                attempt_id=attempt_id,
                security_id=security_id,
                provider_kind="DETERMINISTIC_MOCK",
                provider_id="deterministic_mock",
            )


@pytest.mark.parametrize("decision", ["UNRESOLVED", "EXCLUDED"])
def test_a_prediction_outside_the_universe_is_refused(conn, decision):
    with conn.cursor() as cur:
        attempt_id, security_id = _attempt(cur, universe_decision=decision)
        episode_id = _episode(cur, security_id)
        with pytest.raises(psycopg2.errors.CheckViolation, match="universe_included"):
            _prediction(
                cur,
                episode_id=episode_id,
                attempt_id=attempt_id,
                security_id=security_id,
                universe_decision=decision,
            )


def test_a_target_that_is_not_twenty_percent_of_entry_is_refused(conn):
    """The one that would silently change what a success means."""

    with conn.cursor() as cur:
        attempt_id, security_id = _attempt(cur)
        episode_id = _episode(cur, security_id)
        with pytest.raises(psycopg2.errors.CheckViolation, match="target_is_twenty_percent"):
            _prediction(
                cur,
                episode_id=episode_id,
                attempt_id=attempt_id,
                security_id=security_id,
                target_price=Decimal("1000") * Decimal("1.20"),
            )


def test_an_end_of_day_analysis_cannot_produce_an_entry_attempt(conn):
    with conn.cursor() as cur:
        with pytest.raises(psycopg2.errors.CheckViolation, match="only_intraday"):
            _attempt(cur, analysis_kind="EOD")


def test_a_setup_row_cannot_carry_the_entry_state(conn):
    with conn.cursor() as cur:
        with pytest.raises(psycopg2.errors.CheckViolation, match="eod_never_enters"):
            cur.execute(
                """
                insert into prod.setups (
                  security_id, as_of_date, state, analysis_kind, price_cutoff_at, knowledge_cutoff_at
                ) values (%s, %s, 'ENTRY'::prod.decision_state, 'EOD'::prod.analysis_kind, %s, %s)
                """,
                (_security(), CUTOFF.date(), CUTOFF, CUTOFF),
            )


def test_a_post_close_catalyst_may_not_claim_to_be_priced_in(conn):
    """The close cannot have priced something published after it."""

    with conn.cursor() as cur:
        with pytest.raises(psycopg2.errors.CheckViolation, match="post_close_is_not_priced_in"):
            cur.execute(
                """
                insert into prod.setups (
                  security_id, as_of_date, state, analysis_kind,
                  price_cutoff_at, knowledge_cutoff_at, priced_in_status
                ) values (
                  %s, %s, 'POST_CLOSE_CATALYST_SETUP'::prod.decision_state,
                  'POST_CLOSE_MATERIAL'::prod.analysis_kind, %s, %s, 'EVALUATED_AGAINST_EOD'
                )
                """,
                (_security(), CUTOFF.date(), CUTOFF, CUTOFF),
            )


# ---------------------------------------------------------- append-only


def test_a_prediction_cannot_be_updated(conn):
    with conn.cursor() as cur:
        attempt_id, security_id = _attempt(cur)
        episode_id = _episode(cur, security_id)
        prediction_id = _prediction(
            cur, episode_id=episode_id, attempt_id=attempt_id, security_id=security_id
        )

        with pytest.raises(psycopg2.errors.RaiseException, match="append-only"):
            cur.execute(
                "update prod.predictions set initial_failure_line = 900 where prediction_id = %s",
                (prediction_id,),
            )


def test_a_prediction_cannot_be_deleted(conn):
    with conn.cursor() as cur:
        attempt_id, security_id = _attempt(cur)
        episode_id = _episode(cur, security_id)
        prediction_id = _prediction(
            cur, episode_id=episode_id, attempt_id=attempt_id, security_id=security_id
        )

        with pytest.raises(psycopg2.errors.RaiseException, match="append-only"):
            cur.execute("delete from prod.predictions where prediction_id = %s", (prediction_id,))


def test_a_risk_line_update_cannot_be_rewritten(conn):
    with conn.cursor() as cur:
        attempt_id, security_id = _attempt(cur)
        episode_id = _episode(cur, security_id)
        cur.execute(
            """
            insert into prod.risk_line_updates (episode_id, risk_line, reason, effective_at)
            values (%s, 940, 'initial', %s) returning update_id
            """,
            (episode_id, ENTRY_AT),
        )
        update_id = cur.fetchone()[0]

        with pytest.raises(psycopg2.errors.RaiseException, match="append-only"):
            cur.execute(
                "update prod.risk_line_updates set risk_line = 980 where update_id = %s",
                (update_id,),
            )


# ------------------------------------------------ prediction matches its attempt


def test_a_prediction_may_not_come_from_an_aborted_attempt(conn):
    with conn.cursor() as cur:
        attempt_id, security_id = _attempt(
            cur,
            status="ENTRY_ABORTED_PRICE_LIMIT",
            entry_reference_price=Decimal("3005"),
            entry_price_jpy=Decimal("3005"),
        )
        episode_id = _episode(cur, security_id)

        with pytest.raises(psycopg2.errors.RaiseException, match="only PREDICTION_CREATED"):
            _prediction(
                cur, episode_id=episode_id, attempt_id=attempt_id, security_id=security_id
            )


def test_a_prediction_may_not_carry_different_prices_than_its_attempt(conn):
    """The failure this catches: an attempt recorded honestly, and a prediction
    written beside it with a better entry price."""

    with conn.cursor() as cur:
        attempt_id, security_id = _attempt(cur)
        episode_id = _episode(cur, security_id)

        with pytest.raises(psycopg2.errors.RaiseException, match="does not carry the prices"):
            _prediction(
                cur,
                episode_id=episode_id,
                attempt_id=attempt_id,
                security_id=security_id,
                entry_reference_price=Decimal("995"),
                entry_price_jpy=Decimal("995"),
                target_price=Decimal("995") * Decimal("1.20"),
            )


def test_an_abort_must_show_a_price_over_the_limit(conn):
    """An abort recorded without the price that caused it is unfalsifiable."""

    with conn.cursor() as cur:
        with pytest.raises(psycopg2.errors.CheckViolation, match="abort_shows_the_price"):
            _attempt(
                cur,
                status="ENTRY_ABORTED_PRICE_LIMIT",
                entry_price_jpy=Decimal("1010"),
            )


def test_an_entry_price_observed_before_the_decision_finished_is_refused(conn):
    with conn.cursor() as cur:
        with pytest.raises(psycopg2.errors.CheckViolation, match="entry_after_decision"):
            _attempt(cur, entry_price_observed_at=COMPLETED - timedelta(seconds=1))


# ------------------------------------------------------------ episodes


def test_only_one_episode_may_be_open_per_security_and_thesis(conn):
    with conn.cursor() as cur:
        security_id = _security()
        _episode(cur, security_id, "same-thesis")

        with pytest.raises(psycopg2.errors.UniqueViolation):
            _episode(cur, security_id, "same-thesis")


def test_a_second_episode_under_a_different_thesis_is_allowed(conn):
    with conn.cursor() as cur:
        security_id = _security()
        first = _episode(cur, security_id, "thesis-a")
        second = _episode(cur, security_id, "thesis-b")

        assert first != second


def test_a_closed_episode_frees_the_thesis_for_a_new_one(conn):
    with conn.cursor() as cur:
        security_id = _security()
        first = _episode(cur, security_id, "thesis-a")
        cur.execute(
            """
            update prod.episodes
               set status = 'CLOSED', closed_at = %s, close_reason = 'TARGET_HIT'
             where episode_id = %s
            """,
            (ENTRY_AT + timedelta(days=5), first),
        )

        second = _episode(cur, security_id, "thesis-a")
        assert second != first


def test_the_horizon_cannot_be_set_to_anything_but_twenty(conn):
    with conn.cursor() as cur:
        with pytest.raises(psycopg2.errors.CheckViolation, match="horizon_is_twenty"):
            cur.execute(
                """
                insert into prod.episodes (
                  security_id, thesis_key, opened_at, entry_price_observed_at, horizon_sessions
                ) values (%s, 't-1', %s, %s, 30)
                """,
                (_security(), ENTRY_AT, ENTRY_AT),
            )


def test_a_closed_episode_must_say_why(conn):
    with conn.cursor() as cur:
        with pytest.raises(psycopg2.errors.CheckViolation, match="closed_has_reason"):
            cur.execute(
                """
                insert into prod.episodes (
                  security_id, thesis_key, status, opened_at, entry_price_observed_at, closed_at
                ) values (%s, 't-1', 'CLOSED', %s, %s, %s)
                """,
                (_security(), ENTRY_AT, ENTRY_AT, ENTRY_AT),
            )


# ------------------------------------------------------------ the watch machine


def _watch(cur) -> tuple[str, str]:
    security_id = _security()
    cur.execute(
        """
        insert into prod.setups (
          security_id, as_of_date, state, analysis_kind, price_cutoff_at, knowledge_cutoff_at
        ) values (%s, %s, 'WATCH_BREAKOUT'::prod.decision_state, 'EOD'::prod.analysis_kind, %s, %s)
        returning setup_id
        """,
        (security_id, CUTOFF.date(), CUTOFF, CUTOFF),
    )
    setup_id = cur.fetchone()[0]
    cur.execute(
        """
        insert into prod.watches (setup_id, security_id, trigger_description)
        values (%s, %s, 'breaks the prior high') returning watch_id
        """,
        (setup_id, security_id),
    )
    return cur.fetchone()[0], security_id


def _move(cur, watch_id, frm, to, *, kind=None, at=CUTOFF):
    cur.execute(
        """
        insert into prod.watch_transitions (watch_id, from_state, to_state, occurred_at, analysis_kind)
        values (%s, %s::prod.watch_state, %s::prod.watch_state, %s, %s::prod.analysis_kind)
        """,
        (watch_id, frm, to, at, kind),
    )


def test_a_watch_cannot_go_from_trigger_straight_to_entered(conn):
    """CLAUDE.md 1-5, at the level where it cannot be bypassed."""

    with conn.cursor() as cur:
        watch_id, _ = _watch(cur)
        _move(cur, watch_id, "ARMED", "TRIGGER_HIT", kind="WATCH_MONITOR")

        with pytest.raises(psycopg2.errors.RaiseException, match="REANALYSIS must run first"):
            _move(cur, watch_id, "TRIGGER_HIT", "ENTERED", kind="REANALYSIS")


def test_the_legal_path_through_a_reanalysis_is_accepted(conn):
    with conn.cursor() as cur:
        watch_id, _ = _watch(cur)
        _move(cur, watch_id, "ARMED", "TRIGGER_HIT", kind="WATCH_MONITOR")
        _move(cur, watch_id, "TRIGGER_HIT", "IN_REANALYSIS", kind="REANALYSIS")
        _move(cur, watch_id, "IN_REANALYSIS", "ENTERED", kind="REANALYSIS")

        cur.execute("select state::text from prod.watches where watch_id = %s", (watch_id,))
        assert cur.fetchone()[0] == "ENTERED"


def test_entering_without_a_reanalysis_kind_is_refused(conn):
    with conn.cursor() as cur:
        watch_id, _ = _watch(cur)
        _move(cur, watch_id, "ARMED", "TRIGGER_HIT", kind="WATCH_MONITOR")
        _move(cur, watch_id, "TRIGGER_HIT", "IN_REANALYSIS", kind="REANALYSIS")

        with pytest.raises(psycopg2.errors.RaiseException, match="requires analysis_kind"):
            _move(cur, watch_id, "IN_REANALYSIS", "ENTERED", kind="WATCH_MONITOR")


def test_a_rejected_watch_is_terminal(conn):
    with conn.cursor() as cur:
        watch_id, _ = _watch(cur)
        _move(cur, watch_id, "ARMED", "TRIGGER_HIT", kind="WATCH_MONITOR")
        _move(cur, watch_id, "TRIGGER_HIT", "IN_REANALYSIS", kind="REANALYSIS")
        _move(cur, watch_id, "IN_REANALYSIS", "REJECTED", kind="REANALYSIS")

        with pytest.raises(psycopg2.errors.RaiseException, match="illegal watch transition"):
            _move(cur, watch_id, "REJECTED", "TRIGGER_HIT", kind="WATCH_MONITOR")


def test_a_watch_transition_cannot_be_rewritten(conn):
    with conn.cursor() as cur:
        watch_id, _ = _watch(cur)
        _move(cur, watch_id, "ARMED", "TRIGGER_HIT", kind="WATCH_MONITOR")

        with pytest.raises(psycopg2.errors.RaiseException, match="append-only"):
            cur.execute(
                "update prod.watch_transitions set to_state = 'ENTERED' where watch_id = %s",
                (watch_id,),
            )


# ------------------------------------------------------------ the read contracts


def test_the_open_episode_view_shows_both_failure_lines(conn):
    with conn.cursor() as cur:
        attempt_id, security_id = _attempt(cur)
        episode_id = _episode(cur, security_id)
        _prediction(cur, episode_id=episode_id, attempt_id=attempt_id, security_id=security_id)
        cur.execute(
            """
            insert into prod.risk_line_updates (episode_id, risk_line, reason, effective_at)
            values (%s, 940, 'initial', %s), (%s, 980, 'trailed', %s)
            """,
            (episode_id, ENTRY_AT, episode_id, ENTRY_AT + timedelta(days=2)),
        )

        cur.execute(
            """
            select initial_failure_line, current_risk_line, verification
            from ui.open_episodes where episode_id = %s
            """,
            (episode_id,),
        )
        initial, current, verification = cur.fetchone()

        assert initial == Decimal("940")
        assert current == Decimal("980")
        assert verification == "IMPLEMENTED_NOT_LIVE_VERIFIED"


def test_the_attempt_ledger_counts_the_attempts_that_produced_nothing(conn):
    """The denominator. A ledger of only successful entries makes any hit rate
    computed from it meaningless."""

    with conn.cursor() as cur:
        aborted, _ = _attempt(
            cur,
            status="ENTRY_ABORTED_PRICE_LIMIT",
            entry_reference_price=Decimal("3005"),
            entry_price_jpy=Decimal("3005"),
        )

        cur.execute(
            "select produced_a_prediction, status from ui.entry_attempt_ledger where attempt_id = %s",
            (aborted,),
        )
        produced, status = cur.fetchone()

        assert produced is False
        assert status == "ENTRY_ABORTED_PRICE_LIMIT"
