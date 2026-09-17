"""The Phase 8 guards, checked against the database rather than the code.

The Python layer refuses these things too. That is not duplication for its own
sake: the Python check gives a job a clear error before it does the wrong thing,
and the database check holds when some future path does not come through the
Python at all. Every case here bypasses ``surge.entry.decision`` deliberately
and writes straight to the table.

Several of these exist because the first version of the guard did not hold. The
watch machine trusted the caller's ``from_state`` and used a simple CASE whose
``when null`` branch cannot match, so a brand new watch could be inserted
straight into ENTERED - which it was, against the live database, before this was
fixed.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

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

#: The fields an attempt and its prediction must agree on. Kept in one place so
#: the happy path agrees by construction and each test can break exactly one.
SHARED = {
    "thesis_key": "t-1",
    "analysis_kind": "ENTRY_DECISION",
    "decision_price": Decimal("1000"),
    "decision_price_observed_at": CUTOFF,
    "decision_price_jpy": Decimal("1000"),
    "entry_reference_price": Decimal("1010"),
    "entry_price_observed_at": ENTRY_AT,
    "entry_price_jpy": Decimal("1010"),
    "entry_price_method": "first trade after the decision completed",
    "universe_decision": "INCLUDED",
    "provider_id": "hosted-1",
    "verification": "IMPLEMENTED_NOT_LIVE_VERIFIED",
}


def target_for(price: Decimal) -> Decimal:
    """The same arithmetic the constraint does, and the same rounding."""

    return (price * Decimal("1.20")).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)


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


def _attempt(cur, **overrides):
    params = {
        "security_id": _security(),
        "status": "PREDICTION_CREATED",
        "decision_cutoff_at": CUTOFF,
        "decision_completed_at": COMPLETED,
        "decision_price_currency": "JPY",
        **SHARED,
    }
    params.update(overrides)
    cur.execute(
        """
        insert into prod.entry_attempts (
          security_id, status, analysis_kind, decision_cutoff_at, decision_completed_at,
          decision_price, decision_price_observed_at, decision_price_currency, decision_price_jpy,
          entry_reference_price, entry_price_observed_at, entry_price_method, entry_price_jpy,
          universe_decision, thesis_key, provider_id, verification
        ) values (
          %(security_id)s, %(status)s::prod.entry_attempt_status,
          %(analysis_kind)s::prod.analysis_kind, %(decision_cutoff_at)s, %(decision_completed_at)s,
          %(decision_price)s, %(decision_price_observed_at)s, %(decision_price_currency)s,
          %(decision_price_jpy)s,
          %(entry_reference_price)s, %(entry_price_observed_at)s, %(entry_price_method)s,
          %(entry_price_jpy)s,
          %(universe_decision)s::universe.decision, %(thesis_key)s, %(provider_id)s,
          %(verification)s::prod.verification_status
        ) returning attempt_id, security_id
        """,
        params,
    )
    return cur.fetchone()


def _episode(cur, security_id: str, thesis_key: str = "t-1", entry_at=ENTRY_AT) -> str:
    cur.execute(
        """
        insert into prod.episodes (security_id, thesis_key, opened_at, entry_price_observed_at)
        values (%s, %s, %s, %s) returning episode_id
        """,
        (security_id, thesis_key, entry_at, entry_at),
    )
    return cur.fetchone()[0]


def _prediction(cur, *, episode_id, attempt_id, security_id, **overrides):
    params = {
        "episode_id": episode_id,
        "attempt_id": attempt_id,
        "security_id": security_id,
        "entry_price_currency": "JPY",
        "initial_failure_line": Decimal("940"),
        "target_price": target_for(SHARED["entry_reference_price"]),
        "data_cutoff": CUTOFF,
        "provider_kind": "HOSTED_LLM",
        "rule_version": "entry-decision-1.0.0",
        **SHARED,
    }
    params.update(overrides)
    cur.execute(
        """
        insert into prod.predictions (
          episode_id, attempt_id, security_id, thesis_key, analysis_kind,
          entry_reference_price, entry_price_observed_at, entry_price_currency,
          entry_price_method, entry_price_jpy,
          decision_price, decision_price_observed_at, decision_price_jpy,
          initial_failure_line, target_price, data_cutoff,
          provider_id, provider_kind, rule_version, universe_decision, verification
        ) values (
          %(episode_id)s, %(attempt_id)s, %(security_id)s, %(thesis_key)s,
          %(analysis_kind)s::prod.analysis_kind,
          %(entry_reference_price)s, %(entry_price_observed_at)s, %(entry_price_currency)s,
          %(entry_price_method)s, %(entry_price_jpy)s,
          %(decision_price)s, %(decision_price_observed_at)s, %(decision_price_jpy)s,
          %(initial_failure_line)s, %(target_price)s, %(data_cutoff)s,
          %(provider_id)s, %(provider_kind)s, %(rule_version)s,
          %(universe_decision)s::universe.decision, %(verification)s::prod.verification_status
        ) returning prediction_id
        """,
        params,
    )
    return cur.fetchone()[0]


def _entered(cur):
    """A complete, consistent entry. The baseline every guard test breaks."""

    attempt_id, security_id = _attempt(cur)
    episode_id = _episode(cur, security_id)
    prediction_id = _prediction(
        cur, episode_id=episode_id, attempt_id=attempt_id, security_id=security_id
    )
    return attempt_id, episode_id, prediction_id, security_id


# --------------------------------------------------------------- the happy path


def test_a_complete_entry_writes_an_attempt_an_episode_and_a_prediction(conn):
    with conn.cursor() as cur:
        _, _, prediction_id, _ = _entered(cur)
        cur.execute(
            "select count(*) from prod.predictions where prediction_id = %s", (prediction_id,)
        )
        assert cur.fetchone()[0] == 1


# ------------------------------------------------------- the four hard guards


def test_a_prediction_over_the_limit_is_refused_by_the_database(conn):
    with conn.cursor() as cur:
        over = {
            "entry_reference_price": Decimal("3005"),
            "entry_price_jpy": Decimal("3005"),
            "decision_price": Decimal("2990"),
            "decision_price_jpy": Decimal("2990"),
        }
        attempt_id, security_id = _attempt(cur, **over)
        episode_id = _episode(cur, security_id)
        with pytest.raises(psycopg2.errors.CheckViolation, match="entry_under_limit"):
            _prediction(
                cur,
                episode_id=episode_id,
                attempt_id=attempt_id,
                security_id=security_id,
                initial_failure_line=Decimal("2800"),
                target_price=target_for(Decimal("3005")),
                **over,
            )


def test_a_prediction_from_a_stand_in_provider_is_refused(conn):
    with conn.cursor() as cur:
        attempt_id, security_id = _attempt(cur, provider_id="deterministic_mock")
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


# ------------------------------------------- D. the target, without a tolerance


def test_the_target_must_equal_the_rounded_arithmetic_exactly(conn):
    """Was a tolerance comparison. Two numerics have nothing to tolerate, and a
    tolerance is a place a wrong number can sit."""

    with conn.cursor() as cur:
        attempt_id, security_id = _attempt(cur)
        episode_id = _episode(cur, security_id)
        exact = target_for(Decimal("1010"))

        with pytest.raises(psycopg2.errors.CheckViolation, match="target_is_twenty_percent"):
            _prediction(
                cur,
                episode_id=episode_id,
                attempt_id=attempt_id,
                security_id=security_id,
                target_price=exact + Decimal("0.000001"),
            )


def test_a_price_whose_twenty_percent_needs_rounding_still_matches(conn):
    """1234.567891 * 1.2 has more decimals than the column holds. Python and the
    database have to round it the same way or nothing with an odd price stores."""

    with conn.cursor() as cur:
        price = Decimal("1234.567891")
        shared = {"entry_reference_price": price, "entry_price_jpy": price}
        attempt_id, security_id = _attempt(cur, **shared)
        episode_id = _episode(cur, security_id)

        _prediction(
            cur,
            episode_id=episode_id,
            attempt_id=attempt_id,
            security_id=security_id,
            target_price=target_for(price),
            initial_failure_line=Decimal("1100"),
            **shared,
        )

        cur.execute("select target_price from prod.predictions where attempt_id = %s", (attempt_id,))
        assert cur.fetchone()[0] == target_for(price)


def test_the_python_and_database_targets_agree():
    """The two implementations of the same arithmetic, compared directly."""

    from surge.entry.models import target_for as python_target

    for raw in ("1010", "1234.567891", "0.000001", "2999.999999", "3000"):
        assert python_target(Decimal(raw)) == target_for(Decimal(raw))


# ------------------------------------------ B. prediction / attempt / episode


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("thesis_key", "a-different-thesis"),
        ("analysis_kind", "REANALYSIS"),
        ("decision_price_jpy", Decimal("999")),
        ("entry_price_method", "something else"),
        ("provider_id", "another-provider"),
        ("verification", "LIVE_VERIFIED"),
        ("data_cutoff", CUTOFF - timedelta(minutes=1)),
    ],
)
def test_a_prediction_that_disagrees_with_its_attempt_anywhere_is_refused(conn, field, value):
    """One honest attempt, one flattering prediction beside it. Every field that
    could carry the flattery is compared."""

    with conn.cursor() as cur:
        attempt_id, security_id = _attempt(cur)
        episode_id = _episode(cur, security_id)

        with pytest.raises(psycopg2.errors.RaiseException, match="disagrees with its attempt"):
            _prediction(
                cur,
                episode_id=episode_id,
                attempt_id=attempt_id,
                security_id=security_id,
                **{field: value},
            )


def test_a_prediction_may_not_come_from_an_aborted_attempt(conn):
    with conn.cursor() as cur:
        over = {"entry_reference_price": Decimal("3005"), "entry_price_jpy": Decimal("3005")}
        attempt_id, security_id = _attempt(cur, status="ENTRY_ABORTED_PRICE_LIMIT", **over)
        episode_id = _episode(cur, security_id)

        with pytest.raises(psycopg2.errors.RaiseException, match="only PREDICTION_CREATED"):
            _prediction(
                cur, episode_id=episode_id, attempt_id=attempt_id, security_id=security_id, **over
            )


def test_a_prediction_cannot_borrow_another_securitys_episode(conn):
    with conn.cursor() as cur:
        attempt_id, security_id = _attempt(cur)
        other_episode = _episode(cur, _security())

        with pytest.raises(psycopg2.errors.RaiseException, match="but episode .* is on"):
            _prediction(
                cur,
                episode_id=other_episode,
                attempt_id=attempt_id,
                security_id=security_id,
            )


def test_a_prediction_cannot_join_an_episode_under_a_different_thesis(conn):
    with conn.cursor() as cur:
        attempt_id, security_id = _attempt(cur)
        episode_id = _episode(cur, security_id, thesis_key="a-different-thesis")

        with pytest.raises(psycopg2.errors.RaiseException, match="under thesis"):
            _prediction(
                cur, episode_id=episode_id, attempt_id=attempt_id, security_id=security_id
            )


def test_an_episode_that_starts_its_horizon_elsewhere_is_refused(conn):
    """S0 and the entry are the same moment. An episode starting earlier would
    give the prediction extra sessions."""

    with conn.cursor() as cur:
        attempt_id, security_id = _attempt(cur)
        episode_id = _episode(cur, security_id, entry_at=ENTRY_AT - timedelta(days=2))

        with pytest.raises(psycopg2.errors.RaiseException, match="S0 and the entry"):
            _prediction(
                cur, episode_id=episode_id, attempt_id=attempt_id, security_id=security_id
            )


def test_an_abort_must_show_a_price_over_the_limit(conn):
    """An abort recorded without the price that caused it is unfalsifiable."""

    with conn.cursor() as cur:
        with pytest.raises(psycopg2.errors.CheckViolation, match="abort_shows_the_price"):
            _attempt(cur, status="ENTRY_ABORTED_PRICE_LIMIT")


def test_an_entry_price_observed_before_the_decision_finished_is_refused(conn):
    with conn.cursor() as cur:
        with pytest.raises(psycopg2.errors.CheckViolation, match="entry_after_decision"):
            _attempt(cur, entry_price_observed_at=COMPLETED - timedelta(seconds=1))


# ------------------------------------------------- C. the mutation boundary


def test_a_prediction_cannot_be_updated(conn):
    with conn.cursor() as cur:
        _, _, prediction_id, _ = _entered(cur)
        with pytest.raises(psycopg2.errors.RaiseException, match="append-only"):
            cur.execute(
                "update prod.predictions set initial_failure_line = 900 where prediction_id = %s",
                (prediction_id,),
            )


def test_a_prediction_cannot_be_deleted(conn):
    with conn.cursor() as cur:
        _, _, prediction_id, _ = _entered(cur)
        with pytest.raises(psycopg2.errors.RaiseException, match="append-only"):
            cur.execute("delete from prod.predictions where prediction_id = %s", (prediction_id,))


def test_a_setup_cannot_be_edited_after_the_fact(conn):
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into prod.setups (
              security_id, as_of_date, state, analysis_kind, price_cutoff_at, knowledge_cutoff_at
            ) values (%s, %s, 'WATCH_BREAKOUT'::prod.decision_state, 'EOD'::prod.analysis_kind, %s, %s)
            returning setup_id
            """,
            (_security(), CUTOFF.date(), CUTOFF, CUTOFF),
        )
        setup_id = cur.fetchone()[0]

        with pytest.raises(psycopg2.errors.RaiseException, match="append-only"):
            cur.execute(
                "update prod.setups set rationale = 'reworded' where setup_id = %s", (setup_id,)
            )


