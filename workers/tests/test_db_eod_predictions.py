"""prod.eod_predictions: the EOD rules (D-267) held by the database itself.

Every case writes straight to the table, bypassing surge.entry.eod_prediction,
so each constraint is shown to hold for a path that does not come through the
Python. All writes roll back.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

psycopg2 = pytest.importorskip("psycopg2")
from psycopg2 import errors  # noqa: E402

DSN = os.environ.get("SURGE_TEST_DATABASE_URL")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not DSN, reason="SURGE_TEST_DATABASE_URL is not set"),
]

END = datetime(2026, 9, 17, 6, 30, tzinfo=UTC)
COLUMNS = (
    "security_id", "market_code", "thesis_key", "s0_session_date", "signal_reference_price",
    "signal_price_currency", "signal_price_jpy", "fx_rate", "fx_observed_at", "s0_session_closed_at",
    "close_fetched_at", "close_publication_delay_seconds", "close_provider", "close_feed", "close_basis",
    "data_cutoff", "decision_completed_at", "target_price", "initial_failure_line",
    "evaluation_start_session", "checkpoint_sessions", "horizon_sessions", "provider_id",
    "provider_kind", "rule_version", "universe_decision",
)


def _row(**overrides) -> dict:
    row = {
        "security_id": str(uuid.uuid4()),
        "market_code": "JP",
        "thesis_key": "t-1",
        "s0_session_date": END.date(),
        "signal_reference_price": Decimal("2950"),
        "signal_price_currency": "JPY",
        "signal_price_jpy": Decimal("2950"),
        "fx_rate": None,
        "fx_observed_at": None,
        "s0_session_closed_at": END,
        "close_fetched_at": END + timedelta(minutes=25),
        "close_publication_delay_seconds": 1200,
        "close_provider": "YAHOO",
        "close_feed": "yahoo-daily-chart",
        "close_basis": "YAHOO_DAILY_BAR_RAW_CLOSE",
        "data_cutoff": END + timedelta(minutes=30),
        "decision_completed_at": END + timedelta(minutes=40),
        "target_price": Decimal("3540"),
        "initial_failure_line": None,
        "evaluation_start_session": 1,
        "checkpoint_sessions": [1, 3, 5, 10, 20],
        "horizon_sessions": 20,
        "provider_id": "hosted-1",
        "provider_kind": "HOSTED_LLM",
        "rule_version": "eod-prediction-1.0.0",
        "universe_decision": "INCLUDED",
    }
    row.update(overrides)
    return row


def _insert(cur, row):
    placeholders = ", ".join(
        "%(checkpoint_sessions)s::smallint[]" if c == "checkpoint_sessions" else f"%({c})s" for c in COLUMNS
    )
    cur.execute(
        f"insert into prod.eod_predictions ({', '.join(COLUMNS)}) values ({placeholders}) "
        "returning eod_prediction_id",
        row,
    )
    return cur.fetchone()[0]


@pytest.fixture()
def cur():
    connection = psycopg2.connect(DSN)
    connection.autocommit = False
    try:
        with connection.cursor() as cursor:
            yield cursor
    finally:
        connection.rollback()
        connection.close()


def _refused(cur, row, error=errors.CheckViolation):
    cur.execute("savepoint s")
    with pytest.raises(error):
        _insert(cur, row)
    cur.execute("rollback to savepoint s")


def test_a_prediction_that_follows_the_rules_is_stored(cur):
    assert _insert(cur, _row())


def test_a_us_prediction_is_converted_with_a_rate_from_before_the_close(cur):
    us_end = datetime(2026, 9, 17, 20, 0, tzinfo=UTC)
    row = _row(
        market_code="US", signal_reference_price=Decimal("18.50"), signal_price_currency="USD",
        signal_price_jpy=Decimal("2779.625"), fx_rate=Decimal("150.25"),
        fx_observed_at=us_end - timedelta(hours=6), s0_session_closed_at=us_end,
        close_fetched_at=us_end + timedelta(minutes=20), close_publication_delay_seconds=960,
        close_provider="ALPACA", close_feed="sip", close_basis="CONSOLIDATED_SIP_REQUESTED",
        data_cutoff=us_end + timedelta(minutes=30), decision_completed_at=us_end + timedelta(minutes=40),
        target_price=Decimal("22.2"),
    )
    assert _insert(cur, row)

    _refused(cur, {**row, "fx_observed_at": us_end + timedelta(seconds=1)})
    _refused(cur, {**row, "fx_rate": None, "fx_observed_at": None})


def test_the_3000_yen_filter_is_on_the_s0_close(cur):
    assert _insert(cur, _row(signal_reference_price=Decimal("3000"), signal_price_jpy=Decimal("3000"),
                             target_price=Decimal("3600")))
    _refused(cur, _row(signal_reference_price=Decimal("3000.5"), signal_price_jpy=Decimal("3000.5"),
                       target_price=Decimal("3600.6")))


def test_the_target_is_the_close_times_one_point_two(cur):
    _refused(cur, _row(target_price=Decimal("3539")))


def test_a_close_read_before_the_end_plus_the_delay_is_not_a_reference(cur):
    _refused(cur, _row(close_fetched_at=END + timedelta(minutes=19)))


def test_the_prediction_comes_after_the_close_was_confirmed(cur):
    _refused(cur, _row(decision_completed_at=END + timedelta(minutes=24)))
    _refused(cur, _row(data_cutoff=END - timedelta(seconds=1)))


def test_evaluation_starts_at_s1_with_the_fixed_checkpoints_and_deadline(cur):
    _refused(cur, _row(evaluation_start_session=0))
    _refused(cur, _row(checkpoint_sessions=[1, 5, 20]))
    _refused(cur, _row(horizon_sessions=10))


def test_no_mock_no_unresolved_security_and_one_per_session(cur):
    _refused(cur, _row(provider_kind="DETERMINISTIC_MOCK"))
    _refused(cur, _row(universe_decision="UNRESOLVED"))
    row = _row()
    _insert(cur, row)
    _refused(cur, row, error=errors.UniqueViolation)


def test_a_failure_line_if_given_is_below_the_close(cur):
    assert _insert(cur, _row(initial_failure_line=Decimal("2800")))
    _refused(cur, _row(initial_failure_line=Decimal("2950")))


def test_a_prediction_cannot_be_revised_or_removed(cur):
    prediction_id = _insert(cur, _row())
    for statement in (
        "update prod.eod_predictions set target_price = 1 where eod_prediction_id = %s",
        "delete from prod.eod_predictions where eod_prediction_id = %s",
    ):
        cur.execute("savepoint s")
        with pytest.raises(psycopg2.Error):
            cur.execute(statement, (prediction_id,))
        cur.execute("rollback to savepoint s")
