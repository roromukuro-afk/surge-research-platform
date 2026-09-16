"""Phase 2.0 carry-forward: datasets, ruleset version, and recorded corrections.

These run against a real Postgres with the migrations applied. CI provides one;
locally they are skipped unless SURGE_TEST_DATABASE_URL is set.
"""

from __future__ import annotations

import os
import uuid

import pytest

psycopg2 = pytest.importorskip("psycopg2")

from test_db_master_semantics import T1, T2, _add_snapshot_row, _apply, _new_run  # noqa: E402
from test_db_publication_and_time import _publishable_run  # noqa: E402

DSN = os.environ.get("SURGE_TEST_DATABASE_URL")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not DSN, reason="SURGE_TEST_DATABASE_URL is not set"),
]

APPEND_ONLY = psycopg2.errors.ReadOnlySqlTransaction


@pytest.fixture()
def conn():
    connection = psycopg2.connect(DSN)
    connection.autocommit = False
    try:
        yield connection
    finally:
        connection.rollback()
        connection.close()


# ------------------------------------------------------------------ datasets
def test_the_two_sic_datasets_are_required_separately(conn):
    """One fetch of "sec_sic_directory" used to satisfy both SPAC and REIT."""

    with conn.cursor() as cur:
        run = _publishable_run(cur, "US", T1, symbol="DS1")
        cur.execute("select pipeline.validate_run(%s::uuid)", (str(run),))
        assert cur.fetchone()[0]["ok"] is True

        # the provider was fetched - but only one of its two datasets
        cur.execute(
            "delete from pipeline.source_fetches where run_id = %s and dataset_key = 'SEC_SIC_6798'",
            (str(run),),
        )
        cur.execute(
            "select count(*) from pipeline.source_fetches where run_id = %s and source_id = 'sec_sic_directory'",
            (str(run),),
        )
        assert cur.fetchone()[0] == 1, "the provider is still present"

        cur.execute("select pipeline.validate_run(%s::uuid)", (str(run),))
        validation = cur.fetchone()[0]
        assert validation["checks"]["required_sources_hashed"] is False
        assert validation["checks"]["missing_required_sources"] == ["SEC_SIC_6798"]
        assert validation["ok"] is False

        with pytest.raises(psycopg2.errors.RaiseException):
            cur.execute("select pipeline.publish_run(%s::uuid)", (str(run),))
    conn.rollback()


def test_a_fetch_without_a_dataset_key_is_read_from_its_endpoint(conn):
    """Runs published before the column existed are immutable, not rewritten."""

    with conn.cursor() as cur:
        cur.execute(
            """
            select pipeline.fetch_dataset_key('sec_sic_directory',
                                              'https://www.sec.gov/cgi-bin/browse-edgar?SIC=6770', null),
                   pipeline.fetch_dataset_key('sec_sic_directory',
                                              'https://www.sec.gov/cgi-bin/browse-edgar?SIC=6798', null),
                   pipeline.fetch_dataset_key('sec_company_tickers', 'https://www.sec.gov/files/x.json', null),
                   pipeline.fetch_dataset_key('sec_sic_directory', 'fixture://', 'SEC_SIC_6798')
            """
        )
        derived = cur.fetchone()
        assert derived == ("SEC_SIC_6770", "SEC_SIC_6798", "sec_company_tickers", "SEC_SIC_6798")


def test_the_published_runs_on_this_database_still_validate(conn):
    """The rule changed; the runs the platform currently depends on must survive it."""

    with conn.cursor() as cur:
        cur.execute(
            """
            select r.market_code, p.run_id
            from pipeline.run_publications p
            join pipeline.runs r using (run_id)
            join universe.current_eligibility c on c.run_id = p.run_id
            group by r.market_code, p.run_id
            """
        )
        authoritative = cur.fetchall()
        if not authoritative:
            pytest.skip("no authoritative run on this database")
        for market, run_id in authoritative:
            cur.execute("select pipeline.validate_run(%s::uuid)", (str(run_id),))
            validation = cur.fetchone()[0]
            assert validation["ok"] is True, f"{market} {run_id}: {validation['checks']}"


# ----------------------------------------------------------- ruleset version
def test_publication_records_the_ruleset_that_passed_it(conn):
    with conn.cursor() as cur:
        run = _publishable_run(cur, "US", T1, symbol="VER1")
        cur.execute("select pipeline.publish_run(%s::uuid)", (str(run),))
        published = cur.fetchone()[0]

        cur.execute("select pipeline.validation_ruleset_version()")
        current = cur.fetchone()[0]

        assert published["validation_version"] == current
        cur.execute(
            "select validation_version from pipeline.run_publications where run_id = %s", (str(run),)
        )
        assert cur.fetchone()[0] == current
        assert current != "publication-1.0.0", "the Phase 1.1c rules are not the 1.0.0 rules"
    conn.rollback()