def test_a_risk_line_update_cannot_be_rewritten(conn):
    with conn.cursor() as cur:
        _, episode_id, _, _ = _entered(cur)
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


def _close(cur, episode_id, reason="TARGET_HIT", at=None, **outcome):
    """The only way an episode closes. A direct UPDATE is refused."""

    params = {
        "episode_id": episode_id,
        "reason": reason,
        "closed_at": at or ENTRY_AT + timedelta(days=5),
        "sessions": 21 if reason == "HORIZON_EXPIRED" else 5,
        "resolved": 20 if reason == "HORIZON_EXPIRED" else 3,
        "later": False if reason == "HORIZON_EXPIRED" else None,
    }
    params.update(outcome)
    cur.execute(
        """
        select prod.close_episode_with_outcome(
          %(episode_id)s::uuid, %(reason)s::prod.episode_close_reason, %(closed_at)s,
          null, null, %(resolved)s, null, null, null, %(later)s, null, null, null, null,
          %(sessions)s, '{}', 'JPY', 'outcome-engine-1.0.0', null, null
        )
        """,
        params,
    )


def test_closing_directly_is_refused(conn):
    """The gap this migration closes: the worker could take an episode
    OPEN -> CLOSED and simply never write the outcome."""

    with conn.cursor() as cur:
        _, episode_id, _, _ = _entered(cur)

        with pytest.raises(psycopg2.errors.RaiseException, match="close_episode_with_outcome"):
            cur.execute(
                """
                update prod.episodes
                   set status = 'CLOSED', closed_at = %s, close_reason = 'TARGET_HIT'
                 where episode_id = %s
                """,
                (ENTRY_AT, episode_id),
            )


