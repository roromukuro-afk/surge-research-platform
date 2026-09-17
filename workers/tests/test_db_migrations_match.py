"""Does the database actually contain what the migrations say?

`supabase/migrations/` is the single source of truth for the schema (CLAUDE.md
§4). That is a rule about intent, and intent is not self-enforcing: a function
applied from a paste rather than from the file leaves the two agreeing on
behaviour and disagreeing on text, and the next person to read the file is
reading something the database is not running.

This caught a real drift on 2026-09-17: two functions had been applied with
their explanatory comments stripped, so the file explained reasoning the
database did not carry.

Compares whitespace-normalised bodies, because formatting is not the point and
Postgres does not preserve it exactly. Names not present in any migration file
are ignored, so this does not object to functions created by extensions.
"""

from __future__ import annotations

import os
import pathlib
import re

import pytest

psycopg2 = pytest.importorskip("psycopg2")

DSN = os.environ.get("SURGE_TEST_DATABASE_URL")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not DSN, reason="SURGE_TEST_DATABASE_URL is not set"),
]

MIGRATIONS = pathlib.Path(__file__).resolve().parents[2] / "supabase" / "migrations"

#: `create or replace function <schema>.<name>(...) ... as $tag$ <body> $tag$`.
#: The tag may be empty - most of this project's functions use a bare `$$` - so
#: `\w*` rather than `\w+`. Requiring a tag matched a tenth of them and would
#: have made this guard look like it was watching the whole schema.
_FUNCTION = re.compile(
    r"create\s+or\s+replace\s+function\s+(\w+)\.(\w+)\s*\(.*?\$(\w*)\$(.*?)\$\3\$",
    re.S | re.I,
)


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _bodies_in_the_files() -> dict[tuple[str, str], set[str]]:
    """Every body each function name is given across the migrations.

    A set rather than "the last one wins", because a name can be overloaded:
    ``ref.listings_as_of`` has two signatures and the database holds both. A
    last-wins map would call the older signature drifted for no better reason
    than that it is not the newer one.
    """

    bodies: dict[tuple[str, str], set[str]] = {}
    for path in sorted(MIGRATIONS.glob("*.sql")):
        text = path.read_text(encoding="utf-8")
        for schema, name, _tag, body in _FUNCTION.findall(text):
            bodies.setdefault((schema.lower(), name.lower()), set()).add(_normalise(body))
    return bodies


@pytest.fixture()
def conn():
    connection = psycopg2.connect(DSN)
    connection.autocommit = False
    try:
        yield connection
    finally:
        connection.rollback()
        connection.close()


def test_the_migrations_define_functions_at_all():
    """A guard on the guard: if the regex stopped matching, every other
    assertion here would pass vacuously."""

    bodies = _bodies_in_the_files()

    assert len(bodies) > 60
    assert ("prod", "begin_entry_analysis") in bodies


def test_every_function_in_the_database_matches_its_migration(conn):
    bodies = _bodies_in_the_files()

    with conn.cursor() as cur:
        cur.execute(
            """
            select n.nspname, p.proname, p.prosrc
              from pg_proc p
              join pg_namespace n on n.oid = p.pronamespace
             where n.nspname in ('prod', 'labels', 'market', 'analysis', 'ref',
                                 'pipeline', 'universe', 'news', 'research', 'ui')
            """
        )
        live = cur.fetchall()

    drifted = []
    for schema, name, source in live:
        expected = bodies.get((schema.lower(), name.lower()))
        if not expected:
            # Not defined by a `create or replace` in any migration - a `create
            # function` without `or replace`, or something an extension owns.
            continue
        if _normalise(source) not in expected:
            drifted.append(f"{schema}.{name}")

    assert drifted == [], (
        "these functions differ between supabase/migrations and the database: "
        + ", ".join(sorted(drifted))
        + ". The migrations are the source of truth, so the database is what has to change"
    )
