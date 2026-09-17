"""Phase 12: the improvement surfaces, and the fact that they are empty.

The assertion that matters is the second group: every view returns nothing, and
nothing in this schema is seeded. A dashboard that fills itself with synthetic
numbers while waiting for real ones is worse than an empty one, because the
caveat is forgotten long before the numbers are replaced.
"""

from __future__ import annotations

import os
from datetime import date, timedelta

import pytest

psycopg2 = pytest.importorskip("psycopg2")

DSN = os.environ.get("SURGE_TEST_DATABASE_URL")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not DSN, reason="SURGE_TEST_DATABASE_URL is not set"),
]

VIEWS = [
    "ui.teacher_data_summary",
    "ui.miss_breakdown",
    "ui.route_performance",
    "ui.material_driver_performance",
    "ui.concept_performance",
    "ui.llm_judgement_audit",
    "ui.model_version_comparison",
    "ui.walk_forward_results",
    "ui.calibration_diagnostics",
    "ui.promotion_audit",
]

LAB_TABLES = [
    "research.model_versions",
    "research.walk_forward_runs",
    "research.fold_results",
    "research.calibration_bins",
    "research.promotion_attempts",
    "research.episode_attribution",
]

TEACHER_TABLES = [
    "labels.objective_labels",
    "labels.interpretive_labels",
    "labels.pipeline_miss_records",
    "labels.datasets",
]

START = date(2024, 1, 1)


@pytest.fixture()
def conn():
    connection = psycopg2.connect(DSN)
    connection.autocommit = False
    try:
        yield connection
    finally:
        connection.rollback()
        connection.close()


@pytest.mark.parametrize("view", VIEWS)
def test_every_improvement_view_reads(conn, view):
    """A view that only works once it has rows is a view nobody has run."""

    with conn.cursor() as cur:
        cur.execute(f"select * from {view} limit 1")
        cur.fetchall()


@pytest.mark.parametrize("view", VIEWS)
def test_every_improvement_view_is_empty(conn, view):
    """Phase 12 ships with nothing in it, and that is the point."""

    with conn.cursor() as cur:
        cur.execute(f"select count(*) from {view}")
        assert cur.fetchone()[0] == 0, f"{view} has rows; nothing should be seeded here"


@pytest.mark.parametrize("table", LAB_TABLES)
def test_nothing_in_the_model_lab_is_seeded(conn, table):
    with conn.cursor() as cur:
        cur.execute(f"select count(*) from {table}")
        assert cur.fetchone()[0] == 0, f"{table} is seeded"


@pytest.mark.parametrize("table", TEACHER_TABLES)
def test_the_teacher_tables_are_empty_too(conn, table):
    """The 0-start. The first production teacher row comes from a real episode
    whose outcome was decided, and from nothing else - no fixture, no synthetic
    run, no historical replay."""

    with conn.cursor() as cur:
        cur.execute(f"select count(*) from {table}")
        assert cur.fetchone()[0] == 0, f"{table} has rows"


# ------------------------------------------------------- the guards that hold


def _model(cur, version="v1"):
    cur.execute(
        "insert into research.model_versions (model_version, description) values (%s, 'probe')"
        " returning model_version",
        (version,),
    )
    return cur.fetchone()[0]


def _run(cur, version="v1", purge=30):
    cur.execute(
        """
        insert into research.walk_forward_runs
          (model_version, fold_count, purge_days, metric_name)
        values (%s, 1, %s, 'balanced_accuracy') returning run_id
        """,
        (version, purge),
    )
    return cur.fetchone()[0]


def test_a_walk_forward_run_cannot_shorten_the_purge_gap(conn):
    """A gap under the horizon trains on answers the test window contains."""

    with conn.cursor() as cur:
        _model(cur)
        with pytest.raises(psycopg2.errors.CheckViolation, match="purge_covers_the_horizon"):
            _run(cur, purge=5)


