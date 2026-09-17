"""Phase 2.1 database behaviour: licence guard, manifest, purge, FX, revisions.

These run against a real Postgres with the migrations applied. CI provides one;
locally they are skipped unless SURGE_TEST_DATABASE_URL is set.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, date, datetime, timedelta

import pytest

psycopg2 = pytest.importorskip("psycopg2")

from test_db_master_semantics import T1, _new_run  # noqa: E402

DSN = os.environ.get("SURGE_TEST_DATABASE_URL")
WORKER_DSN = os.environ.get("SURGE_TEST_WORKER_DSN")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not DSN, reason="SURGE_TEST_DATABASE_URL is not set"),
]

READ_ONLY = psycopg2.errors.ReadOnlySqlTransaction
DENIED = psycopg2.errors.InsufficientPrivilege


@pytest.fixture()
def conn():
    connection = psycopg2.connect(DSN)
    connection.autocommit = False
    try:
        yield connection
    finally:
        connection.rollback()
        connection.close()


def _store_object(
    cur,
    *,
    run_id,
    provider="jquants",
    dataset="JQ_EQ_BARS_DAILY",
    policy="jquants-2026-09-16",
    plan="Standard",
    min_plan="Standard",
    suffix: str | None = None,
) -> str:
    digest = (suffix or uuid.uuid4().hex).ljust(64, "0")[:64]
    key = f"raw/{provider}/{dataset}/{digest}.json"
    cur.execute(
        """
        insert into market.raw_objects (
          object_key, store_id, provider_id, dataset_key, license_policy_version,
          entitlement_plan, required_min_plan, sha256, bytes, content_type,
          request_url, observed_at, available_at, availability_basis, run_id
        ) values (%s, 'test', %s, %s, %s, %s, %s, %s, 10, 'application/json',
                  'https://example.invalid/fixture', %s, %s, 'OBSERVED_NOW', %s)
        """,
        (key, provider, dataset, policy, plan, min_plan, digest, T1, T1, str(run_id)),
    )
    return key


# ------------------------------------------------------------------- licence
def test_the_licence_guard_refuses_what_the_terms_refuse(conn):
    with conn.cursor() as cur:
        # J-Quants: private use by one person. Showing it to anyone is outside that.
        for action in ("PUBLIC_DISPLAY", "THIRD_PARTY_ACCESS", "COMMERCIAL_USE", "ACADEMIC_USE"):
            with pytest.raises(DENIED, match="does not permit"):
                cur.execute(
                    "select market.assert_license_allows('jquants', 'JQ_EQ_BARS_DAILY', %s)", (action,)
                )
            conn.rollback()

        # The ECB publishes for anyone to use, with attribution.
        cur.execute("select market.assert_license_allows('ecb', 'ECB_EXR_DAILY', 'PUBLIC_DISPLAY')")
        cur.execute("select market.assert_license_allows('openfigi', 'OPENFIGI_MAPPING', 'RAW_REDISTRIBUTION')")
    conn.rollback()


def test_an_unrecorded_dataset_is_prohibited_not_unrestricted(conn):
    with conn.cursor() as cur, pytest.raises(DENIED, match="no licence policy"):
        cur.execute("select market.assert_license_allows('nobody', 'NOTHING', 'PUBLIC_DISPLAY')")
    conn.rollback()


def test_eodhd_deletion_obligations_are_recorded_as_unspecified(conn):
    """The terms contain no deletion clause. Absence is not permission."""

    with conn.cursor() as cur:
        cur.execute(
            """
            select delete_on_cancel, delete_on_downgrade
            from market.provider_license_policies
            where provider_id = 'eodhd' and dataset_key = 'EODHD_US_EOD_BULK' and effective_to is null
            """
        )
        assert cur.fetchone() == ("NOT_SPECIFIED", "NOT_SPECIFIED")

        cur.execute(
            """
            select delete_on_cancel, delete_on_downgrade
            from market.provider_license_policies
            where provider_id = 'jquants' and dataset_key = 'JQ_EQ_BARS_DAILY' and effective_to is null
            """
        )
        assert cur.fetchone() == ("REQUIRED", "REQUIRED")
    conn.rollback()


def test_a_licence_policy_cannot_be_edited_or_deleted(conn):
    with conn.cursor() as cur, pytest.raises(READ_ONLY, match="append-only"):
        cur.execute(
            "delete from market.provider_license_policies where provider_id = 'jquants' "
            "and dataset_key = 'JQ_EQ_BARS_DAILY'"
        )
    conn.rollback()

    with conn.cursor() as cur, pytest.raises(READ_ONLY, match="immutable apart from effective_to"):
        cur.execute(
            "update market.provider_license_policies set public_display_allowed = 'ALLOWED' "
            "where provider_id = 'jquants' and dataset_key = 'JQ_EQ_BARS_DAILY' and effective_to is null"
        )
    conn.rollback()


def test_superseding_a_policy_is_the_one_permitted_update(conn):
    with conn.cursor() as cur:
        cur.execute(
            "update market.provider_license_policies set effective_to = clock_timestamp() "
            "where provider_id = 'jquants' and dataset_key = 'JQ_EQ_MASTER' and effective_to is null"
        )
        assert cur.rowcount == 1
        cur.execute("select * from market.current_license_policy('jquants', 'JQ_EQ_MASTER')")
        assert cur.fetchone()[0] is None, "a superseded policy leaves no current reading"
    conn.rollback()


# ------------------------------------------------------------------ manifest
def test_a_manifest_row_is_immutable_apart_from_its_purge_columns(conn):
    with conn.cursor() as cur:
        run = _new_run(cur, "JP", T1)
        key = _store_object(cur, run_id=run)

        with pytest.raises(READ_ONLY, match="immutable apart from its purge columns"):
            cur.execute("update market.raw_objects set bytes = 99 where object_key = %s", (key,))
    conn.rollback()

    with conn.cursor() as cur:
        run = _new_run(cur, "JP", T1)
        key = _store_object(cur, run_id=run)
        with pytest.raises(READ_ONLY, match="append-only"):
            cur.execute("delete from market.raw_objects where object_key = %s", (key,))
    conn.rollback()


def test_an_object_must_name_a_licence_policy_that_exists(conn):
    with conn.cursor() as cur:
        run = _new_run(cur, "JP", T1)
        with pytest.raises(psycopg2.errors.ForeignKeyViolation):
            _store_object(cur, run_id=run, policy="a-policy-nobody-recorded")
    conn.rollback()


# --------------------------------------------------------------------- purge
def test_a_downgrade_purges_only_what_the_retained_plan_does_not_cover(conn):
    with conn.cursor() as cur:
        run = _new_run(cur, "JP", T1)
        standard_only = _store_object(cur, run_id=run, min_plan="Standard", suffix="a" * 8)
        light_ok = _store_object(cur, run_id=run, min_plan="Light", suffix="b" * 8)

        cur.execute(
            "select market.open_purge_request('jquants', null, 'Light', 'downgrade test', 'pytest', true)"
        )
        request_id = cur.fetchone()[0]

        cur.execute(
            "select object_key from market.purge_targets where purge_request_id = %s", (request_id,)
        )
        targets = {row[0] for row in cur.fetchall()}
        assert standard_only in targets
        assert light_ok not in targets, "an object the retained plan still covers must not be purged"
    conn.rollback()


def test_cancelling_purges_everything_for_the_provider(conn):
    with conn.cursor() as cur:
        run = _new_run(cur, "JP", T1)
        keys = {
            _store_object(cur, run_id=run, min_plan="Light", suffix="c" * 8),
            _store_object(cur, run_id=run, min_plan="Standard", suffix="d" * 8),
        }
        other_provider = _store_object(
            cur, run_id=run, provider="ecb", dataset="ECB_EXR_DAILY",
            policy="ecb-2026-09-16", plan="public", min_plan="public", suffix="e" * 8,
        )

        cur.execute(
            "select market.open_purge_request('jquants', null, null, 'cancellation test', 'pytest', true)"
        )
        request_id = cur.fetchone()[0]
        cur.execute("select object_key from market.purge_targets where purge_request_id = %s", (request_id,))
        targets = {row[0] for row in cur.fetchall()}

        assert keys <= targets
        assert other_provider not in targets
    conn.rollback()


def test_a_dry_run_never_looks_like_a_completed_purge(conn):
    with conn.cursor() as cur:
        run = _new_run(cur, "JP", T1)
        key = _store_object(cur, run_id=run, suffix="f" * 8)

        cur.execute("select market.open_purge_request('jquants', null, null, 'rehearsal', 'pytest', true)")
        request_id = cur.fetchone()[0]
        cur.execute(
            "select market.record_purge_result(%s, %s, 'SKIPPED', 'dry run')", (request_id, key)
        )
        cur.execute("select market.complete_purge_request(%s)", (request_id,))
        summary = cur.fetchone()[0]

        assert summary["dry_run"] is True
        assert summary["complete"] is False, "a rehearsal is not an obligation met"

        cur.execute("select purged_at from market.raw_objects where object_key = %s", (key,))
        assert cur.fetchone()[0] is None

        cur.execute("select completed_at from market.purge_requests where purge_request_id = %s", (request_id,))
        assert cur.fetchone()[0] is None

        # and it stays on the list of obligations nobody has met
        cur.execute(
            "select count(*) from market.outstanding_purge_obligations where purge_request_id = %s",
            (request_id,),
        )
        assert cur.fetchone()[0] == 1
    conn.rollback()


def test_a_real_purge_marks_the_manifest_and_closes(conn):
    with conn.cursor() as cur:
        run = _new_run(cur, "JP", T1)
        key = _store_object(cur, run_id=run, suffix="1" * 8)

        cur.execute(
            "select market.open_purge_request('jquants', null, null, 'cancellation', 'pytest', false)"
        )
        request_id = cur.fetchone()[0]

        cur.execute("select object_key from market.purge_targets where purge_request_id = %s", (request_id,))
        for (target,) in cur.fetchall():
            cur.execute("select market.record_purge_result(%s, %s, 'DELETED', null)", (request_id, target))

        cur.execute("select market.purge_market_rows(%s)", (request_id,))
        cur.execute("select market.complete_purge_request(%s)", (request_id,))
        summary = cur.fetchone()[0]

        assert summary["complete"] is True
        assert summary["pending"] == 0

        cur.execute("select purged_at, purge_request_id from market.raw_objects where object_key = %s", (key,))
        purged_at, recorded_request = cur.fetchone()
        assert purged_at is not None, "the manifest row records that we no longer hold it"
        assert recorded_request == request_id
    conn.rollback()


def test_a_purge_cannot_be_un_recorded(conn):
    with conn.cursor() as cur:
        run = _new_run(cur, "JP", T1)
        key = _store_object(cur, run_id=run, suffix="2" * 8)
        cur.execute("select market.open_purge_request('jquants', null, null, 'x', 'pytest', false)")
        request_id = cur.fetchone()[0]
        cur.execute("select market.record_purge_result(%s, %s, 'DELETED', null)", (request_id, key))

        with pytest.raises(READ_ONLY, match="cannot be un-recorded"):
            cur.execute("update market.raw_objects set purged_at = null where object_key = %s", (key,))
    conn.rollback()


def test_purging_removes_the_rows_derived_from_the_object(conn):
    with conn.cursor() as cur:
        run = _new_run(cur, "JP", T1)
        key = _store_object(cur, run_id=run, suffix="3" * 8)
        cur.execute(
            """
            insert into market.daily_bars (
              provider_id, dataset_key, market_code, native_symbol, trade_date, currency,
              open, high, low, close, volume,
              open_basis, high_basis, low_basis, close_basis, volume_basis,
              observed_at, available_at, availability_basis, raw_object_key, run_id
            ) values ('jquants', 'JQ_EQ_BARS_DAILY', 'JP', '13010', date '2026-09-16', 'JPY',
                      1000, 1100, 990, 1050, 12345,
                      'RAW', 'RAW', 'RAW', 'RAW', 'RAW',
                      %s, %s, 'OBSERVED_NOW', %s, %s)
            """,
            (T1, T1, key, str(run)),
        )

        cur.execute("select market.open_purge_request('jquants', null, null, 'x', 'pytest', false)")
        request_id = cur.fetchone()[0]
        cur.execute("select market.purge_market_rows(%s)", (request_id,))
        counts = cur.fetchone()[0]

        assert counts["daily_bars"] >= 1
        cur.execute("select count(*) from market.daily_bars where raw_object_key = %s", (key,))
        assert cur.fetchone()[0] == 0
    conn.rollback()


# ------------------------------------------------------------------------ FX
def _insert_fx(cur, run, key, source_date: date, rate: str, available_at: datetime) -> None:
    cur.execute(
        """
        insert into market.fx_rates (
          provider_id, dataset_key, source_date, eur_usd, eur_jpy, derived_usd_jpy,
          derivation_method, rate_kind, observed_at, available_at, availability_basis,
          content_sha256, raw_object_key, run_id
        ) values ('ecb', 'ECB_EXR_DAILY', %s, 1.1537, 178.88, %s,
                  'USDJPY = JPY leg / USD leg', 'REFERENCE_RATE', %s, %s, 'OBSERVED_NOW',
                  %s, %s, %s)
        """,
        (source_date, rate, available_at, available_at, "a" * 64, key, str(run)),
    )


def test_the_fx_reader_never_returns_a_rate_the_cutoff_could_not_know(conn):
    with conn.cursor() as cur:
        run = _new_run(cur, "US", T1)
        key = _store_object(
            cur, run_id=run, provider="ecb", dataset="ECB_EXR_DAILY",
            policy="ecb-2026-09-16", plan="public", min_plan="public", suffix="4" * 8,
        )
        known = datetime(2026, 9, 15, 14, 0, tzinfo=UTC)
        later = datetime(2026, 9, 16, 14, 0, tzinfo=UTC)
        _insert_fx(cur, run, key, date(2026, 9, 15), "155.0", known)
        _insert_fx(cur, run, key, date(2026, 9, 16), "156.0", later)

        cutoff = datetime(2026, 9, 15, 20, 0, tzinfo=UTC)
        cur.execute("select * from market.usdjpy_as_of(%s)", (cutoff,))
        row = cur.fetchone()

        assert row[0] == date(2026, 9, 15)
        assert row[1] == pytest.approx(155.0)
        age = row[9]
        assert age == pytest.approx((cutoff - known).total_seconds())
    conn.rollback()


def test_the_fx_reader_reports_the_age_of_a_carried_forward_rate(conn):
    """A rate from before a closing day is not a same-day rate, and says so."""

    with conn.cursor() as cur:
        run = _new_run(cur, "US", T1)
        key = _store_object(
            cur, run_id=run, provider="ecb", dataset="ECB_EXR_DAILY",
            policy="ecb-2026-09-16", plan="public", min_plan="public", suffix="5" * 8,
        )
        friday = datetime(2026, 9, 11, 14, 0, tzinfo=UTC)
        _insert_fx(cur, run, key, date(2026, 9, 11), "154.0", friday)

        monday_cutoff = friday + timedelta(days=3)
        cur.execute("select * from market.usdjpy_as_of(%s)", (monday_cutoff,))
        row = cur.fetchone()
        assert row[0] == date(2026, 9, 11)
        assert row[9] == pytest.approx(timedelta(days=3).total_seconds())
    conn.rollback()


def test_a_changed_fx_observation_is_recorded_rather_than_overwritten(conn):
    with conn.cursor() as cur:
        run = _new_run(cur, "US", T1)
        key = _store_object(
            cur, run_id=run, provider="ecb", dataset="ECB_EXR_DAILY",
            policy="ecb-2026-09-16", plan="public", min_plan="public", suffix="6" * 8,
        )
        _insert_fx(cur, run, key, date(2026, 9, 16), "155.0", datetime(2026, 9, 16, 14, 0, tzinfo=UTC))

        cur.execute(
            "update market.fx_rates set derived_usd_jpy = 155.5, content_sha256 = %s "
            "where provider_id = 'ecb' and dataset_key = 'ECB_EXR_DAILY' and source_date = %s",
            ("b" * 64, date(2026, 9, 16)),
        )

        cur.execute(
            "select previous_values, new_values, previous_sha256, new_sha256 "
            "from market.fx_rate_revisions where source_date = %s",
            (date(2026, 9, 16),),
        )
        rows = cur.fetchall()
        assert len(rows) == 1
        previous, new, old_sha, new_sha = rows[0]
        assert float(previous["derived_usd_jpy"]) == pytest.approx(155.0)
        assert float(new["derived_usd_jpy"]) == pytest.approx(155.5)
        assert old_sha != new_sha

        cur.execute(
            "select revision from market.fx_rates where source_date = %s", (date(2026, 9, 16),)
        )
        assert cur.fetchone()[0] == 2
    conn.rollback()


def test_a_changed_bar_is_recorded_rather_than_overwritten(conn):
    """J-Quants corrects by silent overwrite; this is where that becomes visible."""

    with conn.cursor() as cur:
        run = _new_run(cur, "JP", T1)
        key = _store_object(cur, run_id=run, suffix="7" * 8)
        cur.execute(
            """
            insert into market.daily_bars (
              provider_id, dataset_key, market_code, native_symbol, trade_date, currency,
              open, high, low, close, volume,
              open_basis, high_basis, low_basis, close_basis, volume_basis,
              observed_at, available_at, availability_basis, raw_object_key, run_id
            ) values ('jquants', 'JQ_EQ_BARS_DAILY', 'JP', '99990', date '2026-09-16', 'JPY',
                      1000, 1100, 990, 1050, 12345,
                      'RAW', 'RAW', 'RAW', 'RAW', 'RAW',
                      %s, %s, 'OBSERVED_NOW', %s, %s)
            """,
            (T1, T1, key, str(run)),
        )
        cur.execute(
            "update market.daily_bars set close = 1060 where native_symbol = '99990' and trade_date = %s",
            (date(2026, 9, 16),),
        )

        cur.execute(
            "select previous_values, new_values from market.daily_bar_revisions where native_symbol = '99990'"
        )
        previous, new = cur.fetchone()
        assert float(previous["close"]) == pytest.approx(1050)
        assert float(new["close"]) == pytest.approx(1060)

        cur.execute("select revision from market.daily_bars where native_symbol = '99990'")
        assert cur.fetchone()[0] == 2
    conn.rollback()


def test_re_ingesting_an_identical_bar_is_not_a_revision(conn):
    with conn.cursor() as cur:
        run = _new_run(cur, "JP", T1)
        key = _store_object(cur, run_id=run, suffix="8" * 8)
        cur.execute(
            """
            insert into market.daily_bars (
              provider_id, dataset_key, market_code, native_symbol, trade_date, currency,
              open, high, low, close, volume,
              open_basis, high_basis, low_basis, close_basis, volume_basis,
              observed_at, available_at, availability_basis, raw_object_key, run_id
            ) values ('jquants', 'JQ_EQ_BARS_DAILY', 'JP', '99991', date '2026-09-16', 'JPY',
                      1000, 1100, 990, 1050, 12345,
                      'RAW', 'RAW', 'RAW', 'RAW', 'RAW',
                      %s, %s, 'OBSERVED_NOW', %s, %s)
            """,
            (T1, T1, key, str(run)),
        )
        # a later run confirms the same values from a newer fetch
        cur.execute(
            "update market.daily_bars set ingested_at = clock_timestamp() "
            "where native_symbol = '99991' and trade_date = %s",
            (date(2026, 9, 16),),
        )
        cur.execute("select count(*) from market.daily_bar_revisions where native_symbol = '99991'")
        assert cur.fetchone()[0] == 0
    conn.rollback()


# ------------------------------------------------------------------ privilege
@pytest.mark.skipif(not WORKER_DSN, reason="SURGE_TEST_WORKER_DSN is not set")
def test_the_ingestion_worker_cannot_purge():
    """The worker writes market data and cannot destroy it. That separation is the control."""

    worker = psycopg2.connect(WORKER_DSN)
    worker.autocommit = False
    try:
        for statement in (
            "select market.open_purge_request('jquants', null, null, 'x', 'worker', true)",
            "select market.purge_market_rows(1)",
            "delete from market.daily_bars where native_symbol = 'nothing'",
            "delete from market.raw_objects where object_key = 'nothing'",
        ):
            with worker.cursor() as cur, pytest.raises((DENIED, READ_ONLY)):
                cur.execute(statement)
            worker.rollback()
    finally:
        worker.rollback()
        worker.close()


def test_the_market_schema_is_covered_by_the_public_execute_guard(conn):
    """Adding a schema is how this guard gets bypassed, so assert the list itself."""

    with conn.cursor() as cur:
        cur.execute("select 'market' = any (pipeline.project_schemas())")
        assert cur.fetchone()[0] is True

        cur.execute(
            """
            select p.proname
            from pg_proc p join pg_namespace n on n.oid = p.pronamespace
            where n.nspname = 'market' and has_function_privilege('public', p.oid, 'execute')
            order by 1
            """
        )
        assert cur.fetchall() == []
    conn.rollback()
