"""RF-15: production / research separation, enforced by the database.

Runs only when a database is reachable (SURGE_TEST_DATABASE_URL). CI provides a
Postgres service and applies the migrations before running these.
"""

from __future__ import annotations

import os

import pytest

psycopg2 = pytest.importorskip("psycopg2")

DSN = os.environ.get("SURGE_TEST_DATABASE_URL")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not DSN, reason="SURGE_TEST_DATABASE_URL is not set"),
]


@pytest.fixture()
def connection():
    conn = psycopg2.connect(DSN)
    conn.autocommit = False
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()


def test_research_role_cannot_write_to_prod(connection):
    with connection.cursor() as cur:
        cur.execute("set local role surge_worker_research")
        with pytest.raises(psycopg2.errors.InsufficientPrivilege):
            cur.execute("insert into prod.access_guard (guard_id, note) values ('x', 'should fail')")


def test_research_role_cannot_create_objects_in_prod(connection):
    with connection.cursor() as cur:
        cur.execute("set local role surge_worker_research")
        with pytest.raises(psycopg2.errors.InsufficientPrivilege):
            cur.execute("create table prod.should_not_exist (id int)")


def test_research_role_can_write_research_schema(connection):
    with connection.cursor() as cur:
        cur.execute("set local role surge_worker_research")
        cur.execute("insert into research.access_guard (guard_id, note) values ('rf15', 'ok')")
        cur.execute("select count(*) from research.access_guard where guard_id = 'rf15'")
        assert cur.fetchone()[0] == 1


def test_research_role_is_read_only_on_master(connection):
    with connection.cursor() as cur:
        cur.execute("set local role surge_worker_research")
        cur.execute("select count(*) from ref.exchanges")
        assert cur.fetchone()[0] >= 0
        with pytest.raises(psycopg2.errors.InsufficientPrivilege):
            cur.execute(
                "insert into ref.exchanges (exchange_id, market_code, name, country, timezone) "
                "values ('XTST', 'JP', 'test', 'JP', 'Asia/Tokyo')"
            )


def test_history_tables_reject_deletes_for_application_roles(connection):
    """Past tickers and delistings must not be removable by the workers."""

    with connection.cursor() as cur:
        cur.execute("set local role surge_worker_prod")
        with pytest.raises(psycopg2.errors.InsufficientPrivilege):
            cur.execute("delete from ref.listing_symbols")