def test_an_outcome_cannot_be_inserted_directly(conn):
    with conn.cursor() as cur:
        _, episode_id, _, _ = _entered(cur)

        with pytest.raises(psycopg2.errors.RaiseException, match="not inserted directly"):
            cur.execute(
                """
                insert into prod.episode_outcomes (episode_id, primary_episode_outcome)
                values (%s, 'TARGET_HIT')
                """,
                (episode_id,),
            )


def test_an_outcome_cannot_be_updated(conn):
    with conn.cursor() as cur:
        _, episode_id, _, _ = _entered(cur)
        _close(cur, episode_id)

        with pytest.raises(psycopg2.errors.RaiseException, match="append-only"):
            cur.execute(
                "update prod.episode_outcomes set counterfactual_mfe = 9 where episode_id = %s",
                (episode_id,),
            )


def test_a_horizon_expiry_before_s20_is_refused_by_the_database(conn):
    """HORIZON_EXPIRED is a statement about S20. A writer that has seen eight
    sessions has not seen S20."""

    with conn.cursor() as cur:
        _, episode_id, _, _ = _entered(cur)

        with pytest.raises(psycopg2.errors.CheckViolation, match="expiry_needs_the_whole_horizon"):
            _close(cur, episode_id, reason="HORIZON_EXPIRED", sessions=8, resolved=8, later=False)


