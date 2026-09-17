"""The database side of Phase 4.

Same split as ``surge.market.db``: this module writes what it is given and reads
what it is asked for, and makes no judgements. Transaction control belongs to the
caller so one collection pass commits once, and a half-written day is never
visible to a reader.

The one thing it does enforce is that a document row cannot be written without
the licence check having passed - ``news.assert_storage_allows`` is called in the
same transaction as the insert, so a licence that forbids storage rolls the write
back rather than leaving the row behind.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime

from surge.news.collector import CollectionResult, KnownDocument
from surge.news.models import BodyStorage, CollectedDocument, SourceKind

INSERT_DOCUMENT = """
insert into news.documents (
  source_key, source_document_id, document_url, document_type, title, language,
  source_published_at, source_published_precision,
  system_first_seen_at, ingested_at, available_to_model_at, availability_basis,
  replay_assumed_available_at, replay_assumption_note,
  content_sha256, raw_object_key, body_storage, body_storage_reason, body_text, byte_size,
  run_id, fetch_id, revision_seq, supersedes_document_id
) values (
  %(source_key)s, %(source_document_id)s, %(document_url)s, %(document_type)s, %(title)s, %(language)s,
  %(source_published_at)s, %(source_published_precision)s,
  %(system_first_seen_at)s, %(ingested_at)s, %(available_to_model_at)s, %(availability_basis)s,
  %(replay_assumed_available_at)s, %(replay_assumption_note)s,
  %(content_sha256)s, %(raw_object_key)s, %(body_storage)s, %(body_storage_reason)s, %(body_text)s, %(byte_size)s,
  %(run_id)s, %(fetch_id)s, %(revision_seq)s, %(supersedes_document_id)s
)
on conflict (source_key, source_document_id, content_sha256) do nothing
returning document_id
"""

INSERT_COVERAGE = """
insert into news.source_coverage (
  run_id, source_key, as_of_date,
  items_listed, items_new, items_duplicate, items_revised, items_skipped_licence,
  fetch_errors, parse_errors, quality_warnings, coverage_quality, notes
) values (
  %(run_id)s, %(source_key)s, %(as_of_date)s,
  %(items_listed)s, %(items_new)s, %(items_duplicate)s, %(items_revised)s, %(items_skipped_licence)s,
  %(fetch_errors)s, %(parse_errors)s, %(quality_warnings)s, %(coverage_quality)s, %(notes)s
)
on conflict (run_id, source_key, as_of_date) do update set
  items_listed = excluded.items_listed,
  items_new = excluded.items_new,
  items_duplicate = excluded.items_duplicate,
  items_revised = excluded.items_revised,
  items_skipped_licence = excluded.items_skipped_licence,
  fetch_errors = excluded.fetch_errors,
  parse_errors = excluded.parse_errors,
  quality_warnings = excluded.quality_warnings,
  coverage_quality = excluded.coverage_quality,
  notes = excluded.notes
"""

UPSERT_CURSOR = """
insert into news.fetch_cursors (
  source_key, cursor_name, cursor_value, last_success_at, last_attempt_at,
  consecutive_failures, etag, last_modified, updated_at
) values (
  %(source_key)s, %(cursor_name)s, %(cursor_value)s, %(last_success_at)s, %(last_attempt_at)s,
  %(consecutive_failures)s, %(etag)s, %(last_modified)s, clock_timestamp()
)
on conflict (source_key, cursor_name) do update set
  cursor_value = excluded.cursor_value,
  last_success_at = coalesce(excluded.last_success_at, news.fetch_cursors.last_success_at),
  last_attempt_at = excluded.last_attempt_at,
  consecutive_failures = excluded.consecutive_failures,
  etag = excluded.etag,
  last_modified = excluded.last_modified,
  updated_at = clock_timestamp()
"""

SELECT_KNOWN = """
select distinct on (source_document_id)
       source_document_id, content_sha256, revision_seq, document_id
from news.documents
where source_key = %(source_key)s
order by source_document_id, revision_seq desc
"""

SELECT_SOURCE = """
select source_key, name, scope::text, source_kind::text, access_mechanism::text,
       official_url, feed_url, docs_url, auth_requirement::text, required_env,
       documented_rate_limit, min_request_interval_seconds,
       discovery_role, verification_role, fetch_priority, enabled, live_verified_at
