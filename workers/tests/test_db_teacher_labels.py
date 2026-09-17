"""The label consistency rules, checked against the database.

A label that contradicts the measured path is not a difference of opinion; it is
a label about a different episode. These are the cases where an unreviewed guess
would otherwise become ground truth.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, date, datetime, timedelta

import pytest

psycopg2 = pytest.importorskip("psycopg2")

# A PREDICTED observation must point at a real episode - the constraint says so,
# and a teacher row claiming a prediction that does not exist is the thing it
# is there to prevent. Reusing the entry fixtures keeps one definition of what
# a well-formed episode looks like.
from test_db_entry_lifecycle import _episode as _make_episode  # noqa: E402

DSN = os.environ.get("SURGE_TEST_DATABASE_URL")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not DSN, reason="SURGE_TEST_DATABASE_URL is not set"),
]

CUTOFF = datetime(2026, 9, 17, 2, 0, tzinfo=UTC)
AS_OF = date(2026, 9, 17)
DIGEST = "a" * 64


@pytest.fixture()
def conn():
    connection = psycopg2.connect(DSN)
    connection.autocommit = False
    try:
        yield connection
    finally:
        connection.rollback()
        connection.close()


def _objective(cur, **overrides) -> tuple[str, str]:
    security_id = overrides.pop("security_id", str(uuid.uuid4()))
    kind = overrides.get("observation_kind", "PREDICTED")
    episode_id = overrides.pop(
        "episode_id", _make_episode(cur, security_id) if kind == "PREDICTED" else None
    )
    params = {
        "security_id": security_id,
        "as_of_date": AS_OF,
        "observation_kind": kind,
        "episode_id": episode_id,
        "path_resolution": "TARGET_FIRST",
        "primary_episode_outcome": "TARGET_HIT",
        "hit_20": True,
        "hit_20_before_failure": True,
        "failure_before_20": False,
        "label_version": "objective-labels-1.0.0",
    }
    params.update(overrides)
    cur.execute(
        """
        insert into labels.objective_labels (
          security_id, as_of_date, observation_kind, episode_id, path_resolution,
          primary_episode_outcome, hit_20, hit_20_before_failure, failure_before_20, label_version
        ) values (
          %(security_id)s, %(as_of_date)s, %(observation_kind)s::labels.observation_kind,
          %(episode_id)s, %(path_resolution)s::prod.path_resolution,
          %(primary_episode_outcome)s::prod.episode_close_reason,
          %(hit_20)s, %(hit_20_before_failure)s, %(failure_before_20)s, %(label_version)s
        ) returning objective_id, security_id
        """,
        params,
    )
    return cur.fetchone()


def _label(cur, objective_id, security_id, label, **overrides):
    params = {
        "objective_id": objective_id,
        "security_id": security_id,
        "label": label,
        "labeler_model_version": "rule-based-labeler-1.0.0",
        "confidence": 0.9,
        "evidence": '{"why": "fixture"}',
        "information_cutoff_at": CUTOFF,
        "input_sha256": DIGEST,
    }
    params.update(overrides)
    cur.execute(
        """
        insert into labels.interpretive_labels (
          objective_id, security_id, label, labeler_model_version, confidence, evidence,
          information_cutoff_at, input_sha256
        ) values (
          %(objective_id)s, %(security_id)s, %(label)s::labels.interpretive_label,
          %(labeler_model_version)s, %(confidence)s, %(evidence)s::jsonb,
          %(information_cutoff_at)s, %(input_sha256)s
        ) returning label_id
        """,
        params,
    )
    return cur.fetchone()[0]


# ------------------------------------------------------------ consistency


def test_a_success_label_on_a_target_hit_is_accepted(conn):
    with conn.cursor() as cur:
        objective_id, security_id = _objective(cur)
        assert _label(cur, objective_id, security_id, "PREDICTIVE_SUCCESS")


def test_a_success_label_requires_the_target_to_have_been_reached_first(conn):
    with conn.cursor() as cur:
        objective_id, security_id = _objective(
            cur,
            path_resolution="FAILURE_FIRST",
            primary_episode_outcome="INITIAL_FAILURE_HIT",
            hit_20_before_failure=False,
            failure_before_20=True,
        )
        with pytest.raises(psycopg2.errors.RaiseException, match="requires the target"):
            _label(cur, objective_id, security_id, "PREDICTIVE_SUCCESS")


def test_no_success_label_after_an_invalidated_thesis(conn):
    """RF-24. The price reaching the target afterwards is counterfactual and
    does not make the call right."""

    with conn.cursor() as cur:
        objective_id, security_id = _objective(
            cur, path_resolution="TARGET_FIRST", primary_episode_outcome="THESIS_INVALIDATED"
        )
        with pytest.raises(psycopg2.errors.RaiseException, match="does not make the call right"):
            _label(cur, objective_id, security_id, "PREDICTIVE_SUCCESS")


@pytest.mark.parametrize(
    ("resolution", "outcome"),
    [
        ("AMBIGUOUS_PATH", "AMBIGUOUS_PATH"),
        ("UNRESOLVED_MISSING_DATA", "UNRESOLVED_MISSING_DATA"),
        (None, "CORPORATE_ACTION_SUSPECTED"),
    ],
)
def test_an_unresolved_path_supports_no_judgement(conn, resolution, outcome):
    with conn.cursor() as cur:
        objective_id, security_id = _objective(
            cur,
            path_resolution=resolution,
            primary_episode_outcome=outcome,
            hit_20_before_failure=None,
            failure_before_20=None,
        )
        with pytest.raises(psycopg2.errors.RaiseException, match="was not resolved"):
            _label(cur, objective_id, security_id, "FALSE_POSITIVE")


def test_priced_in_error_cannot_describe_an_episode_that_reached_its_target(conn):
    with conn.cursor() as cur:
        objective_id, security_id = _objective(cur)
        with pytest.raises(psycopg2.errors.RaiseException, match="did not reach its target"):
            _label(cur, objective_id, security_id, "PRICED_IN_ERROR")


def test_a_miss_label_cannot_be_attached_to_an_episode(conn):
    with conn.cursor() as cur:
        objective_id, security_id = _objective(cur)
        with pytest.raises(psycopg2.errors.RaiseException, match="never entered"):
            _label(cur, objective_id, security_id, "ACTIONABLE_FALSE_NEGATIVE")


def test_a_prediction_label_cannot_be_attached_to_something_never_predicted(conn):
    with conn.cursor() as cur:
        objective_id, security_id = _objective(
            cur, observation_kind="ELIGIBLE_ONLY", path_resolution=None,
            primary_episode_outcome=None,
        )
        with pytest.raises(psycopg2.errors.RaiseException, match="no prediction was made"):
            _label(cur, objective_id, security_id, "FALSE_POSITIVE")


def test_a_miss_label_needs_the_price_to_have_moved(conn):
    with conn.cursor() as cur:
        objective_id, security_id = _objective(
            cur,
            observation_kind="ELIGIBLE_ONLY",
            path_resolution=None,
            primary_episode_outcome=None,
            hit_20=False,
            hit_20_before_failure=None,
            failure_before_20=None,
        )
        with pytest.raises(psycopg2.errors.RaiseException, match="did reach"):
            _label(cur, objective_id, security_id, "ACTIONABLE_FALSE_NEGATIVE")


def test_a_miss_label_on_a_security_that_moved_is_accepted(conn):
    with conn.cursor() as cur:
        objective_id, security_id = _objective(
            cur,
            observation_kind="ELIGIBLE_ONLY",
            path_resolution=None,
            primary_episode_outcome=None,
            hit_20=True,
            hit_20_before_failure=None,
            failure_before_20=None,
        )
        assert _label(cur, objective_id, security_id, "ACTIONABLE_FALSE_NEGATIVE")


# ---------------------------------------------------------- append-only


def test_a_judgement_cannot_be_rewritten(conn):
    with conn.cursor() as cur:
        objective_id, security_id = _objective(cur)
        label_id = _label(cur, objective_id, security_id, "PREDICTIVE_SUCCESS")

        with pytest.raises(psycopg2.errors.RaiseException, match="supersedes this one"):
            cur.execute(
                "update labels.interpretive_labels set label = 'FALSE_POSITIVE' where label_id = %s",
                (label_id,),
            )


def test_review_may_be_added_afterwards(conn):
    with conn.cursor() as cur:
        objective_id, security_id = _objective(cur)
        label_id = _label(cur, objective_id, security_id, "PREDICTIVE_SUCCESS")

        cur.execute(
            """
            update labels.interpretive_labels
               set human_review_status = 'APPROVED', reviewed_by = 'a reviewer', reviewed_at = %s
             where label_id = %s
            """,
            (CUTOFF, label_id),
        )
        cur.execute(
            "select human_review_status::text from labels.interpretive_labels where label_id = %s",
            (label_id,),
        )
        assert cur.fetchone()[0] == "APPROVED"


def test_a_review_needs_a_reviewer(conn):
    """A review status with nobody attached is a claim that someone looked."""

    with conn.cursor() as cur:
        objective_id, security_id = _objective(cur)
        with pytest.raises(psycopg2.errors.CheckViolation, match="reviewed_has_a_reviewer"):
            cur.execute(
                """
                insert into labels.interpretive_labels (
                  objective_id, security_id, label, labeler_model_version, evidence,
                  information_cutoff_at, input_sha256, human_review_status
                ) values (%s, %s, 'PREDICTIVE_SUCCESS', 'v1', '{}'::jsonb, %s, %s, 'APPROVED')
                """,
                (objective_id, security_id, CUTOFF, DIGEST),
            )


def test_a_judgement_cannot_be_deleted(conn):
    with conn.cursor() as cur:
        objective_id, security_id = _objective(cur)
        label_id = _label(cur, objective_id, security_id, "PREDICTIVE_SUCCESS")

        with pytest.raises(psycopg2.errors.RaiseException, match="append-only"):
            cur.execute(
                "delete from labels.interpretive_labels where label_id = %s", (label_id,)
            )


# ------------------------------------------------------------- the rest


def test_an_unresolved_path_may_not_claim_an_order(conn):
    with conn.cursor() as cur:
        with pytest.raises(psycopg2.errors.CheckViolation, match="unresolved_is_null"):
            _objective(
                cur,
                path_resolution="AMBIGUOUS_PATH",
                primary_episode_outcome="AMBIGUOUS_PATH",
                hit_20_before_failure=True,
            )


def test_a_pipeline_miss_must_actually_be_late(conn):
    """Published in time, available too late. That is the definition, and a row
    that does not fit it is describing something else."""

    with conn.cursor() as cur:
        objective_id, security_id = _objective(
            cur, observation_kind="ELIGIBLE_ONLY", path_resolution=None,
            primary_episode_outcome=None,
        )
        with pytest.raises(psycopg2.errors.CheckViolation, match="actually_late"):
            cur.execute(
                """
                insert into labels.pipeline_miss_records (
                  objective_id, security_id, source_published_at, available_to_model_at,
                  information_cutoff_at
                ) values (%s, %s, %s, %s, %s)
                """,
                (
                    objective_id,
                    security_id,
                    CUTOFF - timedelta(hours=2),
                    CUTOFF - timedelta(hours=1),
                    CUTOFF,
                ),
            )


def test_a_genuine_pipeline_miss_is_accepted(conn):
    with conn.cursor() as cur:
        objective_id, security_id = _objective(
            cur, observation_kind="ELIGIBLE_ONLY", path_resolution=None,
            primary_episode_outcome=None,
        )
        cur.execute(
            """
            insert into labels.pipeline_miss_records (
              objective_id, security_id, source_published_at, available_to_model_at,
              information_cutoff_at, delay_seconds, cause
            ) values (%s, %s, %s, %s, %s, 18000, 'collector outage')
            returning source_timestamp_trusted
            """,
            (objective_id, security_id, CUTOFF - timedelta(hours=2), CUTOFF + timedelta(hours=3), CUTOFF),
        )
        # Defaults to untrusted: the publisher's own claim about when it published.
        assert cur.fetchone()[0] is False


def test_a_policy_with_fewer_than_three_classes_is_refused_by_the_database(conn):
    with conn.cursor() as cur:
        with pytest.raises(psycopg2.errors.CheckViolation, match="more_than_two_classes"):
            cur.execute(
                """
                insert into labels.admission_policies (policy_version, description, admitted_labels)
                values ('binary-1.0.0', 'two classes',
                        array['PREDICTIVE_SUCCESS','FALSE_POSITIVE']::labels.interpretive_label[])
                """
            )


def test_the_opening_policy_admits_no_pipeline_or_shock_labels(conn):
    """None of them is a prediction-model failure."""

    with conn.cursor() as cur:
        cur.execute(
            "select admitted_labels::text[] from labels.admission_policies where policy_version = 'admission-1.0.0'"
        )
        admitted = set(cur.fetchone()[0])

        assert "ACTIONABLE_FALSE_NEGATIVE" in admitted
        assert "PIPELINE_MISSED_ACTIONABLE_SIGNAL" not in admitted
        assert "OUT_OF_SCOPE_SHOCK" not in admitted
        assert "OUT_OF_SCOPE_LATE" not in admitted


# ------------------------------------------- lineage as a condition of admission


def _dataset(cur, purpose="PRODUCTION_TRAINING", name=None):
    cur.execute(
        """
        insert into labels.datasets (name, policy_version, knowledge_cutoff, purpose)
        values (%s, 'admission-1.1.0', %s, %s::labels.dataset_purpose)
        returning dataset_id
        """,
        (name or f"lineage-probe-{uuid.uuid4()}", CUTOFF, purpose),
    )
    return cur.fetchone()[0]


def _context(cur, objective_id, **overrides):
    """An input snapshot for one observation.

    The episode, setup and attempt are read from the observation rather than
    passed in: `labels.check_observation_context` refuses a context that points
    at a different one, which is the right rule and means a test that hard-coded
    nulls here would fail at the wrong step and prove nothing about admission.
    """

    cur.execute("select run_id from pipeline.runs limit 1")
    row = cur.fetchone()
    run_id = row[0] if row else None
    cur.execute(
        "select episode_id, setup_id, entry_attempt_id from labels.objective_labels "
        "where objective_id = %s",
        (objective_id,),
    )
    episode_id, setup_id, entry_attempt_id = cur.fetchone()
    params = {
        "objective_id": objective_id,
        "information_cutoff_at": CUTOFF,
        "production_run_id": run_id,
        "universe_run_id": run_id,
        "market_data_run_id": run_id,
        "feature_version": "features-1.0.0",
        "feature_snapshot_ref": "probe/ref",
        "coverage_snapshot": '{"materials": 1.0}',
        "episode_id": episode_id,
        "setup_id": setup_id,
        "entry_attempt_id": entry_attempt_id,
        "input_bundle_sha256": DIGEST,
    }
    params.update(overrides)
    cur.execute(
        """
        insert into labels.observation_contexts (
          objective_id, information_cutoff_at, production_run_id, universe_run_id,
          market_data_run_id, feature_version, feature_snapshot_ref, coverage_snapshot,
          episode_id, setup_id, entry_attempt_id, input_bundle_sha256
        ) values (
          %(objective_id)s, %(information_cutoff_at)s, %(production_run_id)s,
          %(universe_run_id)s, %(market_data_run_id)s, %(feature_version)s,
          %(feature_snapshot_ref)s, %(coverage_snapshot)s::jsonb,
          %(episode_id)s, %(setup_id)s, %(entry_attempt_id)s, %(input_bundle_sha256)s
        )
        """,
        params,
    )


def _admit(cur, dataset_id, label_id, admitted=True):
    cur.execute(
        "insert into labels.dataset_members (dataset_id, label_id, admitted) values (%s, %s, %s)",
        (dataset_id, label_id, admitted),
    )


def test_a_training_set_refuses_a_label_whose_inputs_were_never_recorded(conn):
    """Teacher data is input snapshot + decision + outcome. Two of the three is
    not teacher data, and the database is the last place that can say so."""

    with conn.cursor() as cur:
        objective_id, security_id = _objective(cur)
        label_id = _label(cur, objective_id, security_id, "PREDICTIVE_SUCCESS")

        with pytest.raises(psycopg2.errors.RaiseException, match="no observation context"):
            _admit(cur, _dataset(cur), label_id)


def test_a_rejected_member_needs_no_lineage(conn):
    """The reason a label was left out may well be that its lineage is missing;
    refusing to record that would erase the finding."""

    with conn.cursor() as cur:
        objective_id, security_id = _objective(cur)
        label_id = _label(cur, objective_id, security_id, "PREDICTIVE_SUCCESS")

        _admit(cur, _dataset(cur), label_id, admitted=False)


def test_a_research_set_may_admit_a_label_with_no_lineage(conn):
    with conn.cursor() as cur:
        objective_id, security_id = _objective(cur)
        label_id = _label(cur, objective_id, security_id, "PREDICTIVE_SUCCESS")

        _admit(cur, _dataset(cur, purpose="RESEARCH_ONLY"), label_id)


def test_a_predicted_example_needs_the_attempt_it_came_from(conn):
    """A context complete enough for an ELIGIBLE_ONLY observation is not complete
    for a PREDICTED one: the latter has a decision behind it, and which attempt
    that was is part of what the example is.

    The episode is not tested here because it cannot be missing: the objective
    row's own CHECK requires one for PREDICTED and the context trigger requires
    the context to name the same one. The lineage function still asks, because
    defence that only holds while two other rules hold is not defence."""

    with conn.cursor() as cur:
        objective_id, security_id = _objective(cur)
        label_id = _label(cur, objective_id, security_id, "PREDICTIVE_SUCCESS")
        _context(cur, objective_id)

        with pytest.raises(psycopg2.errors.RaiseException, match="no entry_attempt_id"):
            _admit(cur, _dataset(cur), label_id)


def test_an_incomplete_context_names_what_is_missing(conn):
    with conn.cursor() as cur:
        objective_id, security_id = _objective(
            cur,
            observation_kind="ELIGIBLE_ONLY",
            episode_id=None,
            primary_episode_outcome=None,
        )
        label_id = _label(cur, objective_id, security_id, "ACTIONABLE_FALSE_NEGATIVE")
        _context(cur, objective_id, coverage_snapshot=None, feature_snapshot_ref=None)

        with pytest.raises(psycopg2.errors.RaiseException) as raised:
            _admit(cur, _dataset(cur), label_id)

        message = str(raised.value)
        assert "no coverage_snapshot" in message
        assert "no snapshot or reference" in message


def test_a_complete_context_is_admitted(conn):
    with conn.cursor() as cur:
        objective_id, security_id = _objective(
            cur,
            observation_kind="ELIGIBLE_ONLY",
            episode_id=None,
            primary_episode_outcome=None,
        )
        label_id = _label(cur, objective_id, security_id, "ACTIONABLE_FALSE_NEGATIVE")
        _context(cur, objective_id)

        _admit(cur, _dataset(cur), label_id)

        cur.execute("select count(*) from labels.dataset_members where label_id = %s", (label_id,))
        assert cur.fetchone()[0] == 1


def test_an_empty_coverage_snapshot_is_not_a_coverage_snapshot(conn):
    """`{}` records that somebody wrote a field, not what the collectors had."""

    with conn.cursor() as cur:
        objective_id, security_id = _objective(
            cur,
            observation_kind="ELIGIBLE_ONLY",
            episode_id=None,
            primary_episode_outcome=None,
        )
        label_id = _label(cur, objective_id, security_id, "ACTIONABLE_FALSE_NEGATIVE")
        _context(cur, objective_id, coverage_snapshot="{}")

        with pytest.raises(psycopg2.errors.RaiseException, match="no coverage_snapshot"):
            _admit(cur, _dataset(cur), label_id)