def test_a_horizon_expiry_cannot_claim_the_target_was_reached(conn):
    with conn.cursor() as cur:
        _, episode_id, _, _ = _entered(cur)

        with pytest.raises(psycopg2.errors.CheckViolation, match="expiry_means_target_not_reached"):
            _close(cur, episode_id, reason="HORIZON_EXPIRED", later=True)


def test_a_horizon_expiry_with_the_whole_window_is_accepted(conn):
    with conn.cursor() as cur:
        _, episode_id, _, _ = _entered(cur)
        _close(cur, episode_id, reason="HORIZON_EXPIRED")

        cur.execute(
            "select close_reason::text from prod.episodes where episode_id = %s", (episode_id,)
        )
        assert cur.fetchone()[0] == "HORIZON_EXPIRED"


def test_an_unknown_later_target_hit_stays_null(conn):
    """Three-valued. Defaulting NULL to false would turn "we have not finished
    looking" into "it did not happen"."""

    with conn.cursor() as cur:
        _, episode_id, _, _ = _entered(cur)
        _close(cur, episode_id, reason="INITIAL_FAILURE_HIT", later=None)

        cur.execute(
            "select counterfactual_later_target_hit from prod.episode_outcomes where episode_id = %s",
            (episode_id,),
        )
        assert cur.fetchone()[0] is None


