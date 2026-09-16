"""Phase 1.1a database fixtures: privileges, historical tickers, issuer names.

These run against a real Postgres with the migrations applied. CI provides one;
locally they are skipped unless SURGE_TEST_DATABASE_URL is set.
"""

from __future__ import annotations

import os
import uuid
from datetime import timedelta

import pytest

psycopg2 = pytest.importorskip("psycopg2")

# Sibling module import: pytest puts this directory on sys.path, so the shared
# snapshot helpers are reachable regardless of where pytest was invoked from.
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

# Configuration and reference data. The runtime worker reads these and may never
# write them: the first one is the control that decides which hosts the database
# is allowed to fetch a snapshot from.
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


# ------------------------------------------------ historical ticker in as-of
def test_as_of_returns_the_ticker_that_was_current_then(conn):
    """A renamed listing must not show a future ticker beside a past state.

    ref.listings.local_code is the materialised current value; the as-of read
    takes the symbol from ref.listing_symbols at the same cutoff.
    """

    identity_key = f"US:CIK:{uuid.uuid4().hex[:8]}:COMMON_STOCK:"
    with conn.cursor() as cur:
        run1 = _new_run(cur, "US", T1)
        _add_snapshot_row(cur, run1, symbol="ABC", identity_key=identity_key, observed_at=T1)
        _apply(cur, run1)

        run2 = _new_run(cur, "US", T2)
        _add_snapshot_row(cur, run2, symbol="XYZ", identity_key=identity_key, observed_at=T2)
        _apply(cur, run2)

        cur.execute(
            "select listing_id, security_id from ref.listings where listing_identity_key = %s",
            (f"XNAS|{identity_key}",),
        )
        listing_id, security_id = cur.fetchone()

        cur.execute(
            "select symbol, security_id from ref.listings_as_of(%s) where listing_id = %s",
            (T1 + timedelta(hours=1), listing_id),
        )
        assert cur.fetchone() == ("ABC", security_id)

        cur.execute(
            "select symbol, security_id from ref.listings_as_of(%s) where listing_id = %s",
            (T2 + timedelta(hours=1), listing_id),
        )
        assert cur.fetchone() == ("XYZ", security_id)

        # the materialised row moved on; the as-of read did not follow it
        cur.execute("select local_code from ref.listings where listing_id = %s", (listing_id,))
        assert cur.fetchone()[0] == "XYZ"

        cur.execute(
            "select symbol from ref.listing_symbols_as_of(%s) where listing_id = %s",
            (T1 + timedelta(hours=1), listing_id),
        )
        assert cur.fetchone()[0] == "ABC"


# ----------------------------------------------------- issuer names from registry
def test_issuer_legal_name_is_the_registry_name_not_a_product_name(conn):
    identity_common = f"US:CIK:{uuid.uuid4().hex[:8]}:COMMON_STOCK:"
    identity_preferred = identity_common.replace("COMMON_STOCK", "PREFERRED")
    issuer_key = f"CIK:{uuid.uuid4().hex[:8]}"
    with conn.cursor() as cur:
        run = _new_run(cur, "US", T1)
        _add_snapshot_row(
            cur, run, symbol="AAA", identity_key=identity_common, observed_at=T1,
            name="Example Inc. - Common Stock", issuer_key=issuer_key, issuer_name="EXAMPLE INC",
        )
        _add_snapshot_row(
            cur, run, symbol="AAAP", identity_key=identity_preferred, observed_at=T1,
            name="Example Inc. 6% Series A Preferred", issuer_key=issuer_key, issuer_name="EXAMPLE INC",
        )
        _apply(cur, run)

        cur.execute(
            "select legal_name, name_source from ref.issuers where identity_key = %s", (issuer_key,)
        )
        assert cur.fetchone() == ("EXAMPLE INC", "sec_company_tickers")

        cur.execute(
            """
            select n.name, n.name_type
            from ref.issuer_names n
            join ref.issuers i on i.issuer_id = n.issuer_id
            where i.identity_key = %s and n.effective_to is null
            """,
            (issuer_key,),
        )
        assert cur.fetchall() == [("EXAMPLE INC", "LEGAL")]

        # the securities keep their own, different names
        cur.execute(
            """
            select count(distinct sn.name) from ref.security_names sn
            join ref.securities s on s.security_id = sn.security_id
            join ref.issuers i on i.issuer_id = s.issuer_id
            where i.identity_key = %s and sn.effective_to is null
            """,
            (issuer_key,),
        )
        assert cur.fetchone()[0] == 2