from news.sources
where enabled = true
order by fetch_priority, source_key
"""


def document_params(
    document: CollectedDocument,
    *,
    run_id: str | None = None,
    fetch_id: str | None = None,
) -> dict:
    times = document.times
    return {
        "source_key": document.source_key,
        "source_document_id": document.source_document_id,
        "document_url": document.document_url,
        "document_type": document.document_type.value,
        "title": document.title,
        "language": document.language,
        "source_published_at": times.source_published_at,
        "source_published_precision": times.source_published_precision.value,
        "system_first_seen_at": times.system_first_seen_at,
        "ingested_at": times.ingested_at,
        "available_to_model_at": times.available_to_model_at,
        "availability_basis": times.availability_basis.value,
        "replay_assumed_available_at": times.replay_assumed_available_at,
        "replay_assumption_note": times.replay_assumption_note,
        "content_sha256": document.content_sha256,
        "raw_object_key": document.raw_object_key,
        "body_storage": document.body_storage.value,
        "body_storage_reason": document.body_storage_reason,
        "body_text": document.body_text,
        "byte_size": document.byte_size,
        "run_id": run_id,
        "fetch_id": fetch_id,
        "supersedes_document_id": document.supersedes_document_id,
        "revision_seq": document.revision_seq,
    }


def assert_storage_allowed(conn, source_key: str, body_storage: BodyStorage) -> None:
    """Ask the database whether this depth of storage is permitted.

    Deliberately a second check: :class:`~surge.news.collector.SourcePolicy`
    already decided in Python. Doing it again in the same transaction as the
    insert means a stale in-process policy, or a caller that built a document by
    hand, still cannot write text the licence forbids.
    """

    with conn.cursor() as cur:
        cur.execute(
            "select news.assert_storage_allows(%(source_key)s, %(body_storage)s::news.body_storage)",
            {"source_key": source_key, "body_storage": body_storage.value},
        )


def write_documents(
    conn,
    documents: Sequence[CollectedDocument],
    *,
    run_id: str | None = None,
    fetch_id: str | None = None,
) -> list[str]:
    """Insert documents, returning the ids of the ones that were actually new.

    ``on conflict do nothing`` makes a rerun of the same collection pass a no-op
    rather than a duplicate-key failure, which is what idempotent means here: the
    unique key is (source, document, content hash), so re-seeing identical
    content writes nothing and re-seeing changed content writes a new revision.
    """

    inserted: list[str] = []
    checked: set[tuple[str, str]] = set()
    with conn.cursor() as cur:
        for document in documents:
            key = (document.source_key, document.body_storage.value)
            if key not in checked:
                assert_storage_allowed(conn, document.source_key, document.body_storage)
                checked.add(key)
            cur.execute(INSERT_DOCUMENT, document_params(document, run_id=run_id, fetch_id=fetch_id))
            row = cur.fetchone()
            if row is not None:
                inserted.append(str(row[0]))
    return inserted


def write_coverage(conn, result: CollectionResult, *, run_id: str, as_of_date: date) -> None:
    with conn.cursor() as cur:
        cur.execute(
            INSERT_COVERAGE,
            {
                "run_id": run_id,
                "source_key": result.source_key,
                "as_of_date": as_of_date,
                "items_listed": result.items_listed,
                "items_new": result.items_new,
                "items_duplicate": result.items_duplicate,
                "items_revised": result.items_revised,
                "items_skipped_licence": result.items_skipped_licence,
                "fetch_errors": result.fetch_errors,
                "parse_errors": result.parse_errors,
                "quality_warnings": len(result.quality_warnings),
                "coverage_quality": result.coverage_quality,
                "notes": "\n".join(result.notes) or None,
            },
        )


def update_cursor(
    conn,
    *,
    source_key: str,
    cursor_name: str,
    cursor_value: str | None,
    attempted_at: datetime,
    succeeded: bool,
    previous_failures: int = 0,
    etag: str | None = None,
    last_modified: str | None = None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            UPSERT_CURSOR,
            {
                "source_key": source_key,
                "cursor_name": cursor_name,
                "cursor_value": cursor_value,
                "last_success_at": attempted_at if succeeded else None,
                "last_attempt_at": attempted_at,
                "consecutive_failures": 0 if succeeded else previous_failures + 1,
                "etag": etag,
                "last_modified": last_modified,
            },
        )


def read_known_documents(conn, source_key: str) -> dict[str, KnownDocument]:
    """The newest revision we hold per source document, for change detection."""

    with conn.cursor() as cur:
        cur.execute(SELECT_KNOWN, {"source_key": source_key})
        return {
            row[0]: KnownDocument(content_sha256=row[1], revision_seq=row[2], document_id=str(row[3]))
            for row in cur.fetchall()
        }


def read_enabled_sources(conn) -> list[dict]:
    """Enabled sources in fetch order.

    The order is a polling schedule, not a ranking of how much a source's word is
    worth. Phase 5 decides that per event.
    """

    with conn.cursor() as cur:
        cur.execute(SELECT_SOURCE)
        columns = [description[0] for description in cur.description]
        rows = [dict(zip(columns, row, strict=True)) for row in cur.fetchall()]
    for row in rows:
        row["source_kind"] = SourceKind(row["source_kind"])
    return rows


def read_documents_as_of(conn, knowledge_cutoff: datetime, *, source_keys: Sequence[str] | None = None) -> list[dict]:
    """Read through the as-of function, never straight off the table.

    ``news.documents_as_of`` filters on ``available_to_model_at`` and does not
    return the replay columns at all, so a caller cannot read a counterfactual
    availability time by accident. Reading the table directly would hand back
    both, which is exactly the mistake this wrapper exists to make inconvenient.
    """

    sql = "select * from news.documents_as_of(%(cutoff)s)"
    params: dict = {"cutoff": knowledge_cutoff}
    if source_keys:
        sql += " where source_key = any(%(source_keys)s)"
        params["source_keys"] = list(source_keys)
    sql += " order by available_to_model_at, source_key, source_document_id"

    with conn.cursor() as cur:
        cur.execute(sql, params)
        columns = [description[0] for description in cur.description]
        return [dict(zip(columns, row, strict=True)) for row in cur.fetchall()]