def test_an_episode_can_be_closed_once(conn):
    with conn.cursor() as cur:
        _, episode_id, _, _ = _entered(cur)
        _close(cur, episode_id)

        cur.execute(
            "select status::text, close_reason::text from prod.episodes where episode_id = %s",
            (episode_id,),
        )
        assert cur.fetchone() == ("CLOSED", "TARGET_HIT")


def test_a_closed_episode_cannot_be_reopened(conn):
    with conn.cursor() as cur:
        _, episode_id, _, _ = _entered(cur)
        _close(cur, episode_id)

        with pytest.raises(psycopg2.errors.RaiseException, match="is closed"):
            cur.execute(
                """
                update prod.episodes set status = 'OPEN', closed_at = null, close_reason = null
                 where episode_id = %s
                """,
                (episode_id,),
            )


def test_a_closed_episode_cannot_have_its_reason_changed(conn):
    """Which outcome an episode ended with is decided once."""

    with conn.cursor() as cur:
        _, episode_id, _, _ = _entered(cur)
        _close(cur, episode_id, reason="INITIAL_FAILURE_HIT")

        with pytest.raises(psycopg2.errors.RaiseException, match="is closed"):
            cur.execute(
                "update prod.episodes set close_reason = 'TARGET_HIT' where episode_id = %s",
                (episode_id,),
            )


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("entry_price_observed_at", ENTRY_AT - timedelta(days=3)),
        ("thesis_key", "something-else"),
        ("opened_at", ENTRY_AT - timedelta(days=3)),
        ("verification", "LIVE_VERIFIED"),
    ],
)
def test_the_identity_columns_of_an_episode_are_frozen(conn, column, value):
    """Moving entry_price_observed_at would move S0, and therefore the horizon,
    after the outcome is already visible."""

    with conn.cursor() as cur:
        _, episode_id, _, _ = _entered(cur)
        cast = "::prod.verification_status" if column == "verification" else ""

        with pytest.raises(psycopg2.errors.RaiseException, match="only status, closed_at"):
            cur.execute(
                f"update prod.episodes set {column} = %s{cast} where episode_id = %s",
                (value, episode_id),
            )