def test_an_explicit_version_still_wins(conn):
    with conn.cursor() as cur:
        run = _publishable_run(cur, "US", T1, symbol="VER2")
        cur.execute("select pipeline.publish_run(%s::uuid, %s)", (str(run), "publication-test"))
        assert cur.fetchone()[0]["validation_version"] == "publication-test"
    conn.rollback()


# ------------------------------------------------- ingestion vs provenance run
def test_re_confirming_provenance_does_not_move_the_ingestion_run(conn):
    with conn.cursor() as cur:
        first = _new_run(cur, "US", T1)
        key = f"US:CIK:{uuid.uuid4().hex[:8]}:COMMON_STOCK:"
        _add_snapshot_row(cur, first, symbol="PRV1", identity_key=key, observed_at=T1)
        _apply(cur, first)

        cur.execute(
            """
            select identifier_id, ingestion_run_id, last_provenance_run_id
            from ref.security_identifiers si
            join ref.securities s using (security_id)
            where s.identity_key = %s and si.id_type = 'CIK'
            """,
            (key,),
        )
        identifier_id, ingestion_run, provenance_run = cur.fetchone()
        assert ingestion_run == first
        assert provenance_run == first

        # a second run re-observes the same value from a later fetch
        second = _new_run(cur, "US", T2)
        _add_snapshot_row(cur, second, symbol="PRV1", identity_key=key, observed_at=T2)
        _apply(cur, second)
        cur.execute("select ref.refresh_identifier_provenance(%s::uuid)", (str(second),))

        cur.execute(
            """
            select ingestion_run_id, last_provenance_run_id, observed_at
            from ref.security_identifiers where identifier_id = %s
            """,
            (identifier_id,),
        )
        ingestion_run, provenance_run, observed_at = cur.fetchone()
        assert ingestion_run == first, "the row was still created by the first run"
        assert provenance_run == second, "but the second run is what last confirmed it"
        assert observed_at == T2
    conn.rollback()


# ------------------------------------------------------ recorded corrections
def test_a_provenance_correction_is_recorded(conn):
    with conn.cursor() as cur:
        run = _new_run(cur, "US", T1)
        key = f"US:CIK:{uuid.uuid4().hex[:8]}:COMMON_STOCK:"
        _add_snapshot_row(cur, run, symbol="COR1", identity_key=key, observed_at=T1)
        _apply(cur, run)

        cur.execute(
            """
            select identifier_id from ref.security_identifiers si
            join ref.securities s using (security_id)
            where s.identity_key = %s and si.id_type = 'CIK'
            """,
            (key,),
        )
        identifier_id = cur.fetchone()[0]

        cur.execute(
            "update ref.security_identifiers set source_id = 'sec_company_tickers', "
            "last_provenance_run_id = %s where identifier_id = %s",
            (str(run), identifier_id),
        )
        cur.execute(
            """
            select changed_columns, before_value, after_value, run_id
            from ref.provenance_corrections
            where table_name = 'security_identifiers' and row_id = %s
            """,
            (identifier_id,),
        )
        rows = cur.fetchall()
        assert len(rows) == 1
        changed, before, after, correction_run = rows[0]
        assert changed == ["source_id"]
        assert after["source_id"] == "sec_company_tickers"
        assert before["source_id"] != after["source_id"]
        assert correction_run == run
    conn.rollback()


def test_re_confirmation_alone_is_not_a_correction(conn):
    """last_confirmed_at moving is re-observation, not a change to the record."""

    with conn.cursor() as cur:
        run = _new_run(cur, "US", T1)
        key = f"US:CIK:{uuid.uuid4().hex[:8]}:COMMON_STOCK:"
        _add_snapshot_row(cur, run, symbol="COR2", identity_key=key, observed_at=T1)
        _apply(cur, run)
        cur.execute("select count(*) from ref.provenance_corrections")
        before = cur.fetchone()[0]

        second = _new_run(cur, "US", T2)
        _add_snapshot_row(cur, second, symbol="COR2", identity_key=key, observed_at=T1)
        _apply(cur, second)  # same values: only last_confirmed_at moves

        cur.execute("select count(*) from ref.provenance_corrections")
        assert cur.fetchone()[0] == before
    conn.rollback()


def test_a_correction_record_cannot_be_rewritten(conn):
    for statement in (
        "update ref.provenance_corrections set after_value = '{}'::jsonb where correction_id = %s",
        "delete from ref.provenance_corrections where correction_id = %s",
    ):
        # a fresh record per attempt: each attempt ends in a rollback
        with conn.cursor() as cur:
            cur.execute(
                """
                insert into ref.provenance_corrections
                  (table_name, row_id, changed_columns, before_value, after_value)
                values ('security_identifiers', gen_random_uuid(), array['source_id'],
                        '{"source_id": "a"}'::jsonb, '{"source_id": "b"}'::jsonb)
                returning correction_id
                """
            )
            correction_id = cur.fetchone()[0]

            with pytest.raises(APPEND_ONLY, match="append-only"):
                cur.execute(statement, (correction_id,))
        conn.rollback()
