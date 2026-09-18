"""Does the database contain what the migrations say?

Worth being exact about what this proves *here*. Continuous integration builds
its Postgres by applying these same files to an empty database, so in CI the
comparison is very nearly a tautology: the only thing it can catch is a bug in
the parsing, where a definition the files contain is not the one this module
extracts. That is not nothing - it caught exactly that on 2026-09-18, where the
newest definition of ``pipeline.snapshot_securities`` used ``create function``
rather than ``create or replace function`` and so was invisible - but it is not
the drift the rule exists to prevent.

The drift that matters happens in the cloud database, which CI never opens. That
is what ``python -m surge.jobs.schema_drift_cli`` is for, and running it found
ten functions whose comments had been stripped on the way in. The comparison
itself lives in ``surge.runtime.schema_drift`` so that both callers run the same
code. The parsing is covered on its own in ``test_schema_drift.py``, which
needs no database and so runs in the unit job too.
"""

from __future__ import annotations

import os

import pytest

psycopg2 = pytest.importorskip("psycopg2")

from surge.runtime.schema_drift import (  # noqa: E402
    PROJECT_SCHEMAS,
    assert_scope_matches_the_database,
    compare,
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
        connection.rollback()
        connection.close()


def test_every_function_in_the_database_matches_its_migration(conn):
    report = compare(conn)

    assert report.drifted == (), (
        "these functions differ between supabase/migrations and the database: "
        + ", ".join(report.drifted)
        + ". The migrations are the source of truth, so the database is what has to change"
    )


def test_no_function_exists_that_no_migration_defines(conn):
    """The other direction. A hand-made function survives every body comparison
    by never being compared to anything."""

    report = compare(conn)

    assert report.unmanaged == (), (
        "these functions exist in the database and in no migration: "
        + ", ".join(report.unmanaged)
        + ". Either add the migration that creates them or drop them"
    )


def test_the_comparison_examined_something(conn):
    """`drifted == ()` is also what a query that returned no rows looks like."""

    report = compare(conn)

    assert report.examined > 60
    assert report.in_sync


def test_this_module_and_the_database_agree_on_what_the_project_is(conn):
    """Everything else here is scoped by PROJECT_SCHEMAS, so a schema missing
    from it is a schema nothing checks - and the failure is silent, because a
    narrower scope produces a shorter list of problems rather than an error.

    A hand-kept copy of the list left out screening, material and chart. Four
    live functions were never compared with anything and one of them had
    drifted."""

    assert_scope_matches_the_database(conn)


def test_the_schemas_that_were_missing_are_in_scope(conn):
    """Named rather than implied, so removing one fails here and not silently."""

    assert {"screening", "material", "chart"} <= set(PROJECT_SCHEMAS)
    assert "storage" not in PROJECT_SCHEMAS  # Supabase's, not the project's

    report = compare(conn)
    assert report.examined > 80
