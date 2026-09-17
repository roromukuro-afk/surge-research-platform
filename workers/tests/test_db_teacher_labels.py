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
