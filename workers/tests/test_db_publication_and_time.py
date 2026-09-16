"""Phase 1.1b database fixtures: publication, bitemporal reads, HTTP boundary.

These run against a real Postgres with the migrations applied. CI provides one;
locally they are skipped unless SURGE_TEST_DATABASE_URL is set.
"""

from __future__ import annotations

import os
import uuid
from datetime import timedelta

import pytest

psycopg2 = pytest.importorskip("psycopg2")

from test_db_master_semantics import (  # noqa: E402  - after the importorskip
    T1,
    T2,
    _add_snapshot_row,
    _apply,
    _new_run,
)

DSN = os.environ.get("SURGE_TEST_DATABASE_URL")
WORKER_DSN = os.environ.get("SURGE_TEST_WORKER_DSN")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not DSN, reason="SURGE_TEST_DATABASE_URL is not set"),
]

UNIVERSE_VERSION = "universe-1.0.0"


@pytest.fixture()
def conn():
    connection = psycopg2.connect(DSN)
    connection.autocommit = False
    try:
        yield connection
    finally:
        connection.rollback()
        connection.close()


def _publishable_run(cur, market: str, observed_at, *, symbol: str, status: str = "SUCCEEDED") -> uuid.UUID:
    """A run complete enough to be published: provenance, snapshot, coverage."""

    run_id = _new_run(cur, market, observed_at)
    cur.execute(
        """
        update pipeline.runs
        set status = %s,
            finished_at = %s,
            git_sha = 'deadbeef',
            config_hash = 'cfg-test',
            versions = '{"universe_version": "universe-1.0.0", "identity_version": "identity-test",
                         "job_version": "test"}'::jsonb,
            provider_bindings = '{"test_source": "fixture://"}'::jsonb
        where run_id = %s
        """,
        (status, observed_at, str(run_id)),
    )
    cur.execute(
        """
        insert into pipeline.source_fetches (run_id, source_id, endpoint, requested_at, received_at,
                                             http_status, item_count, bytes, observed_at, available_at)
        values (%s, 'nasdaq_trader_symbol_directory', 'fixture://', %s, %s, 200, 1, 1, %s, %s)
        """,
        (str(run_id), observed_at, observed_at, observed_at, observed_at),
    )
    key = f"US:CIK:{uuid.uuid4().hex[:8]}:COMMON_STOCK:"
    _add_snapshot_row(cur, run_id, symbol=symbol, identity_key=key, observed_at=observed_at)
    _apply(cur, run_id)
    cur.execute(
        "select universe.apply_snapshot_evaluations(%s::uuid, %s, %s::date)",
        (str(run_id), UNIVERSE_VERSION, observed_at.date()),
    )
    cur.execute(
        "select universe.compute_coverage(%s::uuid, %s, %s::date, '{}'::jsonb, 'test')",
        (str(run_id), UNIVERSE_VERSION, observed_at.date()),
    )
    return run_id


def _published_at(cur, run_id: uuid.UUID):
    cur.execute("select published_at from pipeline.run_publications where run_id = %s", (str(run_id),))
    return cur.fetchone()[0]


# --------------------------------------------------------------- publication
def test_an_unfinished_or_unattributable_run_cannot_be_published(conn):
    with conn.cursor() as cur:
        running = _publishable_run(cur, "US", T1, symbol="RUN1", status="RUNNING")
        with pytest.raises(psycopg2.errors.RaiseException):
            cur.execute("select pipeline.publish_run(%s::uuid)", (str(running),))
    conn.rollback()

    with conn.cursor() as cur:
        no_sha = _publishable_run(cur, "US", T1, symbol="RUN2")
        cur.execute("update pipeline.runs set git_sha = null where run_id = %s", (str(no_sha),))
        with pytest.raises(psycopg2.errors.RaiseException):
            cur.execute("select pipeline.publish_run(%s::uuid)", (str(no_sha),))
    conn.rollback()


