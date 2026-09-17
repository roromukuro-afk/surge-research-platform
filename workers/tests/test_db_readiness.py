"""The readiness report, run against a real database.

The point of these is narrow and worth stating: they prove the queries execute
and return the shape the assessment expects. A readiness report built from
queries nobody has run is a report about its own defaults, which is what this
was before it connected.

They assert almost nothing about the *values*, because the values are whatever
the database happens to hold - and a test that pinned them would start failing
the day the system began working.
"""

from __future__ import annotations

import os

import pytest

psycopg2 = pytest.importorskip("psycopg2")

from surge.runtime.readiness import (  # noqa: E402
    SQL_CHECKS,
    CheckStatus,
    Liveness,
    collect,
)

DSN = os.environ.get("SURGE_TEST_DATABASE_URL")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not DSN, reason="SURGE_TEST_DATABASE_URL is not set"),
]


@pytest.fixture()
def conn():
    connection = psycopg2.connect(DSN)
    connection.autocommit = False
    try:
        yield connection
    finally:
        # Read-only by intent; rolled back so it is read-only in fact.
        connection.rollback()
        connection.close()


def test_every_readiness_query_actually_runs(conn):
    """One failing query used to be invisible: the check simply reported False,
    which is indistinguishable from a real negative answer."""

    failures = []
    with conn.cursor() as cur:
        for name, sql in SQL_CHECKS.items():
            try:
                cur.execute(sql, {"market": "JP", "universe_version": "universe-1.0.0"})
                cur.fetchone()
            except Exception as exc:  # noqa: BLE001 - reported, not swallowed
                conn.rollback()
                failures.append(f"{name}: {type(exc).__name__}: {exc}")

    assert failures == []


@pytest.mark.parametrize("market", ["JP", "US"])
def test_collect_produces_a_verdict_for_a_real_market(conn, market):
    readiness = collect(conn, market)

    assert readiness.market_code == market
    assert readiness.verdict
    # Every check carries a reason. "price provider: false" is not a finding.
    assert all(check.detail for check in readiness.checks)
    assert not [c for c in readiness.checks if c.name == "database_check_failed"]


def test_the_teacher_tables_are_read_and_are_empty(conn):
    """The 0-start, asserted against the real database rather than assumed.

    If this ever fails, something synthetic has been written into the production
    teacher tables and will be indistinguishable from real data later."""

    readiness = collect(conn, "JP")
    check = next(c for c in readiness.checks if c.name == "teacher_rows_still_zero")

    assert check.status is CheckStatus.PASS


def test_a_binding_without_rows_is_reported_as_bound_and_not_observed(conn):
    """Today FX_USDJPY is bound to the ECB and market.fx_rates is empty. When
    that stops being true this test still passes - it asserts the distinction
    exists, not which side of it the system is on."""

    readiness = collect(conn, "US")
    check = next(c for c in readiness.checks if c.name == "fx_provider")

    assert check.liveness in (
        Liveness.NOT_BOUND,
        Liveness.BOUND_NOT_LIVE_OBSERVED,
        Liveness.LIVE_OBSERVED,
    )
    if check.liveness is Liveness.BOUND_NOT_LIVE_OBSERVED:
        assert "not yet live observed" in check.detail
        assert check.status is CheckStatus.FAIL


def test_a_caller_flag_cannot_overrule_a_measured_row_count(conn):
    """The database answers what it can answer. A command line saying a price
    source is settled does not make bars appear."""

    readiness = collect(conn, "US", eod_price_provider="somebody's claim")
    check = next(c for c in readiness.checks if c.name == "eod_price_provider")

    if check.liveness is Liveness.BOUND_NOT_LIVE_OBSERVED:
        assert check.status is CheckStatus.FAIL
        assert "holds no rows" in check.detail
