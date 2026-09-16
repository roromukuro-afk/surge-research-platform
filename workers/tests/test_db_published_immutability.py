"""Phase 1.1c: a published run is frozen, and validation looks at its content.

These run against a real Postgres with the migrations applied. CI provides one;
locally they are skipped unless SURGE_TEST_DATABASE_URL is set.
"""

from __future__ import annotations

import os

import pytest

psycopg2 = pytest.importorskip("psycopg2")

from test_db_master_semantics import T1, T2  # noqa: E402
from test_db_publication_and_time import _publishable_run  # noqa: E402

DSN = os.environ.get("SURGE_TEST_DATABASE_URL")
WORKER_DSN = os.environ.get("SURGE_TEST_WORKER_DSN")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not DSN, reason="SURGE_TEST_DATABASE_URL is not set"),
]

UNIVERSE_VERSION = "universe-1.0.0"
IMMUTABLE = psycopg2.errors.ReadOnlySqlTransaction  # errcode raised by the freeze trigger


@pytest.fixture()
def conn():
    connection = psycopg2.connect(DSN)
    connection.autocommit = False
    try:
        yield connection
    finally:
        connection.rollback()
        connection.close()


# ------------------------------------------------------------- immutability
def test_published_run_artifacts_cannot_be_changed(conn):
    with conn.cursor() as cur:
        run = _publishable_run(cur, "US", T1, symbol="FRZ1")
        cur.execute("select pipeline.publish_run(%s::uuid)", (str(run),))

    attempts = (
        ("update evaluations", "update universe.evaluations set reason_code = 'TAMPERED' where run_id = %s"),
        ("update coverage", "update universe.coverage set included_count = 999 where run_id = %s"),
        ("update snapshot", "update pipeline.master_snapshot set name = 'TAMPERED' where run_id = %s"),
        ("delete snapshot", "delete from pipeline.master_snapshot where run_id = %s"),
        ("update run", "update pipeline.runs set status = 'FAILED' where run_id = %s"),
        ("delete run", "delete from pipeline.runs where run_id = %s"),
        ("update source fetch", "update pipeline.source_fetches set item_count = 0 where run_id = %s"),
    )
    for label, statement in attempts:
        with conn.cursor() as cur, pytest.raises(IMMUTABLE, match="immutable"):
            cur.execute(statement, (str(run),))
        conn.rollback()
        # re-publish the run in this fresh transaction for the next attempt
        with conn.cursor() as cur:
            run = _publishable_run(cur, "US", T1, symbol=f"FRZ{label[:3]}")
            cur.execute("select pipeline.publish_run(%s::uuid)", (str(run),))

    # inserting a new diagnostic against a published run is refused too
    with conn.cursor() as cur, pytest.raises(IMMUTABLE, match="immutable"):
        cur.execute(
            """
            insert into pipeline.run_errors (run_id, stage, severity, error_type, message, context)
            values (%s, 'late', 'WARNING', 'PROVIDER_DATA', 'after the fact', '{}')
            """,
            (str(run),),
        )
    conn.rollback()


def test_an_unpublished_run_is_still_writable(conn):
    with conn.cursor() as cur:
        run = _publishable_run(cur, "US", T2, symbol="OPEN1")

        cur.execute("update universe.evaluations set decision_detail = '{}'::jsonb where run_id = %s", (str(run),))
        cur.execute(
            """
            insert into pipeline.run_errors (run_id, stage, severity, error_type, message, context)
            values (%s, 'probe', 'WARNING', 'PROVIDER_DATA', 'still building', '{}')
            """,
            (str(run),),
        )
        cur.execute("update pipeline.runs set runner_id = 'probe' where run_id = %s", (str(run),))
        cur.execute("select count(*) from pipeline.run_errors where run_id = %s", (str(run),))
        assert cur.fetchone()[0] >= 1
    conn.rollback()


@pytest.mark.skipif(not WORKER_DSN, reason="SURGE_TEST_WORKER_DSN is not set")
def test_the_worker_cannot_change_a_published_run():
    """The role the job actually runs as, against a run it built itself."""

    owner = psycopg2.connect(DSN)
    owner.autocommit = True
    worker = psycopg2.connect(WORKER_DSN)
    worker.autocommit = False
    run = None
    try:
        with owner.cursor() as cur:
            run = _publishable_run(cur, "US", T1, symbol="WRK1")
            cur.execute("select pipeline.publish_run(%s::uuid)", (str(run),))

        for statement in (
            "update universe.evaluations set reason_code = 'TAMPERED' where run_id = %s",
            "update pipeline.master_snapshot set name = 'TAMPERED' where run_id = %s",
            "update pipeline.runs set status = 'FAILED' where run_id = %s",
            """insert into pipeline.run_errors (run_id, stage, severity, error_type, message, context)
               values (%s, 'late', 'WARNING', 'PROVIDER_DATA', 'after the fact', '{}')""",
        ):
            with worker.cursor() as cur, pytest.raises(
                (IMMUTABLE, psycopg2.errors.InsufficientPrivilege)
            ):
                cur.execute(statement, (str(run),))
            worker.rollback()
    finally:
        worker.rollback()
        worker.close()
        if run is not None:
            with owner.cursor() as cur:
                # the publication has to go before the artifacts can be cleaned up
                cur.execute("delete from pipeline.run_publications where run_id = %s", (str(run),))
                for table in (
                    "universe.evaluations", "universe.coverage", "pipeline.master_snapshot",
                    "pipeline.run_errors", "pipeline.source_fetches",
                ):
                    cur.execute(f"delete from {table} where run_id = %s", (str(run),))  # noqa: S608
                cur.execute("delete from pipeline.runs where run_id = %s", (str(run),))
        owner.close()