def test_the_authoritative_run_is_the_published_one(conn):
    """A finished run is not the universe until someone publishes it."""

    with conn.cursor() as cur:
        run_a = _publishable_run(cur, "US", T1, symbol="AAA")
        cur.execute("select pipeline.publish_run(%s::uuid)", (str(run_a),))
        published_a = _published_at(cur, run_a)

        def authoritative(cutoff):
            cur.execute(
                "select universe.authoritative_run_at('US', %s, %s)", (UNIVERSE_VERSION, cutoff)
            )
            return cur.fetchone()[0]

        assert authoritative(published_a + timedelta(seconds=1)) == str(run_a)

        # T2: a run that is still going
        _publishable_run(cur, "US", T2, symbol="BBB", status="RUNNING")
        assert authoritative(published_a + timedelta(seconds=2)) == str(run_a)

        # T2: a run that failed
        _publishable_run(cur, "US", T2, symbol="CCC", status="FAILED")
        assert authoritative(published_a + timedelta(seconds=3)) == str(run_a)

        # T2: a run that succeeded but was never published
        run_d = _publishable_run(cur, "US", T2, symbol="DDD")
        assert authoritative(published_a + timedelta(seconds=4)) == str(run_a)

        # T3: it is published, and only then does it take over
        cur.execute("select pipeline.publish_run(%s::uuid)", (str(run_d),))
        published_d = _published_at(cur, run_d)
        assert authoritative(published_d - timedelta(microseconds=1)) == str(run_a)
        assert authoritative(published_d) == str(run_d)

        cur.execute(
            "select supersedes_run_id from pipeline.run_publications where run_id = %s", (str(run_d),)
        )
        assert cur.fetchone()[0] == str(run_a)


def test_eligibility_reads_follow_publication(conn):
    with conn.cursor() as cur:
        run_a = _publishable_run(cur, "US", T1, symbol="EEE")
        cur.execute("select pipeline.publish_run(%s::uuid)", (str(run_a),))
        published_a = _published_at(cur, run_a)

        run_b = _publishable_run(cur, "US", T2, symbol="FFF")
        cur.execute("select pipeline.publish_run(%s::uuid)", (str(run_b),))
        published_b = _published_at(cur, run_b)

        cur.execute(
            "select distinct run_id from universe.eligibility_as_of('US', %s, %s)",
            (UNIVERSE_VERSION, published_a + timedelta(microseconds=1)),
        )
        assert [row[0] for row in cur.fetchall()] == [str(run_a)]

        cur.execute(
            "select distinct run_id from universe.eligibility_as_of('US', %s, %s)",
            (UNIVERSE_VERSION, published_b),
        )
        assert [row[0] for row in cur.fetchall()] == [str(run_b)]


# ------------------------------------------------- knowledge vs effective time
def test_as_of_does_not_return_a_change_before_it_takes_effect(conn):
    """Known at T1, effective at T2: a T1 reader must still see the old ticker."""

    identity_key = f"US:CIK:{uuid.uuid4().hex[:8]}:COMMON_STOCK:"
    with conn.cursor() as cur:
        run = _new_run(cur, "US", T1)
        _add_snapshot_row(cur, run, symbol="ABC", identity_key=identity_key, observed_at=T1)
        _apply(cur, run)

        cur.execute(
            "select listing_id from ref.listings where listing_identity_key = %s",
            (f"XNAS|{identity_key}",),
        )
        listing_id = cur.fetchone()[0]

        # the provider tells us at T1 that the ticker becomes XYZ at T2
        cur.execute(
            "update ref.listing_symbols set effective_to = %s where listing_id = %s and effective_to is null",
            (T2, listing_id),
        )
        cur.execute(
            """
            insert into ref.listing_symbols (listing_id, symbol, symbol_type, effective_from,
                                             observed_at, available_at, last_confirmed_at,
                                             source_id, ingestion_run_id)
            values (%s, 'XYZ', 'TICKER', %s, %s, %s, %s, 'nasdaq_trader_symbol_directory', %s)
            """,
            (listing_id, T2, T1, T1, T1, str(run)),
        )

        def symbol_at(effective_at, known_at=None):
            if known_at is None:
                cur.execute(
                    "select symbol from ref.listings_as_of(%s) where listing_id = %s",
                    (effective_at, listing_id),
                )
            else:
                cur.execute(
                    "select symbol from ref.listings_as_of(%s, %s) where listing_id = %s",
                    (effective_at, known_at, listing_id),
                )
            row = cur.fetchone()
            return row[0] if row else None

        # half way between: the change is known but not yet in force
        assert symbol_at(T1 + timedelta(hours=1)) == "ABC"
        # after it takes effect
        assert symbol_at(T2 + timedelta(hours=1)) == "XYZ"
        # and the two dimensions are independent: effective later, known at T1
        assert symbol_at(T2 + timedelta(hours=1), known_at=T1) == "XYZ"

        # a reader who did not yet know anything sees no ticker for that listing
        cur.execute(
            "select count(*) from ref.listing_symbols_as_of(%s, %s) where listing_id = %s",
            (T2 + timedelta(hours=1), T1 - timedelta(hours=1), listing_id),
        )
        assert cur.fetchone()[0] == 0