def test_an_episode_cannot_be_deleted(conn):
    with conn.cursor() as cur:
        _, episode_id, _, _ = _entered(cur)
        with pytest.raises(psycopg2.errors.RaiseException, match="not deleted from"):
            cur.execute("delete from prod.episodes where episode_id = %s", (episode_id,))


def test_closing_without_a_reason_is_refused(conn):
    """Through the function, since that is now the only door."""

    with conn.cursor() as cur:
        _, episode_id, _, _ = _entered(cur)
        with pytest.raises(psycopg2.errors.RaiseException, match="both closed_at and close_reason"):
            _close(cur, episode_id, reason=None)


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
        assert _episode(cur, security_id, "thesis-a") != _episode(cur, security_id, "thesis-b")


def test_a_closed_episode_frees_the_thesis_for_a_new_one(conn):
    with conn.cursor() as cur:
        security_id = _security()
        first = _episode(cur, security_id, "thesis-a")
        _close(cur, first)

        assert _episode(cur, security_id, "thesis-a") != first


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


# ----------------------------------------- A. the watch machine, for real


def _setup(cur, security_id=None) -> tuple[str, str]:
    security_id = security_id or _security()
    cur.execute(
        """
        insert into prod.setups (
          security_id, as_of_date, state, analysis_kind, price_cutoff_at, knowledge_cutoff_at
        ) values (%s, %s, 'WATCH_BREAKOUT'::prod.decision_state, 'EOD'::prod.analysis_kind, %s, %s)
        returning setup_id
        """,
        (security_id, CUTOFF.date(), CUTOFF, CUTOFF),
    )
    return cur.fetchone()[0], security_id


def _watch(cur) -> tuple[str, str]:
    setup_id, security_id = _setup(cur)
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


def _arm_and_trigger(cur):
    watch_id, _ = _watch(cur)
    _move(cur, watch_id, "ARMED", "TRIGGER_HIT", kind="WATCH_MONITOR")
    return watch_id


def test_a_fresh_watch_cannot_be_inserted_straight_into_entered(conn):
    """The hole this migration exists for.

    ``case new.from_state when null then ...`` compares with ``=``, so NULL never
    matched and ``legal`` stayed NULL; ``if not NULL`` is not true, so nothing was
    raised. Probed against the live database before the fix: the watch reached
    ENTERED with no trigger and no reanalysis.
    """

    with conn.cursor() as cur:
        watch_id, _ = _watch(cur)

        with pytest.raises(psycopg2.errors.RaiseException, match="being armed"):
            _move(cur, watch_id, None, "ENTERED", kind="REANALYSIS")


def test_a_null_origin_may_only_arm(conn):
    with conn.cursor() as cur:
        watch_id, _ = _watch(cur)
        with pytest.raises(psycopg2.errors.RaiseException, match="being armed"):
            _move(cur, watch_id, None, "TRIGGER_HIT", kind="WATCH_MONITOR")


def test_a_watch_cannot_be_armed_twice(conn):
    with conn.cursor() as cur:
        watch_id, _ = _watch(cur)
        _move(cur, watch_id, None, "ARMED")

        with pytest.raises(psycopg2.errors.RaiseException, match="already armed"):
            _move(cur, watch_id, None, "ARMED")


def test_a_declared_from_state_that_disagrees_with_the_watch_is_refused(conn):
    """The second half of the hole: the trigger believed the caller about where
    the watch was, so a caller could simply claim to be one step further on."""

    with conn.cursor() as cur:
        watch_id, _ = _watch(cur)

        with pytest.raises(psycopg2.errors.RaiseException, match="is in state ARMED, not"):
            _move(cur, watch_id, "IN_REANALYSIS", "ENTERED", kind="REANALYSIS")


def test_a_watch_cannot_go_from_trigger_straight_to_entered(conn):
    """CLAUDE.md 1-5, at the level where it cannot be bypassed."""

    with conn.cursor() as cur:
        watch_id = _arm_and_trigger(cur)

        with pytest.raises(psycopg2.errors.RaiseException, match="REANALYSIS must run first"):
            _move(cur, watch_id, "TRIGGER_HIT", "ENTERED", kind="REANALYSIS")


