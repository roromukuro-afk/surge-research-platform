"""TDnet discovery in the database: the licence guard, the code rule, and time.

The rule with the sharpest edge is the last one. Discovery happens when Yanoshin
first hands us the item; fetching the official document hours later must not
move that. It holds because an event's first_known_at is the minimum over its
sources and a later source has a later time - so it is a property of the schema
rather than a discipline anyone has to maintain.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, date, datetime, timedelta

import pytest

psycopg2 = pytest.importorskip("psycopg2")

DSN = os.environ.get("SURGE_TEST_DATABASE_URL")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not DSN, reason="SURGE_TEST_DATABASE_URL is not set"),
]

DISCOVERED_AT = datetime(2026, 9, 17, 4, 20, tzinfo=UTC)
PUBLISHED_AT = datetime(2026, 9, 17, 4, 0, tzinfo=UTC)
SHA = "a" * 64


@pytest.fixture()
def conn():
    connection = psycopg2.connect(DSN)
    connection.autocommit = False
    try:
        yield connection
    finally:
        connection.rollback()
        connection.close()


def _document(cur, source_key="yanoshin_tdnet", *, available_at=DISCOVERED_AT, doc_id=None):
    cur.execute(
        """
        insert into news.documents (
          source_key, source_document_id, document_type, title,
          source_published_at, source_published_precision,
          system_first_seen_at, ingested_at, available_to_model_at,
          content_sha256, body_storage
        ) values (%s, %s, 'TIMELY_DISCLOSURE', 'A disclosure', %s, 'EXACT', %s, %s, %s, %s, 'METADATA_ONLY')
        returning document_id
        """,
        (
            source_key,
            doc_id or f"doc-{uuid.uuid4().hex[:8]}",
            PUBLISHED_AT,
            available_at,
            available_at,
            available_at,
            uuid.uuid4().hex,
        ),
    )
    return cur.fetchone()[0]


def _tdnet_item(cur, document_id, *, raw_code="72030", normalised="7203", kind="FIVE_CHAR_TRAILING_ZERO_STRIPPED"):
    cur.execute(
        """
        insert into news.tdnet_items (
          document_id, yanoshin_id, pubdate, raw_company_code, normalised_company_code,
          code_normalisation, title, source_endpoint, raw_response_sha256,
          system_first_seen_at, ingested_at, available_to_model_at
        ) values (%s, %s, %s, %s, %s, %s, 'A disclosure', 'https://example/recent.json', %s, %s, %s, %s)
        """,
        (
            document_id,
            int(uuid.uuid4().int % 10_000_000),
            PUBLISHED_AT,
            raw_code,
            normalised,
            kind,
            SHA,
            DISCOVERED_AT,
            DISCOVERED_AT,
            DISCOVERED_AT,
        ),
    )


# ------------------------------------------------------------------- licensing


def test_the_source_permits_metadata_and_refuses_full_text(conn):
    """Being able to reach an index that links to a PDF licenses nothing about
    storing the PDF, and the database says so before anything is written."""

    with conn.cursor() as cur:
        cur.execute("select news.assert_storage_allows('yanoshin_tdnet', 'METADATA_ONLY')")

        with pytest.raises(psycopg2.errors.RaiseException, match="full-text storage"):
            cur.execute("select news.assert_storage_allows('yanoshin_tdnet', 'FULL_TEXT')")


def test_the_source_is_registered_for_discovery_only(conn):
    """An unofficial index of a source we cannot read directly cannot also be
    the thing that confirms what a disclosure said."""

    with conn.cursor() as cur:
        cur.execute(
            "select discovery_role, verification_role, enabled from news.sources where source_key = 'yanoshin_tdnet'"
        )
        discovery, verification, enabled = cur.fetchone()

    assert discovery is True
    assert verification is False
    assert enabled is True


def test_a_verification_role_link_from_this_source_is_refused(conn):
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into material.events (event_key, event_type, scope, merge_version)
            values (%s, 'EARNINGS_FORECAST_REVISION', 'JP', 'event-merge-1.0.0')
            returning event_id
            """,
            (f"evt-{uuid.uuid4().hex[:10]}",),
        )
        event_id = cur.fetchone()[0]
        document_id = _document(cur)

        with pytest.raises(psycopg2.errors.RaiseException, match="not registered for verification"):
            cur.execute(
                """
                insert into material.event_sources
                  (event_id, document_id, source_role, available_to_model_at, source_key)
                values (%s, %s, 'VERIFICATION', %s, 'yanoshin_tdnet')
                """,
                (event_id, document_id, DISCOVERED_AT),
            )


# --------------------------------------------------------------- code rule


def test_a_five_char_code_ending_in_zero_must_be_stripped(conn):
    with conn.cursor() as cur:
        document_id = _document(cur)
        _tdnet_item(cur, document_id, raw_code="72030", normalised="7203")
        assert cur.rowcount == 1


def test_a_five_char_code_not_ending_in_zero_must_be_kept_whole(conn):
    """587A4 and 13264 are ETFs. Stripping the fifth character would merge them
    with whatever four-digit issuer shares the prefix."""

    with conn.cursor() as cur:
        document_id = _document(cur)
        _tdnet_item(cur, document_id, raw_code="587A4", normalised="587A4", kind="FIVE_CHAR_KEPT")
        assert cur.rowcount == 1


