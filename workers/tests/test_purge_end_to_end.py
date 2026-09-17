"""The licence purge, rehearsed and then performed, on real objects.

The audit asked for the purge to be demonstrated on a test object before any
licensed data is fetched, because a deletion obligation you have never exercised
is a deletion obligation you do not know you can meet. This walks the whole path:
bytes in a store, a manifest row, a rehearsal that changes nothing, then a real
purge that removes the bytes, the derived rows and leaves the evidence.

Needs a database (CI provides one); the object store is a local directory.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime

import pytest

psycopg2 = pytest.importorskip("psycopg2")

from surge.manifest import INSERT_RAW_OBJECT, RawObjectRecord, manifest_params  # noqa: E402
from surge.purge import PurgeRunner  # noqa: E402
from surge.storage.local import LocalObjectStore  # noqa: E402
from test_db_master_semantics import T1, _new_run  # noqa: E402

DSN = os.environ.get("SURGE_TEST_DATABASE_URL")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not DSN, reason="SURGE_TEST_DATABASE_URL is not set"),
]

NOW = datetime(2026, 9, 17, 0, 0, tzinfo=UTC)


@pytest.fixture()
def conn():
    connection = psycopg2.connect(DSN)
    connection.autocommit = False
    try:
        yield connection
    finally:
        connection.rollback()
        connection.close()


def _store_and_record(cur, store: LocalObjectStore, run_id, payload: bytes, *, min_plan: str) -> str:
    stored = store.put_content_addressed("jquants", "JQ_EQ_BARS_DAILY", payload, "application/json", "json")
    record = RawObjectRecord(
        object_key=stored.key,
        store_id=store.store_id,
        provider_id="jquants",
        dataset_key="JQ_EQ_BARS_DAILY",
        license_policy_version="jquants-2026-09-16",
        entitlement_plan="Standard",
        required_min_plan=min_plan,
        sha256=stored.sha256,
        bytes=stored.bytes,
        content_type=stored.content_type,
        request_url="https://api.jquants.com/v2/equities/bars/daily?date=2026-09-16",
        observed_at=NOW,
        available_at=NOW,
        availability_basis="OBSERVED_NOW",
        run_id=str(run_id),
    )
    cur.execute(INSERT_RAW_OBJECT, manifest_params(record))
    return stored.key


def test_a_rehearsal_deletes_nothing_and_a_purge_deletes_everything(conn, tmp_path):
    store = LocalObjectStore(tmp_path / "objects", store_id="test-store")
    cache = tmp_path / "cache"

    with conn.cursor() as cur:
        run = _new_run(cur, "JP", T1)
        # Two objects at different entitlements: only one is out of reach after
        # a downgrade from Standard to Light.
        standard_only = _store_and_record(
            cur, store, run, b'{"marker": "standard-only", "id": "%s"}' % uuid.uuid4().hex.encode(),
            min_plan="Standard",
        )
        light_ok = _store_and_record(
            cur, store, run, b'{"marker": "light-ok", "id": "%s"}' % uuid.uuid4().hex.encode(),
            min_plan="Light",
        )

    # A copy left in a local cache is still data we hold.
    cached = cache / standard_only
    cached.parent.mkdir(parents=True, exist_ok=True)
    cached.write_bytes(store.get(standard_only))

    runner = PurgeRunner(conn, {"test-store": store}, local_cache_dirs=[cache])

    # --- the rehearsal -----------------------------------------------------
    rehearsal = runner.run(
        provider_id="jquants",
        dataset_key=None,
        retained_plan="Light",
        reason="downgrade rehearsal",
        requested_by="pytest",
        dry_run=True,
    )

    assert rehearsal.targets == 1, "only the object the retained plan cannot reach"
    assert rehearsal.deleted == 0
    assert rehearsal.complete is False, "a rehearsal is never a completed obligation"
    assert store.head(standard_only) is not None, "the bytes are still there after a dry run"
    assert cached.exists()

    with conn.cursor() as cur:
        cur.execute("select purged_at from market.raw_objects where object_key = %s", (standard_only,))
        assert cur.fetchone()[0] is None

    # --- the real thing ----------------------------------------------------
    outcome = runner.run(
        provider_id="jquants",
        dataset_key=None,
        retained_plan="Light",
        reason="downgrade from Standard to Light",
        requested_by="pytest",
        dry_run=False,
    )

    assert outcome.targets == 1
    assert outcome.deleted == 1
    assert outcome.failed == 0
    assert outcome.complete is True
    assert outcome.local_files_removed == 1, "the cached copy has to go too"

    assert store.head(standard_only) is None, "the bytes are gone"
    assert not cached.exists()
    assert store.head(light_ok) is not None, "the object we are still entitled to is untouched"

    with conn.cursor() as cur:
        cur.execute("select purged_at from market.raw_objects where object_key = %s", (standard_only,))
        assert cur.fetchone()[0] is not None, "the manifest records that we held it and no longer do"

        cur.execute("select purged_at from market.raw_objects where object_key = %s", (light_ok,))
        assert cur.fetchone()[0] is None

        cur.execute(
            "select count(*) from market.outstanding_purge_obligations where purge_request_id = %s",
            (outcome.purge_request_id,),
        )
        assert cur.fetchone()[0] == 0, "nothing left outstanding"
    conn.rollback()


def test_an_object_already_gone_satisfies_the_obligation(conn, tmp_path):
    """We must not hold the data. Whether we personally deleted it is beside the point."""

    store = LocalObjectStore(tmp_path / "objects", store_id="test-store")

    with conn.cursor() as cur:
        run = _new_run(cur, "JP", T1)
        key = _store_and_record(
            cur, store, run, b'{"marker": "vanished", "id": "%s"}' % uuid.uuid4().hex.encode(),
            min_plan="Standard",
        )

    store.delete(key)  # something else removed it first

    outcome = PurgeRunner(conn, {"test-store": store}).run(
        provider_id="jquants",
        reason="cancellation",
        requested_by="pytest",
        dry_run=False,
    )

    assert outcome.not_found == 1
    assert outcome.deleted == 0
    assert outcome.complete is True

    with conn.cursor() as cur:
        cur.execute("select purged_at from market.raw_objects where object_key = %s", (key,))
        assert cur.fetchone()[0] is not None
    conn.rollback()


def test_a_store_that_fails_leaves_the_obligation_open(conn, tmp_path):
    """A purge that could not delete must not look like one that did."""

    class BrokenStore(LocalObjectStore):
        def delete(self, key: str) -> bool:
            raise OSError("the bucket is unreachable")

    store = BrokenStore(tmp_path / "objects", store_id="test-store")

    with conn.cursor() as cur:
        run = _new_run(cur, "JP", T1)
        _store_and_record(
            cur, store, run, b'{"marker": "stuck", "id": "%s"}' % uuid.uuid4().hex.encode(),
            min_plan="Standard",
        )

    outcome = PurgeRunner(conn, {"test-store": store}).run(
        provider_id="jquants",
        reason="cancellation",
        requested_by="pytest",
        dry_run=False,
    )

    assert outcome.failed == 1
    assert outcome.complete is False
    assert "unreachable" in outcome.failures[0][1]

    with conn.cursor() as cur:
        cur.execute(
            "select failed from market.outstanding_purge_obligations where purge_request_id = %s",
            (outcome.purge_request_id,),
        )
        assert cur.fetchone()[0] == 1
    conn.rollback()


def test_an_object_in_an_unconfigured_store_is_a_failure_not_a_success(conn, tmp_path):
    store = LocalObjectStore(tmp_path / "objects", store_id="test-store")

    with conn.cursor() as cur:
        run = _new_run(cur, "JP", T1)
        _store_and_record(
            cur, store, run, b'{"marker": "orphan", "id": "%s"}' % uuid.uuid4().hex.encode(),
            min_plan="Standard",
        )

    # The runner is handed a different store id than the manifest records.
    outcome = PurgeRunner(conn, {"some-other-store": store}).run(
        provider_id="jquants",
        reason="cancellation",
        requested_by="pytest",
        dry_run=False,
    )

    assert outcome.failed == 1
    assert outcome.complete is False
    assert "no store configured" in outcome.failures[0][1]
    conn.rollback()