# ----------------------------------------------------- privilege boundaries
def test_a_new_function_is_not_executable_by_public(conn):
    """The default privilege, not a per-function revoke, is what holds."""

    name = f"tmp_probe_{uuid.uuid4().hex[:8]}"
    with conn.cursor() as cur:
        cur.execute(f"create function ref.{name}() returns integer language sql as $$ select 1 $$")
        cur.execute(
            "select has_function_privilege('public', %s, 'EXECUTE')", (f"ref.{name}()",)
        )
        assert cur.fetchone()[0] is False
        cur.execute(f"drop function ref.{name}()")
    conn.rollback()


@pytest.mark.skipif(not WORKER_DSN, reason="SURGE_TEST_WORKER_DSN is not set")
def test_worker_cannot_make_http_requests_directly():
    """The allowlist is only a control if the worker cannot go around it."""

    worker = psycopg2.connect(WORKER_DSN)
    try:
        with worker.cursor() as cur:
            cur.execute("select count(*) from pg_namespace where nspname = 'extensions'")
            if cur.fetchone()[0] == 0:
                pytest.skip("no extensions schema in this database (vanilla Postgres)")

            cur.execute("select has_schema_privilege(current_user, 'extensions', 'USAGE')")
            assert cur.fetchone()[0] is False, "the worker can reach the HTTP client directly"
        worker.rollback()

        for statement in (
            "select extensions.http_get('https://example.com')",
            "select extensions.http_set_curlopt('CURLOPT_TIMEOUT', '1')",
        ):
            with worker.cursor() as cur, pytest.raises(psycopg2.errors.InsufficientPrivilege):
                cur.execute(statement)
            worker.rollback()
    finally:
        worker.rollback()
        worker.close()


@pytest.mark.skipif(not WORKER_DSN, reason="SURGE_TEST_WORKER_DSN is not set")
def test_worker_still_reaches_the_loader_through_the_allowlist():
    """Closing the direct path must not close the legitimate one."""

    worker = psycopg2.connect(WORKER_DSN)
    try:
        with worker.cursor() as cur:
            cur.execute(
                "select count(*) from pg_proc p join pg_namespace n on n.oid = p.pronamespace "
                "where n.nspname = 'pipeline' and p.proname = 'load_master_snapshot_from_signed_url'"
            )
            if cur.fetchone()[0] == 0:
                pytest.skip("the http loader is not installed in this database")

            cur.execute(
                "select has_function_privilege(current_user, "
                "'pipeline.load_master_snapshot_from_signed_url(uuid,text,integer)', 'EXECUTE')"
            )
            assert cur.fetchone()[0] is True

            # reachable, and refused by the allowlist rather than by privileges
            with pytest.raises(psycopg2.errors.RaiseException, match="host not allowed"):
                cur.execute(
                    "select pipeline.load_master_snapshot_from_signed_url("
                    "'00000000-0000-0000-0000-000000000000'::uuid, 'https://evil.example.net/x?token=1')"
                )
        worker.rollback()
    finally:
        worker.rollback()
        worker.close()