# ------------------------------------------------------- publication validation
def test_a_truncated_critical_source_blocks_publication(conn):
    """SPAC and REIT membership decide US classification: partial is not enough."""

    with conn.cursor() as cur:
        run = _publishable_run(cur, "US", T1, symbol="TRUNC")
        cur.execute(
            """
            insert into pipeline.run_errors (run_id, stage, severity, error_type, message, context)
            values (%s, 'classify', 'WARNING', 'CRITICAL_SOURCE_INCOMPLETE',
                    'SEC SIC listing truncated', '{"blocks_publication": true}')
            """,
            (str(run),),
        )
        cur.execute("select pipeline.validate_run(%s::uuid)", (str(run),))
        validation = cur.fetchone()[0]
        assert validation["ok"] is False
        assert validation["checks"]["critical_sources_complete"] is False

        with pytest.raises(psycopg2.errors.RaiseException):
            cur.execute("select pipeline.publish_run(%s::uuid)", (str(run),))
    conn.rollback()


@pytest.mark.parametrize(
    "label,statement,failing_check",
    [
        (
            "a snapshot row from another market",
            "update pipeline.master_snapshot set market_code = 'JP' where run_id = %s",
            "snapshot_market_consistent",
        ),
        (
            "a source data version that disagrees with the run",
            "update pipeline.runs set params = '{\"source_data_version\": \"other\"}'::jsonb where run_id = %s",
            "snapshot_data_version_matches_run",
        ),
        (
            "an identity version that disagrees with the run",
            "update pipeline.master_snapshot set identity_version = 'identity-other' where run_id = %s",
            "snapshot_identity_version_matches_run",
        ),
        (
            "a required source with no digest",
            "update pipeline.source_fetches set content_sha256 = null "
            "where run_id = %s and source_id = 'sec_company_tickers'",
            "required_sources_hashed",
        ),
        (
            "a data cutoff that predates a source",
            "update pipeline.runs set data_cutoff = data_cutoff - interval '1 day' where run_id = %s",
            "data_cutoff_covers_sources",
        ),
        (
            "a source fetch with no available_at",
            "update pipeline.source_fetches set available_at = null "
            "where run_id = %s and source_id = 'sec_sic_directory'",
            "source_available_at_complete",
        ),
    ],
)
def test_validation_rejects_inconsistent_content(conn, label, statement, failing_check):
    with conn.cursor() as cur:
        run = _publishable_run(cur, "US", T1, symbol="VAL1")
        cur.execute("select pipeline.validate_run(%s::uuid)", (str(run),))
        assert cur.fetchone()[0]["ok"] is True, f"the fixture must be publishable before {label}"

        cur.execute(statement, (str(run),))
        cur.execute("select pipeline.validate_run(%s::uuid)", (str(run),))
        validation = cur.fetchone()[0]
        assert validation["checks"][failing_check] is False, label
        assert validation["ok"] is False
    conn.rollback()


# -------------------------------------------------------- identifier provenance
def test_identifiers_carry_the_provenance_of_their_own_source(conn):
    """A CIK comes from the SEC file, not from whoever listed the ticker."""

    with conn.cursor() as cur:
        run = _publishable_run(cur, "US", T1, symbol="PROV1")

        cur.execute(
            """
            select id_type, source_id, source_record_id, observed_at, available_at
            from pipeline.snapshot_identifiers(%s::uuid)
            order by id_type
            """,
            (str(run),),
        )
        rows = {row[0]: row[1:] for row in cur.fetchall()}

        assert rows["TICKER"][0] == "nasdaq_trader_symbol_directory"
        assert rows["CIK"][0] == "sec_company_tickers"
        assert rows["CIK"][1] == "PROV1", "the SEC file is keyed by ticker"

        cur.execute(
            "select max(available_at) from pipeline.source_fetches where run_id = %s", (str(run),)
        )
        dependency_max = cur.fetchone()[0]
        for id_type, values in rows.items():
            assert values[3] == dependency_max, f"{id_type} must not be visible before its identity existed"
    conn.rollback()


def test_issuer_names_carry_the_registry_that_named_them(conn):
    with conn.cursor() as cur:
        run = _publishable_run(cur, "US", T1, symbol="PROV2")
        cur.execute(
            """
            select name_source, source_id, observed_at, available_at
            from pipeline.snapshot_issuers(%s::uuid)
            """,
            (str(run),),
        )
        name_source, source_id, observed_at, available_at = cur.fetchone()
        assert name_source == "sec_company_tickers"
        assert source_id == name_source

        cur.execute(
            """
            select observed_at from pipeline.source_fetches
            where run_id = %s and source_id = 'sec_company_tickers'
            """,
            (str(run),),
        )
        assert observed_at == cur.fetchone()[0]

        cur.execute("select max(available_at) from pipeline.source_fetches where run_id = %s", (str(run),))
        assert available_at == cur.fetchone()[0]
    conn.rollback()


def test_snapshot_rows_are_available_only_after_every_source(conn):
    """The row cannot be known before the last source it depends on arrived."""

    with conn.cursor() as cur:
        run = _publishable_run(cur, "US", T1, symbol="DEP1")
        # a second source arriving later than the primary one
        later = T2
        cur.execute(
            """
            update pipeline.source_fetches set available_at = %s
            where run_id = %s and source_id = 'sec_sic_directory'
            """,
            (later, str(run)),
        )
        cur.execute(
            "select distinct available_at from pipeline.snapshot_identifiers(%s::uuid)", (str(run),)
        )
        assert [row[0] for row in cur.fetchall()] == [later]
    conn.rollback()
