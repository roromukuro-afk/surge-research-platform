"""Database level fixtures from the Phase 1.1 audit (C, E, F, G, H-adjacent, I).

These run against a real Postgres with the migrations applied. CI provides one;
locally they are skipped unless SURGE_TEST_DATABASE_URL is set.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest

psycopg2 = pytest.importorskip("psycopg2")

DSN = os.environ.get("SURGE_TEST_DATABASE_URL")
WORKER_DSN = os.environ.get("SURGE_TEST_WORKER_DSN")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not DSN, reason="SURGE_TEST_DATABASE_URL is not set"),
]

T1 = datetime(2026, 9, 10, 6, 0, tzinfo=UTC)
T2 = datetime(2026, 9, 11, 6, 0, tzinfo=UTC)

SNAPSHOT_COLUMNS = (
    "run_id, source_id, source_record_id, market_code, exchange_id, local_code, symbol, "
    "name, normalized_name, security_type, market_segment_code, market_segment_name, "
    "currency, country, is_adr, is_test_issue, listing_status, cik, "
    "issuer_identity_source, issuer_identity_key, issuer_identity_confidence, "
    "security_identity_source, security_identity_key, security_identity_confidence, "
    "decision, reason_code, observed_at, available_at, source_data_version, "
    "issuer_name, issuer_name_source, identity_version"
)

CONFIG_TABLES = (
    "pipeline.load_host_allowlist",
    "pipeline.sources",
    "ref.exchanges",
    "universe.definitions",
    "universe.decision_reasons",
    "ref.identity_migration_map",
)


@pytest.fixture()
def conn():
    connection = psycopg2.connect(DSN)
    connection.autocommit = False
    try:
        yield connection
    finally:
        connection.rollback()
        connection.close()


def _new_run(cur, market: str, observed_at: datetime) -> uuid.UUID:
    run_id = uuid.uuid4()
    cur.execute(
        """
        insert into pipeline.runs (run_id, job_name, job_version, run_mode, market_code,
                                   idempotency_key, status, as_of_date, data_cutoff)
        values (%s, 'test_universe_sync', 'test', 'DEV', %s, %s, 'RUNNING', %s, %s)
        """,
        (str(run_id), market, f"test:{run_id}", observed_at.date(), observed_at),
    )
    return run_id


def _add_snapshot_row(
    cur,
    run_id: uuid.UUID,
    *,
    symbol: str,
    identity_key: str,
    observed_at: datetime,
    name: str = "Example Inc. Common Stock",
    exchange_id: str | None = "XNAS",
    segment: str = "Q",
    status: str = "LISTED",
    decision: str = "INCLUDED",
    reason: str = "TARGET_MARKET_COMMON_STOCK",
    issuer_key: str = "CIK:0000000001",
    issuer_source: str = "SEC_CIK",
    issuer_confidence: str = "STRONG",
    security_confidence: str = "REGISTRY_ANCHORED",
    issuer_name: str | None = "Example Registrant Inc.",
    cik: str | None = "0000000001",
) -> None:
    cur.execute(
        f"""
        insert into pipeline.master_snapshot ({SNAPSHOT_COLUMNS})
        values (%s, 'nasdaq_trader_symbol_directory', %s, 'US', %s, %s, %s,
                %s, %s, 'COMMON_STOCK', %s, null,
                'USD', 'US', false, false, %s, %s,
                %s, %s, %s,
                'SEC_CIK', %s, %s,
                %s, %s, %s, %s, 'test-version',
                %s, %s, 'identity-test')
        """,
        (
            str(run_id), f"rec:{symbol}", exchange_id, symbol, symbol,
            name, name.lower(), segment,
            status, cik,
            issuer_source, issuer_key, issuer_confidence,
            identity_key, security_confidence,
            decision, reason, observed_at, observed_at,
            issuer_name, "sec_company_tickers" if issuer_name else None,
        ),
    )


def _apply(cur, run_id: uuid.UUID) -> dict:
    cur.execute("select ref.apply_master_snapshot(%s::uuid)", (str(run_id),))
    return cur.fetchone()[0]


# ------------------------------------------------------------------ fixture C
def test_ticker_change_keeps_identity_and_records_history(conn):
    identity_key = f"US:CIK:{uuid.uuid4().hex[:8]}:COMMON_STOCK:"
    with conn.cursor() as cur:
        run1 = _new_run(cur, "US", T1)
        _add_snapshot_row(cur, run1, symbol="ABC", identity_key=identity_key, observed_at=T1)
        _apply(cur, run1)

        cur.execute(
            "select security_id, listing_id from ref.listings where listing_identity_key = %s",
            (f"XNAS|{identity_key}",),
        )
        security_before, listing_before = cur.fetchone()

        run2 = _new_run(cur, "US", T2)
        _add_snapshot_row(cur, run2, symbol="XYZ", identity_key=identity_key, observed_at=T2)
        _apply(cur, run2)

        cur.execute(
            "select security_id, listing_id from ref.listings where listing_identity_key = %s",
            (f"XNAS|{identity_key}",),
        )
        security_after, listing_after = cur.fetchone()

        assert security_before == security_after
        assert listing_before == listing_after

        cur.execute(
            """
            select symbol, effective_to is null as is_open
            from ref.listing_symbols
            where listing_id = %s and symbol_type = 'TICKER'
            order by effective_from
            """,
            (listing_after,),
        )
        history = cur.fetchall()
        assert history == [("ABC", False), ("XYZ", True)]


# ------------------------------------------------------------------ fixture E
def test_unchanged_snapshot_does_not_grow_history(conn):
    identity_key = f"US:CIK:{uuid.uuid4().hex[:8]}:COMMON_STOCK:"
    with conn.cursor() as cur:
        for observed_at in (T1, T1 + timedelta(days=1), T1 + timedelta(days=2)):
            run = _new_run(cur, "US", observed_at)
            _add_snapshot_row(cur, run, symbol="SAME", identity_key=identity_key, observed_at=observed_at)
            _apply(cur, run)

        cur.execute(
            "select listing_id from ref.listings where listing_identity_key = %s",
            (f"XNAS|{identity_key}",),
        )
        listing_id = cur.fetchone()[0]

        cur.execute("select count(*) from ref.listing_symbols where listing_id = %s", (listing_id,))
        assert cur.fetchone()[0] == 1

        cur.execute("select count(*) from ref.listing_states where listing_id = %s", (listing_id,))
        assert cur.fetchone()[0] == 1

        cur.execute(
            "select last_confirmed_at from ref.listing_symbols where listing_id = %s", (listing_id,)
        )
        assert cur.fetchone()[0] == T1 + timedelta(days=2)

        cur.execute(
            """
            select count(*) from ref.security_names sn
            join ref.securities s on s.security_id = sn.security_id
            where s.identity_key = %s
            """,
            (identity_key,),
        )
        assert cur.fetchone()[0] == 1


# ------------------------------------------------------------------ fixture F
def test_as_of_returns_the_state_known_at_that_time(conn):
    identity_key = f"US:CIK:{uuid.uuid4().hex[:8]}:COMMON_STOCK:"
    with conn.cursor() as cur:
        run1 = _new_run(cur, "US", T1)
        _add_snapshot_row(cur, run1, symbol="AAA", identity_key=identity_key, observed_at=T1, segment="G")
        _apply(cur, run1)

        run2 = _new_run(cur, "US", T2)
        _add_snapshot_row(
            cur, run2, symbol="AAA", identity_key=identity_key, observed_at=T2,
            segment="S", status="SUSPENDED",
        )
        _apply(cur, run2)

        cur.execute(
            "select listing_id from ref.listings where listing_identity_key = %s",
            (f"XNAS|{identity_key}",),
        )
        listing_id = cur.fetchone()[0]

        between = T1 + timedelta(hours=1)
        cur.execute(
            "select market_segment_code, listing_status from ref.listings_as_of(%s) where listing_id = %s",
            (between, listing_id),
        )
        assert cur.fetchone() == ("G", "LISTED")

        after = T2 + timedelta(hours=1)
        cur.execute(
            "select market_segment_code, listing_status from ref.listings_as_of(%s) where listing_id = %s",
            (after, listing_id),
        )
        assert cur.fetchone() == ("S", "SUSPENDED")

        # the earlier state row still exists, it is only bounded
        cur.execute(
            "select count(*) from ref.listing_states where listing_id = %s and market_segment_code = 'G'",
            (listing_id,),
        )
        assert cur.fetchone()[0] == 1


# ------------------------------------------------------------------ fixture G
def test_evaluations_with_unresolved_listing_do_not_duplicate(conn):
    key_one = f"US:XNAS:SYMBOL:{uuid.uuid4().hex[:6]}"
    key_two = f"US:XNAS:SYMBOL:{uuid.uuid4().hex[:6]}"
    with conn.cursor() as cur:
        run = _new_run(cur, "US", T1)
        for key in (key_one, key_two):
            _add_snapshot_row(
                cur, run, symbol=key[-6:], identity_key=key, observed_at=T1,
                exchange_id=None, decision="UNRESOLVED", reason="PROVIDER_DATA_MISSING",
            )
        _apply(cur, run)

        cur.execute(
            "select universe.apply_snapshot_evaluations(%s::uuid, 'universe-1.0.0', %s::date)",
            (str(run), T1.date()),
        )
        first = cur.fetchone()[0]
        cur.execute(
            "select universe.apply_snapshot_evaluations(%s::uuid, 'universe-1.0.0', %s::date)",
            (str(run), T1.date()),
        )
        second = cur.fetchone()[0]

        assert first == 2
        assert second == 0

        cur.execute(
            "select count(*) from universe.evaluations where run_id = %s and listing_id is null",
            (str(run),),
        )
        assert cur.fetchone()[0] == 2


# ----------------------------------------------------------- loader hardening
def test_loader_url_validation_rejects_unsafe_urls(conn):
    with conn.cursor() as cur:
        cur.execute("insert into pipeline.load_host_allowlist (host, note) values ('storage.example.com', 'test')")

        cur.execute("select pipeline.assert_load_url_allowed('https://storage.example.com/o/x?token=abc')")
        assert cur.fetchone()[0] == "storage.example.com"

        for bad_url in (
            "http://storage.example.com/o/x?token=abc",  # not https
            "https://evil.example.net/o/x?token=abc",  # host not allowed
            "https://storage.example.com/o/x",  # not signed
        ):
            with pytest.raises(psycopg2.errors.RaiseException):
                cur.execute("select pipeline.assert_load_url_allowed(%s)", (bad_url,))
            conn.rollback()
            cur.execute("insert into pipeline.load_host_allowlist (host, note) values ('storage.example.com', 'test')")


# ------------------------------------------------------------------ fixture I
@pytest.mark.skipif(not WORKER_DSN, reason="SURGE_TEST_WORKER_DSN is not set")
def test_production_worker_principal_has_least_privilege():
    """The worker connects as itself, not as the database owner."""

    worker = psycopg2.connect(WORKER_DSN)
    worker.autocommit = False
    try:
        with worker.cursor() as cur:
            cur.execute("select current_user, pg_has_role(current_user, 'surge_worker_prod', 'member')")
            user, is_member = cur.fetchone()
            assert user != "postgres"
            assert is_member

            cur.execute("select count(*) from ref.exchanges")
            assert cur.fetchone()[0] >= 0

            cur.execute(
                """
                insert into pipeline.runs (run_id, job_name, job_version, run_mode, market_code,
                                           idempotency_key, status)
                values (gen_random_uuid(), 'principal_test', 'test', 'DEV', 'US', %s, 'RUNNING')
                """,
                (f"principal-test:{uuid.uuid4()}",),
            )

            with pytest.raises(psycopg2.errors.InsufficientPrivilege):
                cur.execute("delete from ref.listing_symbols where false")
            worker.rollback()

        with worker.cursor() as cur:
            with pytest.raises(psycopg2.errors.InsufficientPrivilege):
                cur.execute("insert into research.access_guard (guard_id, note) values ('x', 'y')")
            worker.rollback()
    finally:
        worker.rollback()
        worker.close()
