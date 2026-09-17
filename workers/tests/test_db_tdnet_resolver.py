"""The TDnet resolver reads the security master bitemporally.

Two times, and they answer different questions. ``effective_at`` asks which
listing was in force when the disclosure was published; ``known_at`` asks what
the master had learned by the time we read it. Collapsing them - which a query
against current rows does silently - means a 2025 disclosure resolved in 2026
would claim we knew that mapping in 2025.

The fixture reads back the effective and available times the apply step actually
recorded, rather than assuming them. That keeps these tests about the resolver
instead of about which timestamp ``apply_master_snapshot`` happens to choose.
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


def _jp_listing_with_a_code_change(cur) -> dict:
    """One JP security that traded under one code and then another."""

    suffix = uuid.uuid4().hex[:3].upper()
    old_code = f"1{suffix}"
    new_code = f"2{suffix}"
    identity_key = f"JP:EDINET:E{uuid.uuid4().hex[:5].upper()}:COMMON_STOCK:"

    for symbol, moment in ((old_code, FIRST), (new_code, SECOND)):
        run = _new_run(cur, "JP", moment)
        _add_snapshot_row(
            cur,
            run,
            symbol=symbol,
            identity_key=identity_key,
            observed_at=moment,
            exchange_id="XTKS",
            segment="P",
            issuer_key=f"EDINET:E{uuid.uuid4().hex[:5].upper()}",
            issuer_source="EDINET_CODE",
            name="Example Japanese Issuer",
            market_code="JP",
            currency="JPY",
            country="JP",
            source_id="jpx_listed_companies",
            security_identity_source="JPX_LOCAL_CODE",
        )
        _apply(cur, run)

    cur.execute(
        "select listing_id, security_id::text from ref.listings where listing_identity_key = %s",
        (f"XTKS|{identity_key}",),
    )
    row = cur.fetchone()
    assert row is not None, "the fixture listing was not created"
    listing_id, security_id = row

    cur.execute(
        """
        select symbol, effective_from, effective_to, available_at
        from ref.listing_symbols
        where listing_id = %s and symbol_type = 'TICKER'
        order by effective_from
        """,
        (listing_id,),
    )
    symbols = cur.fetchall()

    cur.execute(
        "select min(effective_from), min(available_at) from ref.listing_states where listing_id = %s",
        (listing_id,),
    )
    state_from, state_available = cur.fetchone()

    return {
        "old_code": old_code,
        "new_code": new_code,
        "security_id": security_id,
        "listing_id": listing_id,
        "symbols": symbols,
        "state_from": state_from,
        "state_available": state_available,
    }


def _window(fixture, symbol):
    """The effective window the master actually recorded for one symbol."""

    for recorded, effective_from, effective_to, available_at in fixture["symbols"]:
        if recorded == symbol:
            return effective_from, effective_to, available_at
    raise AssertionError(f"{symbol} is not in the recorded symbol history: {fixture['symbols']}")


def _inside_old_window(fixture):
    """A moment at which the old code was the live one."""

    old_from, _old_to, _ = _window(fixture, fixture["old_code"])
    return max(old_from, fixture["state_from"]) + timedelta(seconds=1)


def test_a_disclosure_resolves_to_the_code_that_was_current_then(conn):
    """The rule this whole change is for.

    A disclosure carries the code that was live when it was published. Resolving
    it against today's listing would find nothing - or worse, find whoever holds
    that code now.
    """

    with conn.cursor() as cur:
        fixture = _jp_listing_with_a_code_change(cur)
        resolver = DatabaseResolver(conn)

        resolved, market = resolver.resolve(
            fixture["old_code"], as_of=_inside_old_window(fixture), known_at=NOW
        )
        assert resolved == fixture["security_id"]
        assert market == "JP"

        new_from, _, _ = _window(fixture, fixture["new_code"])
        resolved_now, _ = resolver.resolve(
            fixture["new_code"], as_of=new_from + timedelta(seconds=1), known_at=NOW
        )
        assert resolved_now == fixture["security_id"]


def test_the_new_code_does_not_resolve_at_the_old_effective_time(conn):
    """The as-of read does not follow the materialised current value."""

    with conn.cursor() as cur:
        fixture = _jp_listing_with_a_code_change(cur)
        resolver = DatabaseResolver(conn)

        resolved, _ = resolver.resolve(
            fixture["new_code"], as_of=_inside_old_window(fixture), known_at=NOW
        )
        assert resolved is None, "the future code answered a question about the past"


def test_knowledge_time_gates_what_the_master_may_say(conn):
    """The second half of bitemporal.

    Using today's master to resolve an old disclosure is allowed. Claiming we
    knew that mapping back then is not - so a read whose knowledge cutoff
    predates the master learning anything returns nothing.
    """

    with conn.cursor() as cur:
        fixture = _jp_listing_with_a_code_change(cur)
        _old_from, _old_to, old_available = _window(fixture, fixture["old_code"])
        effective = _inside_old_window(fixture)
        resolver = DatabaseResolver(conn)

        known_late, _ = resolver.resolve(fixture["old_code"], as_of=effective, known_at=NOW)
        assert known_late == fixture["security_id"]

        before_we_knew = min(old_available, fixture["state_available"]) - timedelta(days=1)
        known_early, _ = resolver.resolve(
            fixture["old_code"], as_of=effective, known_at=before_we_knew
        )
        assert known_early is None, "the master answered a question asked before it knew anything"


def test_the_cache_key_carries_both_times(conn):
    """Keyed on the code alone, the first answer for a code would stand in for
    every later question about it - including ones asked as of a different day,
    which is the case this resolver exists to get right."""

    with conn.cursor() as cur:
        fixture = _jp_listing_with_a_code_change(cur)
        new_from, _, _ = _window(fixture, fixture["new_code"])
        resolver = DatabaseResolver(conn)

        resolver.resolve(fixture["new_code"], as_of=new_from + timedelta(seconds=1), known_at=NOW)
        # Same code, earlier effective time. A code-only cache would return the
        # answer above; this must go back to the database and find nothing.
        earlier, _ = resolver.resolve(
            fixture["new_code"], as_of=_inside_old_window(fixture), known_at=NOW
        )

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