def test_issuer_without_a_registry_name_keeps_the_provider_name_as_an_alias(conn):
    identity_key = f"US:XNAS:SYMBOL:{uuid.uuid4().hex[:6]}"
    issuer_key = f"ISSUER-OF:{identity_key}"
    with conn.cursor() as cur:
        run = _new_run(cur, "US", T1)
        _add_snapshot_row(
            cur, run, symbol=identity_key[-6:], identity_key=identity_key, observed_at=T1,
            name="Nameless Fund ETF", issuer_key=issuer_key,
            issuer_source="PROVISIONAL_SECURITY_COORDINATE", issuer_confidence="PROVISIONAL",
            security_confidence="PROVISIONAL", issuer_name=None, cik=None,
        )
        _apply(cur, run)

        cur.execute(
            """
            select n.name_type, i.identity_confidence
            from ref.issuer_names n join ref.issuers i on i.issuer_id = n.issuer_id
            where i.identity_key = %s and n.effective_to is null
            """,
            (issuer_key,),
        )
        assert cur.fetchall() == [("ALIAS", "PROVISIONAL")]


def test_two_issuers_with_the_same_name_are_not_merged(conn):
    """The database keys issuers on the resolved identity, never on a name."""

    first = f"US:XNAS:SYMBOL:{uuid.uuid4().hex[:6]}"
    second = f"US:XNAS:SYMBOL:{uuid.uuid4().hex[:6]}"
    with conn.cursor() as cur:
        run = _new_run(cur, "US", T1)
        for key in (first, second):
            _add_snapshot_row(
                cur, run, symbol=key[-6:], identity_key=key, observed_at=T1,
                name="Acme Holdings Inc.", issuer_key=f"ISSUER-OF:{key}",
                issuer_source="PROVISIONAL_SECURITY_COORDINATE", issuer_confidence="PROVISIONAL",
                security_confidence="PROVISIONAL", issuer_name=None, cik=None,
            )
        _apply(cur, run)

        cur.execute(
            "select count(distinct issuer_id) from ref.issuers where identity_key in (%s, %s)",
            (f"ISSUER-OF:{first}", f"ISSUER-OF:{second}"),
        )
        assert cur.fetchone()[0] == 2


def test_identity_confidence_vocabulary_is_closed(conn):
    with conn.cursor() as cur:
        cur.execute(
            "select ref.identity_confidence_rank('STRONG'), "
            "ref.identity_confidence_rank('REGISTRY_ANCHORED'), "
            "ref.identity_confidence_rank('PROVISIONAL')"
        )
        assert cur.fetchone() == (3, 2, 1)

    with conn.cursor() as cur, pytest.raises(psycopg2.errors.CheckViolation):
        cur.execute(
            """
            insert into ref.issuers (issuer_id, country, legal_name, normalized_name, name_source,
                                     identity_source, identity_key, identity_confidence)
            values (gen_random_uuid(), 'US', 'x', 'x', 'test', 'TEST', %s, 'TOTALLY_SURE')
            """,
            (f"TEST:{uuid.uuid4()}",),
        )
    conn.rollback()


# ------------------------------------------------------------- coverage buckets
def test_coverage_separates_provider_errors_from_identity_warnings(conn):
    with conn.cursor() as cur:
        run = _new_run(cur, "US", T1)
        key = f"US:CIK:{uuid.uuid4().hex[:8]}:COMMON_STOCK:"
        _add_snapshot_row(cur, run, symbol="AAA", identity_key=key, observed_at=T1)

        cur.execute(
            """
            insert into pipeline.run_errors (run_id, stage, severity, error_type, message, context) values
              (%(run)s, 'provider_fetch', 'WARNING', 'PROVIDER_DATA', 'unmapped exchange letter', '{}'),
              (%(run)s, 'classify', 'WARNING', 'DATA_QUALITY', 'SIC listing truncated', '{}'),
              (%(run)s, 'resolve_identity', 'WARNING', 'IDENTITY_COLLISION', 'collision one',
               '{"identity_key": "US:CIK:1:ETF:", "symbol": "ONE"}'),
              (%(run)s, 'resolve_identity', 'WARNING', 'IDENTITY_COLLISION', 'collision two',
               '{"identity_key": "US:CIK:1:ETF:", "symbol": "TWO"}'),
              (%(run)s, 'resolve_identity', 'WARNING', 'IDENTITY_COLLISION', 'collision three',
               '{"identity_key": "US:CIK:2:ETN:", "symbol": "THREE"}')
            """,
            {"run": str(run)},
        )

        cur.execute(
            "select universe.compute_coverage(%s::uuid, 'universe-1.0.0', %s::date, '{}'::jsonb, 'test')",
            (str(run), T1.date()),
        )
        cur.execute(
            """
            select provider_error_count, data_quality_warning_count,
                   identity_collision_record_count, identity_collision_key_count
            from universe.coverage where run_id = %s and scope_kind = 'MARKET'
            """,
            (str(run),),
        )
        # one provider failure, one data quality warning, three collision records
        # over two distinct keys - never "five provider errors"
        assert cur.fetchone() == (1, 1, 3, 2)


