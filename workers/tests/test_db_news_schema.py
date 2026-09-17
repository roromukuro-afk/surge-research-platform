"""Phase 4 database behaviour: knowledge time, licence guard, append-only, purge.

These run against a real Postgres with the migrations applied. CI provides one;
locally they are skipped unless SURGE_TEST_DATABASE_URL is set.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest

psycopg2 = pytest.importorskip("psycopg2")

DSN = os.environ.get("SURGE_TEST_DATABASE_URL")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not DSN, reason="SURGE_TEST_DATABASE_URL is not set"),
]

PUBLISHED = datetime(2025, 3, 4, 6, 0, tzinfo=UTC)
SEEN = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)
INGESTED = datetime(2026, 9, 17, 12, 0, 30, tzinfo=UTC)


@pytest.fixture()
def conn():
    connection = psycopg2.connect(DSN)
    connection.autocommit = False
    try:
        yield connection
    finally:
        connection.rollback()
        connection.close()


def _source(cur, *, key: str | None = None, **overrides) -> str:
    source_key = key or f"test_src_{uuid.uuid4().hex[:8]}"
    values = {
        "name": "Test ministry",
        "scope": "JP",
        "source_kind": "GOVERNMENT",
        "access_mechanism": "RSS",
        "official_url": "https://example.gov/",
    }
    values.update(overrides)
    cur.execute(
        """
        insert into news.sources (source_key, name, scope, source_kind, access_mechanism, official_url, enabled)
        values (%s, %s, %s, %s, %s, %s, true)
        """,
        (
            source_key,
            values["name"],
            values["scope"],
            values["source_kind"],
            values["access_mechanism"],
            values["official_url"],
        ),
    )
    return source_key


def _policy(cur, source_key: str, *, full_text="ALLOWED", metadata="ALLOWED", version="v1") -> None:
    cur.execute(
        """
        insert into news.source_policies (
          source_key, policy_version, license_mode,
          full_text_storage_allowed, metadata_storage_allowed, deciding_clause, terms_url
        ) values (%s, %s, 'PUBLIC_REDISTRIBUTABLE', %s, %s, 'test clause', 'https://example.gov/terms')
        """,
        (source_key, version, full_text, metadata),
    )


def _document(cur, source_key: str, **overrides):
    values = {
        "source_document_id": f"doc-{uuid.uuid4().hex[:8]}",
        "document_type": "PRESS_RELEASE",
        "title": "A release",
        "source_published_at": PUBLISHED,
        "source_published_precision": "EXACT",
        "system_first_seen_at": SEEN,
        "ingested_at": INGESTED,
        "available_to_model_at": INGESTED,
        "content_sha256": uuid.uuid4().hex,
        "body_storage": "METADATA_ONLY",
        "body_text": None,
        "raw_object_key": None,
        "replay_assumed_available_at": None,
        "replay_assumption_note": None,
        "revision_seq": 1,
    }
    values.update(overrides)
    cur.execute(
        """
        insert into news.documents (
          source_key, source_document_id, document_type, title,
          source_published_at, source_published_precision,
          system_first_seen_at, ingested_at, available_to_model_at,
          content_sha256, body_storage, body_text, raw_object_key,
          replay_assumed_available_at, replay_assumption_note, revision_seq
        ) values (
          %(source_key)s, %(source_document_id)s, %(document_type)s, %(title)s,
          %(source_published_at)s, %(source_published_precision)s,
          %(system_first_seen_at)s, %(ingested_at)s, %(available_to_model_at)s,
          %(content_sha256)s, %(body_storage)s, %(body_text)s, %(raw_object_key)s,
          %(replay_assumed_available_at)s, %(replay_assumption_note)s, %(revision_seq)s
        )
        returning document_id
        """,
        {"source_key": source_key, **values},
    )
    return cur.fetchone()[0]


# --------------------------------------------------------------------- knowledge time


def test_as_of_hides_documents_we_did_not_yet_have(conn):
    with conn.cursor() as cur:
        source_key = _source(cur)
        _policy(cur, source_key)
        _document(cur, source_key, source_document_id="early")
        _document(
            cur,
            source_key,
            source_document_id="late",
            system_first_seen_at=INGESTED + timedelta(days=1),
            ingested_at=INGESTED + timedelta(days=1),
            available_to_model_at=INGESTED + timedelta(days=1),
        )

        cur.execute(
            "select source_document_id from news.documents_as_of(%s) where source_key = %s",
            (INGESTED + timedelta(hours=1), source_key),
        )
        assert [row[0] for row in cur.fetchall()] == ["early"]


def test_a_backfilled_release_is_invisible_to_a_cutoff_before_the_backfill(conn):
    """The headline rule of Phase 4, enforced by the read path rather than by care.

    The document was published in March 2025 and fetched in September 2026. A
    replay standing in April 2025 must not see it, however old its publication
    date is.
    """

    with conn.cursor() as cur:
        source_key = _source(cur)
        _policy(cur, source_key)
        _document(cur, source_key, source_document_id="backfilled")

        cur.execute(
            "select count(*) from news.documents_as_of(%s) where source_key = %s",
            (datetime(2025, 4, 1, tzinfo=UTC), source_key),
        )
        assert cur.fetchone()[0] == 0


def test_as_of_does_not_expose_the_replay_assumption(conn):
    """Production must not be able to read a counterfactual by accident."""

    with conn.cursor() as cur:
        source_key = _source(cur)
        _policy(cur, source_key)
        _document(
            cur,
            source_key,
            replay_assumed_available_at=PUBLISHED,
            replay_assumption_note="assume the collector was running",
        )
        cur.execute("select * from news.documents_as_of(%s) limit 0", (INGESTED,))
        returned = {description[0] for description in cur.description}

        assert "replay_assumed_available_at" not in returned
        assert "replay_assumption_note" not in returned
        assert "available_to_model_at" in returned


def test_replay_assumption_without_a_note_is_rejected(conn):
    with conn.cursor() as cur:
        source_key = _source(cur)
        _policy(cur, source_key)
        with pytest.raises(psycopg2.errors.CheckViolation):
            _document(cur, source_key, replay_assumed_available_at=PUBLISHED)


def test_storing_before_noticing_is_rejected(conn):
    with conn.cursor() as cur:
        source_key = _source(cur)
        _policy(cur, source_key)
        with pytest.raises(psycopg2.errors.CheckViolation):
            _document(cur, source_key, system_first_seen_at=INGESTED + timedelta(hours=1))


def test_model_availability_before_ingestion_is_rejected(conn):
    with conn.cursor() as cur:
        source_key = _source(cur)
        _policy(cur, source_key)
        with pytest.raises(psycopg2.errors.CheckViolation):
            _document(cur, source_key, available_to_model_at=SEEN)


# ------------------------------------------------------------------------- licensing


def test_full_text_storage_needs_an_explicit_permission(conn):
    with conn.cursor() as cur:
        source_key = _source(cur)
        _policy(cur, source_key, full_text="NOT_SPECIFIED")
        with pytest.raises(psycopg2.errors.RaiseException, match="full-text storage"):
            cur.execute("select news.assert_storage_allows(%s, 'FULL_TEXT')", (source_key,))


def test_metadata_storage_needs_an_explicit_permission(conn):
    with conn.cursor() as cur:
        source_key = _source(cur)
        _policy(cur, source_key, metadata="UNKNOWN")
        with pytest.raises(psycopg2.errors.RaiseException, match="metadata storage"):
            cur.execute("select news.assert_storage_allows(%s, 'METADATA_ONLY')", (source_key,))


def test_a_source_with_no_policy_at_all_cannot_store_anything(conn):
    with conn.cursor() as cur:
        source_key = _source(cur)
        with pytest.raises(psycopg2.errors.RaiseException, match="no licence policy"):
            cur.execute("select news.assert_storage_allows(%s, 'METADATA_ONLY')", (source_key,))


def test_permitted_storage_passes(conn):
    with conn.cursor() as cur:
        source_key = _source(cur)
        _policy(cur, source_key)
        cur.execute("select news.assert_storage_allows(%s, 'FULL_TEXT')", (source_key,))


def test_body_may_not_be_present_when_the_row_says_it_was_not_stored(conn):
    with conn.cursor() as cur:
        source_key = _source(cur)
        _policy(cur, source_key)
        with pytest.raises(psycopg2.errors.CheckViolation):
            _document(cur, source_key, body_storage="METADATA_ONLY", body_text="smuggled text")


def test_policies_are_append_only(conn):
    with conn.cursor() as cur:
        source_key = _source(cur)
        _policy(cur, source_key)
        with pytest.raises(psycopg2.errors.RaiseException, match="append-only"):
            cur.execute(
                "update news.source_policies set full_text_storage_allowed = 'ALLOWED' where source_key = %s",
                (source_key,),
            )


def test_closing_a_policy_is_the_one_permitted_update(conn):
    with conn.cursor() as cur:
        source_key = _source(cur)
        _policy(cur, source_key)
        cur.execute(
            "update news.source_policies set effective_to = clock_timestamp() where source_key = %s",
            (source_key,),
        )
        assert cur.rowcount == 1


def test_a_closed_policy_cannot_be_reopened(conn):
    with conn.cursor() as cur:
        source_key = _source(cur)
        _policy(cur, source_key)
        cur.execute("update news.source_policies set effective_to = clock_timestamp() where source_key = %s", (source_key,))
        with pytest.raises(psycopg2.errors.RaiseException, match="append-only"):
            cur.execute("update news.source_policies set effective_to = null where source_key = %s", (source_key,))


# ------------------------------------------------------------------------ immutability


def test_documents_cannot_be_updated(conn):
    with conn.cursor() as cur:
        source_key = _source(cur)
        _policy(cur, source_key)
        document_id = _document(cur, source_key)
        with pytest.raises(psycopg2.errors.RaiseException, match="new revision"):
            cur.execute("update news.documents set title = 'rewritten' where document_id = %s", (document_id,))


def test_documents_cannot_be_deleted_outside_the_purge_path(conn):
    with conn.cursor() as cur:
        source_key = _source(cur)
        _policy(cur, source_key)
        document_id = _document(cur, source_key)
        with pytest.raises(psycopg2.errors.RaiseException, match="purge_documents"):
            cur.execute("delete from news.documents where document_id = %s", (document_id,))


def test_an_amendment_is_a_second_row_not_an_overwrite(conn):
    with conn.cursor() as cur:
        source_key = _source(cur)
        _policy(cur, source_key)
        first = _document(cur, source_key, source_document_id="amended", content_sha256="hash-one")
        _document(
            cur,
            source_key,
            source_document_id="amended",
            content_sha256="hash-two",
            revision_seq=2,
            title="A release (corrected)",
        )
        cur.execute(
            "select revision_seq, title from news.documents where source_key = %s and source_document_id = 'amended' "
            "order by revision_seq",
            (source_key,),
        )
        rows = cur.fetchall()

        assert [row[0] for row in rows] == [1, 2]
        assert rows[0][1] == "A release"
        assert first is not None


def test_identical_content_collides_rather_than_duplicating(conn):
    with conn.cursor() as cur:
        source_key = _source(cur)
        _policy(cur, source_key)
        _document(cur, source_key, source_document_id="same", content_sha256="identical")
        with pytest.raises(psycopg2.errors.UniqueViolation):
            _document(cur, source_key, source_document_id="same", content_sha256="identical")


# ------------------------------------------------------------------------------ purge


def test_purge_dry_run_counts_without_deleting(conn):
    with conn.cursor() as cur:
        source_key = _source(cur)
        _policy(cur, source_key)
        _document(cur, source_key, raw_object_key="raw/test/news/abc.xml")

        cur.execute(
            """
            insert into market.purge_requests (provider_id, reason, requested_by, dry_run)
            values ('test_news', 'licence test', 'test', true)
            returning purge_request_id
            """
        )
        request_id = cur.fetchone()[0]
        cur.execute(
            "insert into market.purge_targets (purge_request_id, object_key, store_id) values (%s, %s, 'test')",
            (request_id, "raw/test/news/abc.xml"),
        )

        cur.execute("select news.purge_documents(%s)", (request_id,))
        result = cur.fetchone()[0]
        assert result == {"dry_run": True, "news_documents": 1}

        cur.execute("select count(*) from news.documents where source_key = %s", (source_key,))
        assert cur.fetchone()[0] == 1


def test_purge_deletes_only_what_the_request_covers(conn):
    with conn.cursor() as cur:
        source_key = _source(cur)
        _policy(cur, source_key)
        _document(cur, source_key, source_document_id="covered", raw_object_key="raw/test/news/covered.xml")
        _document(cur, source_key, source_document_id="spared", raw_object_key="raw/test/news/spared.xml")

        cur.execute(
            """
            insert into market.purge_requests (provider_id, reason, requested_by, dry_run)
            values ('test_news', 'licence test', 'test', false)
            returning purge_request_id
            """
        )
        request_id = cur.fetchone()[0]
        cur.execute(
            "insert into market.purge_targets (purge_request_id, object_key, store_id) values (%s, %s, 'test')",
            (request_id, "raw/test/news/covered.xml"),
        )

        cur.execute("select news.purge_documents(%s)", (request_id,))
        assert cur.fetchone()[0] == {"dry_run": False, "news_documents": 1}

        cur.execute("select source_document_id from news.documents where source_key = %s", (source_key,))
        assert [row[0] for row in cur.fetchall()] == ["spared"]


def test_the_purge_flag_does_not_survive_the_function(conn):
    """A session that just ran a purge must not be able to delete freely."""

    with conn.cursor() as cur:
        source_key = _source(cur)
        _policy(cur, source_key)
        _document(cur, source_key, source_document_id="covered", raw_object_key="raw/test/news/covered.xml")
        document_id = _document(cur, source_key, source_document_id="after", raw_object_key="raw/test/news/after.xml")

        cur.execute(
            """
            insert into market.purge_requests (provider_id, reason, requested_by, dry_run)
            values ('test_news', 'licence test', 'test', false)
            returning purge_request_id
            """
        )
        request_id = cur.fetchone()[0]
        cur.execute(
            "insert into market.purge_targets (purge_request_id, object_key, store_id) values (%s, %s, 'test')",
            (request_id, "raw/test/news/covered.xml"),
        )
        cur.execute("select news.purge_documents(%s)", (request_id,))

        with pytest.raises(psycopg2.errors.RaiseException, match="purge_documents"):
            cur.execute("delete from news.documents where document_id = %s", (document_id,))


# -------------------------------------------------------------------------- privilege


def test_news_is_covered_by_the_schema_guard(conn):
    with conn.cursor() as cur:
        cur.execute("select 'news' = any (pipeline.project_schemas())")
        assert cur.fetchone()[0] is True

        cur.execute(
            """
            select count(*) from pg_proc p
            join pg_namespace n on n.oid = p.pronamespace
            where n.nspname = 'news' and has_function_privilege('public', p.oid, 'EXECUTE')
            """
        )
        assert cur.fetchone()[0] == 0


def test_the_runtime_worker_holds_no_delete_on_news(conn):
    with conn.cursor() as cur:
        cur.execute(
            """
            select count(*) from information_schema.role_table_grants
            where table_schema = 'news' and privilege_type = 'DELETE'
              and grantee in ('surge_worker_prod', 'surge_worker_research')
            """
        )
        assert cur.fetchone()[0] == 0