def test_a_fold_whose_test_window_precedes_its_training_is_refused(conn):
    with conn.cursor() as cur:
        _model(cur)
        run_id = _run(cur)

        with pytest.raises(psycopg2.errors.CheckViolation, match="fold_is_time_ordered"):
            cur.execute(
                """
                insert into research.fold_results
                  (run_id, fold_index, train_start, train_end, test_start, test_end,
                   labels_in_train, labels_in_test)
                values (%s, 0, %s, %s, %s, %s, 100, 20)
                """,
                (run_id, START, START + timedelta(days=200), START, START + timedelta(days=60)),
            )


def test_a_refused_promotion_must_say_why(conn):
    """A gate that records a refusal without a reason is a gate nobody can
    argue with, which is the same as no gate."""

    with conn.cursor() as cur:
        _model(cur)
        with pytest.raises(psycopg2.errors.CheckViolation, match="refusal_has_reasons"):
            cur.execute(
                """
                insert into research.promotion_attempts
                  (challenger_version, promoted, gate_version)
                values ('v1', false, 'promotion-gate-1.0.0')
                """
            )


def test_a_successful_promotion_cannot_carry_refusal_reasons(conn):
    with conn.cursor() as cur:
        _model(cur)
        with pytest.raises(psycopg2.errors.CheckViolation, match="success_has_no_reasons"):
            cur.execute(
                """
                insert into research.promotion_attempts
                  (challenger_version, promoted, gate_version, refusal_reasons)
                values ('v1', true, 'promotion-gate-1.0.0', array['no calibration'])
                """
            )


def test_a_refusal_with_its_reasons_is_recorded(conn):
    with conn.cursor() as cur:
        _model(cur)
        cur.execute(
            """
            insert into research.promotion_attempts
              (challenger_version, promoted, gate_version, refusal_reasons)
            values ('v1', false, 'promotion-gate-1.0.0',
                    array['no live-verified labels', 'no calibration', 'no human approval'])
            returning attempt_id
            """
        )
        attempt_id = cur.fetchone()[0]

        cur.execute(
            "select promoted, array_length(refusal_reasons, 1) from ui.promotion_audit"
            " where attempt_id = %s",
            (attempt_id,),
        )
        promoted, reasons = cur.fetchone()

        assert promoted is False
        assert reasons == 3


def test_a_model_is_not_marked_live_verified_by_default(conn):
    """Until it has been trained on labels from real episodes, it has not been."""

    with conn.cursor() as cur:
        _model(cur, "v-default")
        cur.execute(
            "select trained_on_live_verified_labels from research.model_versions"
            " where model_version = 'v-default'"
        )
        assert cur.fetchone()[0] is False


def test_calibration_bins_must_be_a_real_range(conn):
    with conn.cursor() as cur:
        _model(cur)
        run_id = _run(cur)

        with pytest.raises(psycopg2.errors.CheckViolation, match="bin_is_a_range"):
            cur.execute(
                """
                insert into research.calibration_bins
                  (run_id, bin_lower, bin_upper, sample_count)
                values (%s, 0.8, 0.2, 10)
                """,
                (run_id,),
            )


def test_the_production_worker_cannot_write_the_model_lab(conn):
    """Teacher data and model evaluation are research output. The production
    worker reads them; the research worker builds them."""

    with conn.cursor() as cur:
        cur.execute(
            """
            select table_name, string_agg(privilege_type, ',' order by privilege_type)
              from information_schema.role_table_grants
             where grantee = 'surge_worker_prod' and table_schema = 'research'
               and table_name = any(%s)
             group by table_name
            """,
            ([t.split(".", 1)[1] for t in LAB_TABLES],),
        )
        grants = dict(cur.fetchall())

        # The one exception: the pipeline records what drove an episode.
        assert grants.pop("episode_attribution", None) == "INSERT,SELECT"
        for table, privileges in grants.items():
            assert privileges == "SELECT", f"{table} is writable by the production worker"
