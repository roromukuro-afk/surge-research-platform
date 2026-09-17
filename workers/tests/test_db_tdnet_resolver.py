"""The TDnet resolver reads the security master bitemporally.

Two times, and they answer different questions. ``effective_at`` asks which
listing was in force when the disclosure was published; ``known_at`` asks what
the master had learned by the time we read it. Collapsing them - which a query
against current rows does silently - means a 2025 disclosure resolved in 2026
would claim we knew that mapping in 2025.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest

psycopg2 = pytest.importorskip("psycopg2")

from surge.jobs.tdnet_discovery import DatabaseResolver  # noqa: E402
from surge.news.sources.yanoshin_tdnet import normalise_company_code  # noqa: E402
from test_db_master_semantics import _add_snapshot_row, _apply, _new_run  # noqa: E402

DSN = os.environ.get("SURGE_TEST_DATABASE_URL")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not DSN, reason="SURGE_TEST_DATABASE_URL is not set"),
]

FIRST = datetime(2025, 3, 4, 6, 0, tzinfo=UTC)
SECOND = datetime(2026, 5, 20, 6, 0, tzinfo=UTC)
NOW = datetime(2026, 9, 17, 6, 0, tzinfo=UTC)


@pytest.fixture()
def conn():
    connection = psycopg2.connect(DSN)
    connection.autocommit = False
    try:
        yield connection
    finally:
        connection.rollback()
        connection.close()


def _jp_listing_with_a_code_change(cur) -> tuple[str, str, str]:
    """One JP security that traded under one code and then another.

    Returns ``(old_code, new_code, security_id)``.
    """

    suffix = uuid.uuid4().hex[:4].upper()
    old_code = f"1{suffix[:3]}"
    new_code = f"2{suffix[:3]}"
    identity_key = f"JP:EDINET:E{uuid.uuid4().hex[:5].upper()}:COMMON_STOCK:"

    run_one = _new_run(cur, "JP", FIRST)
    _add_snapshot_row(
        cur,
        run_one,
        symbol=old_code,
        identity_key=identity_key,
        observed_at=FIRST,
        exchange_id="XTKS",
        segment="P",
        issuer_key=f"EDINET:E{uuid.uuid4().hex[:5].upper()}",
        issuer_source="EDINET_CODE",
        name="Example Japanese Issuer",
    )
    _apply(cur, run_one)

    run_two = _new_run(cur, "JP", SECOND)
    _add_snapshot_row(
        cur,
        run_two,
        symbol=new_code,
        identity_key=identity_key,
        observed_at=SECOND,
        exchange_id="XTKS",
        segment="P",
        issuer_key=f"EDINET:E{uuid.uuid4().hex[:5].upper()}",
        issuer_source="EDINET_CODE",
        name="Example Japanese Issuer",
    )
    _apply(cur, run_two)

    cur.execute(
        "select security_id::text from ref.listings where listing_identity_key = %s",
        (f"XTKS|{identity_key}",),
    )
    row = cur.fetchone()
    return old_code, new_code, (row[0] if row else None)


def test_a_disclosure_resolves_to_the_code_that_was_current_then(conn):
    """The rule this whole change is for.

    A 2025 disclosure carries the 2025 code. Resolving it against today's
    listing would find nothing - or worse, find whoever holds that code now.
    """

    with conn.cursor() as cur:
        old_code, new_code, security_id = _jp_listing_with_a_code_change(cur)
        assert security_id is not None

        resolver = DatabaseResolver(conn)

        # Backfill: a 2025 disclosure, read in 2026.
        resolved, market = resolver.resolve(
            old_code, as_of=FIRST + timedelta(hours=1), known_at=NOW
        )
        assert resolved == security_id
        assert market == "JP"

        # The same security under its current code, read as of now.
        resolved_now, _ = resolver.resolve(new_code, as_of=NOW, known_at=NOW)
        assert resolved_now == security_id


def test_the_new_code_does_not_resolve_at_the_old_effective_time(conn):
    """The as-of read does not follow the materialised current value."""

    with conn.cursor() as cur:
        old_code, new_code, security_id = _jp_listing_with_a_code_change(cur)
        resolver = DatabaseResolver(conn)

        resolved, _ = resolver.resolve(new_code, as_of=FIRST + timedelta(hours=1), known_at=NOW)
        assert resolved is None, "the future code answered a question about the past"


def test_knowledge_time_gates_what_the_master_may_say(conn):
    """The second half of bitemporal.

    Using today's master to resolve a 2025 disclosure is allowed. Claiming we
    knew that mapping in 2025 is not - so a read whose knowledge cutoff predates
    the master learning anything returns nothing.
    """

    with conn.cursor() as cur:
        old_code, _new_code, security_id = _jp_listing_with_a_code_change(cur)
        resolver = DatabaseResolver(conn)

        known_late, _ = resolver.resolve(old_code, as_of=FIRST + timedelta(hours=1), known_at=NOW)
        assert known_late == security_id

        known_early, _ = resolver.resolve(
            old_code, as_of=FIRST + timedelta(hours=1), known_at=FIRST - timedelta(days=30)
        )
        assert known_early is None, "the master answered a question asked before it knew anything"


def test_the_cache_key_carries_both_times(conn):
    """Keyed on the code alone, the first answer for a code would stand in for
    every later question about it - including ones asked as of a different day,
    which is the case this resolver exists to get right."""

    with conn.cursor() as cur:
        old_code, new_code, security_id = _jp_listing_with_a_code_change(cur)
        resolver = DatabaseResolver(conn)

        resolver.resolve(new_code, as_of=NOW, known_at=NOW)
        # Same code, earlier effective time. A code-only cache would return the
        # answer above; this must go back to the database and find nothing.
        earlier, _ = resolver.resolve(new_code, as_of=FIRST + timedelta(hours=1), known_at=NOW)

        assert earlier is None
        assert len(resolver._cache) == 2
        assert {len(key) for key in resolver._cache} == {3}


def test_resolve_code_tries_the_base_and_reports_which_key_matched(conn):
    """Against the real master, where every ETF is held under four characters."""

    with conn.cursor() as cur:
        cur.execute(
            "select count(*) from ref.listings l join ref.securities s using (security_id) "
            "where s.market_code = 'JP' and l.local_code = '1326'"
        )
        if cur.fetchone()[0] == 0:
            pytest.skip("the JP master is not loaded in this database")

        resolver = DatabaseResolver(conn)
        code = normalise_company_code("13264")
        security_id, market, matched = resolver.resolve_code(code, as_of=NOW, known_at=NOW)

        assert security_id is not None
        assert market == "JP"
        assert matched == "1326", "the exact five-character form should not have matched"