def test_the_legal_path_through_a_reanalysis_is_accepted(conn):
    with conn.cursor() as cur:
        watch_id = _arm_and_trigger(cur)
        _move(cur, watch_id, "TRIGGER_HIT", "IN_REANALYSIS", kind="REANALYSIS")
        _move(cur, watch_id, "IN_REANALYSIS", "ENTERED", kind="REANALYSIS")

        cur.execute("select state::text from prod.watches where watch_id = %s", (watch_id,))
        assert cur.fetchone()[0] == "ENTERED"


def test_entering_without_a_reanalysis_kind_is_refused(conn):
    with conn.cursor() as cur:
        watch_id = _arm_and_trigger(cur)
        _move(cur, watch_id, "TRIGGER_HIT", "IN_REANALYSIS", kind="REANALYSIS")

        with pytest.raises(psycopg2.errors.RaiseException, match="requires analysis_kind"):
            _move(cur, watch_id, "IN_REANALYSIS", "ENTERED", kind="WATCH_MONITOR")


def test_a_rejected_watch_is_terminal(conn):
    with conn.cursor() as cur:
        watch_id = _arm_and_trigger(cur)
        _move(cur, watch_id, "TRIGGER_HIT", "IN_REANALYSIS", kind="REANALYSIS")
        _move(cur, watch_id, "IN_REANALYSIS", "REJECTED", kind="REANALYSIS")

        with pytest.raises(psycopg2.errors.RaiseException, match="illegal watch transition"):
            _move(cur, watch_id, "REJECTED", "TRIGGER_HIT", kind="WATCH_MONITOR")


def test_a_watch_transition_cannot_be_rewritten(conn):
    with conn.cursor() as cur:
        watch_id = _arm_and_trigger(cur)

        with pytest.raises(psycopg2.errors.RaiseException, match="append-only"):
            cur.execute(
                "update prod.watch_transitions set to_state = 'ENTERED' where watch_id = %s",
                (watch_id,),
            )


def test_the_watch_head_cannot_be_updated_directly(conn):
    """The head row moves from inside the transition trigger and nowhere else."""

    with conn.cursor() as cur:
        watch_id, _ = _watch(cur)

        with pytest.raises(psycopg2.errors.RaiseException, match="not by updating this row"):
            cur.execute(
                "update prod.watches set state = 'ENTERED' where watch_id = %s", (watch_id,)
            )


def test_a_watch_cannot_be_created_half_way_through_the_machine(conn):
    with conn.cursor() as cur:
        setup_id, security_id = _setup(cur)

        with pytest.raises(psycopg2.errors.RaiseException, match="starts ARMED"):
            cur.execute(
                """
                insert into prod.watches (setup_id, security_id, trigger_description, state)
                values (%s, %s, 'probe', 'IN_REANALYSIS'::prod.watch_state)
                """,
                (setup_id, security_id),
            )


def test_a_watch_must_be_on_the_same_security_as_its_setup(conn):
    with conn.cursor() as cur:
        setup_id, _ = _setup(cur)

        with pytest.raises(psycopg2.errors.RaiseException, match="different security"):
            cur.execute(
                """
                insert into prod.watches (setup_id, security_id, trigger_description)
                values (%s, %s, 'probe')
                """,
                (setup_id, _security()),
            )


# ------------------------------------------------------------ the read contracts


def test_the_open_episode_view_shows_both_failure_lines(conn):
    with conn.cursor() as cur:
        _, episode_id, _, _ = _entered(cur)
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


# --------------------------------------- Phase 9: closing and recording as one