# ------------------------------------------- the worker cannot rewrite its config
@pytest.mark.skipif(not WORKER_DSN, reason="SURGE_TEST_WORKER_DSN is not set")
def test_worker_cannot_write_the_load_host_allowlist():
    """The SSRF control must not be writable by the role it constrains."""

    worker = psycopg2.connect(WORKER_DSN)
    try:
        with worker.cursor() as cur:
            cur.execute("select count(*) from pipeline.load_host_allowlist")
            assert cur.fetchone()[0] >= 0
        worker.rollback()

        statements = (
            ("insert into pipeline.load_host_allowlist (host, note) values (%s, 'evil')", ("evil.example.net",)),
            ("update pipeline.load_host_allowlist set note = %s", ("evil",)),
            ("delete from pipeline.load_host_allowlist where host = %s", ("evil.example.net",)),
        )
        for statement, params in statements:
            with worker.cursor() as cur, pytest.raises(psycopg2.errors.InsufficientPrivilege):
                cur.execute(statement, params)
            worker.rollback()
    finally:
        worker.rollback()
        worker.close()


@pytest.mark.skipif(not WORKER_DSN, reason="SURGE_TEST_WORKER_DSN is not set")
def test_worker_cannot_write_configuration_tables():
    """Source registry, exchange list, universe definition, reason codes, id map."""

    worker = psycopg2.connect(WORKER_DSN)
    try:
        for table in CONFIG_TABLES:
            with worker.cursor() as cur:
                cur.execute(f"select count(*) from {table}")  # noqa: S608 - fixed table list
                assert cur.fetchone()[0] >= 0

                cur.execute(
                    """
                    select has_table_privilege(current_user, %(t)s, 'SELECT'),
                           has_table_privilege(current_user, %(t)s, 'INSERT'),
                           has_table_privilege(current_user, %(t)s, 'UPDATE'),
                           has_table_privilege(current_user, %(t)s, 'DELETE')
                    """,
                    {"t": table},
                )
                readable, *writable = cur.fetchone()
                assert readable, f"{table} must stay readable"
                assert not any(writable), f"{table} is writable by the runtime worker: {writable}"

                # and the write really is refused, not merely absent from a catalog
                with pytest.raises(psycopg2.errors.InsufficientPrivilege):
                    cur.execute(f"delete from {table} where false")  # noqa: S608
            worker.rollback()
    finally:
        worker.rollback()
        worker.close()


@pytest.mark.skipif(not WORKER_DSN, reason="SURGE_TEST_WORKER_DSN is not set")
def test_worker_can_still_write_its_own_output():
    """The tightening must not break the job: run output stays writable."""

    worker = psycopg2.connect(WORKER_DSN)
    try:
        with worker.cursor() as cur:
            cur.execute(
                """
                insert into pipeline.runs (run_id, job_name, job_version, run_mode, market_code,
                                           idempotency_key, status)
                values (gen_random_uuid(), 'privilege_probe', 'test', 'DEV', 'US', %s, 'RUNNING')
                returning run_id
                """,
                (f"privilege-probe:{uuid.uuid4()}",),
            )
            run_id = cur.fetchone()[0]
            cur.execute(
                """
                insert into pipeline.run_errors (run_id, stage, severity, error_type, message, context)
                values (%s, 'probe', 'WARNING', 'PROVIDER_DATA', 'probe', '{}')
                """,
                (run_id,),
            )
        worker.rollback()
    finally:
        worker.rollback()
        worker.close()