def test_stripping_a_non_zero_fifth_character_is_refused(conn):
    """The mistake the whole rule exists to prevent, rejected by the database."""

    with conn.cursor() as cur:
        document_id = _document(cur)
        with pytest.raises(psycopg2.errors.CheckViolation):
            _tdnet_item(
                cur, document_id, raw_code="587A4", normalised="587A", kind="FIVE_CHAR_TRAILING_ZERO_STRIPPED"
            )


def test_claiming_the_wrong_normalisation_branch_is_refused(conn):
    with conn.cursor() as cur:
        document_id = _document(cur)
        with pytest.raises(psycopg2.errors.CheckViolation):
            _tdnet_item(cur, document_id, raw_code="72030", normalised="72030", kind="FIVE_CHAR_KEPT")


def test_the_raw_code_cannot_be_blank(conn):
    with conn.cursor() as cur:
        document_id = _document(cur)
        with pytest.raises(psycopg2.errors.CheckViolation):
            _tdnet_item(cur, document_id, raw_code="   ", normalised="", kind="UNEXPECTED_SHAPE")


# ----------------------------------------------------------------------- time


def test_discovery_time_survives_a_later_official_source(conn):
    """The rule this whole design turns on.

    Yanoshin hands us the item at 04:20. The issuer's own IR release is fetched
    at 09:00. The event was knowable at 04:20 and stays that way - because
    first_known_at is the minimum over the sources, a later source can only ever
    confirm it, never postpone it.
    """

    with conn.cursor() as cur:
        cur.execute(
            """
            insert into material.events (event_key, event_type, scope, merge_version)
            values (%s, 'EARNINGS_FORECAST_REVISION', 'JP', 'event-merge-1.0.0')
            returning event_id
            """,
            (f"evt-{uuid.uuid4().hex[:10]}",),
        )
        event_id = cur.fetchone()[0]

        discovery_doc = _document(cur, "yanoshin_tdnet", available_at=DISCOVERED_AT)
        cur.execute(
            """
            insert into material.event_sources
              (event_id, document_id, source_role, available_to_model_at, source_key)
            values (%s, %s, 'DISCOVERY', %s, 'yanoshin_tdnet')
            """,
            (event_id, discovery_doc, DISCOVERED_AT),
        )
        cur.execute("select first_known_at from material.events where event_id = %s", (event_id,))
        assert cur.fetchone()[0] == DISCOVERED_AT

        # Hours later, the storable source arrives and confirms it.
        later = DISCOVERED_AT + timedelta(hours=4, minutes=40)
        verification_doc = _document(cur, "edinet", available_at=later)
        cur.execute(
            """
            insert into material.event_sources
              (event_id, document_id, source_role, available_to_model_at, source_key)
            values (%s, %s, 'VERIFICATION', %s, 'edinet')
            """,
            (event_id, verification_doc, later),
        )

        cur.execute("select first_known_at from material.events where event_id = %s", (event_id,))
        assert cur.fetchone()[0] == DISCOVERED_AT, "the official source moved the discovery time"

        # And the event now has two independent publishers behind it.
        cur.execute(
            "select count(distinct source_key) from material.event_sources "
            "where event_id = %s and source_role in ('DISCOVERY', 'VERIFICATION')",
            (event_id,),
        )
        assert cur.fetchone()[0] == 2


def test_a_disclosure_is_not_knowable_before_it_was_collected(conn):
    """Its own pubdate is earlier than our availability time, and that gap is the
    honest measure of how late we were."""

    with conn.cursor() as cur:
        document_id = _document(cur)
        cur.execute(
            "select source_published_at, available_to_model_at from news.documents where document_id = %s",
            (document_id,),
        )
        published, available = cur.fetchone()

    assert published == PUBLISHED_AT
    assert available == DISCOVERED_AT
    assert available > published


def test_an_index_row_cannot_claim_to_predate_its_own_collection(conn):
    with conn.cursor() as cur:
        document_id = _document(cur)
        with pytest.raises(psycopg2.errors.CheckViolation):
            cur.execute(
                """
                insert into news.tdnet_items (
                  document_id, yanoshin_id, pubdate, raw_company_code, normalised_company_code,
                  code_normalisation, title, source_endpoint, raw_response_sha256,
                  system_first_seen_at, ingested_at, available_to_model_at
                ) values (%s, 1, %s, '72030', '7203', 'FIVE_CHAR_TRAILING_ZERO_STRIPPED',
                          't', 'e', %s, %s, %s, %s)
                """,
                (document_id, PUBLISHED_AT, SHA, DISCOVERED_AT, DISCOVERED_AT, DISCOVERED_AT - timedelta(hours=1)),
            )


# ------------------------------------------------------------------- coverage


def test_the_coverage_row_carries_every_counter(conn):
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into news.tdnet_coverage (
              as_of_date, items_retrieved, unique_items, new_items, duplicate_items,
              unmapped_company_codes, document_url_present, xbrl_url_present,
              verification_source_found, metadata_only_events, fetch_errors,
              last_seen_id, collection_lag_seconds, endpoint
            ) values (%s, 300, 300, 42, 258, 7, 300, 22, 5, 37, 0, 1281127, 1422.298, 'recent.json?limit=300')
            returning coverage_id
            """,
            (date(2026, 9, 17),),
        )
        assert cur.fetchone()[0] is not None

        cur.execute(
            "select collection_lag_seconds, last_seen_id from news.tdnet_coverage "
            "where as_of_date = %s order by coverage_id desc limit 1",
            (date(2026, 9, 17),),
        )
        lag, last_id = cur.fetchone()

    assert float(lag) == pytest.approx(1422.298)
    assert last_id == 1281127