def _outcome(cur, episode_id, **overrides):
    params = {
        "episode_id": episode_id,
        "primary_outcome": "TARGET_HIT",
        "closed_at": ENTRY_AT + timedelta(days=5),
        "path_resolution": "TARGET_FIRST",
        "granularity": "DAY",
        "resolved_session_index": 3,
        "resolved_at": None,
        "primary_detail": "the high reached the target",
        "counterfactual_path": "TARGET_FIRST",
        "later_target_hit": True,
        "later_target_hit_at": None,
        "later_target_hit_session_index": 3,
        "mfe": Decimal("0.21"),
        "mae": Decimal("-0.03"),
        "sessions_observed": 21,
        "corporate_action_ids": [],
        "outcome_currency": "JPY",
        "engine_version": "outcome-engine-1.0.0",
        "label_version": None,
        "notes": None,
    }
    params.update(overrides)
    cur.execute(
        """
        select prod.close_episode_with_outcome(
          %(episode_id)s::uuid, %(primary_outcome)s::prod.episode_close_reason, %(closed_at)s,
          %(path_resolution)s::prod.path_resolution, %(granularity)s, %(resolved_session_index)s,
          %(resolved_at)s, %(primary_detail)s,
          %(counterfactual_path)s::prod.path_resolution, %(later_target_hit)s,
          %(later_target_hit_at)s, %(later_target_hit_session_index)s,
          %(mfe)s, %(mae)s, %(sessions_observed)s, %(corporate_action_ids)s,
          %(outcome_currency)s, %(engine_version)s, %(label_version)s, %(notes)s
        )
        """,
        params,
    )
    return cur.fetchone()[0]


def test_closing_an_episode_and_recording_its_outcome_is_one_call(conn):
    with conn.cursor() as cur:
        _, episode_id, _, _ = _entered(cur)
        _outcome(cur, episode_id)

        cur.execute(
            """
            select e.status::text, e.close_reason::text, o.primary_episode_outcome::text
            from prod.episodes e join prod.episode_outcomes o using (episode_id)
            where e.episode_id = %s
            """,
            (episode_id,),
        )
        assert cur.fetchone() == ("CLOSED", "TARGET_HIT", "TARGET_HIT")


def test_an_episode_cannot_be_closed_twice(conn):
    with conn.cursor() as cur:
        _, episode_id, _, _ = _entered(cur)
        _outcome(cur, episode_id)

        with pytest.raises(psycopg2.errors.RaiseException, match="closed once"):
            _outcome(cur, episode_id, primary_outcome="HORIZON_EXPIRED")


def test_an_unresolved_path_is_a_close_reason_of_its_own(conn):
    with conn.cursor() as cur:
        _, episode_id, _, _ = _entered(cur)
        _outcome(
            cur,
            episode_id,
            primary_outcome="AMBIGUOUS_PATH",
            path_resolution="AMBIGUOUS_PATH",
            granularity="INTRADAY_BAR",
            counterfactual_path="AMBIGUOUS_PATH",
        )

        cur.execute(
            "select outcome, episodes from ui.outcome_counts where outcome = 'AMBIGUOUS_PATH'"
        )
        assert cur.fetchone()[1] == 1


def test_the_results_view_shows_both_layers(conn):
    """A later target hit after an invalidated thesis has to be visible as what
    it is, and not as the answer."""

    with conn.cursor() as cur:
        _, episode_id, _, _ = _entered(cur)
        _outcome(
            cur,
            episode_id,
            primary_outcome="THESIS_INVALIDATED",
            path_resolution=None,
            granularity=None,
            later_target_hit=True,
            later_target_hit_session_index=12,
        )

        cur.execute(
            """
            select primary_outcome, later_target_hit, later_target_hit_session_index,
                   mfe, mae, sessions_observed, outcome_currency
            from ui.episode_results where episode_id = %s
            """,
            (episode_id,),
        )
        row = cur.fetchone()

        assert row[0] == "THESIS_INVALIDATED"
        assert row[1] is True
        assert row[2] == 12
        assert row[6] == "JPY"


def test_a_resolved_session_beyond_the_horizon_is_refused(conn):
    with conn.cursor() as cur:
        _, episode_id, _, _ = _entered(cur)
        with pytest.raises(psycopg2.errors.CheckViolation, match="resolved_session_within_horizon"):
            _outcome(cur, episode_id, resolved_session_index=21)
