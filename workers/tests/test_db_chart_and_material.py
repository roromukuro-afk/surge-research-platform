"""Phase 5 and 6 database behaviour.

The rules worth testing here are the ones that only exist as constraints: a
concept that cannot be enabled without a recorded failure, a macro relation that
cannot be stored without a mechanism, a route table that cannot accept
WEAK_ASSOCIATION, and a Stage 2 union that keeps both sides.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, date, datetime

import pytest

psycopg2 = pytest.importorskip("psycopg2")

DSN = os.environ.get("SURGE_TEST_DATABASE_URL")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not DSN, reason="SURGE_TEST_DATABASE_URL is not set"),
]

T0 = datetime(2026, 9, 17, 6, 0, tzinfo=UTC)
AS_OF = date(2026, 9, 17)


@pytest.fixture()
def conn():
    connection = psycopg2.connect(DSN)
    connection.autocommit = False
    try:
        yield connection
    finally:
        connection.rollback()
        connection.close()


def _run(cur, market="JP") -> uuid.UUID:
    run_id = uuid.uuid4()
    cur.execute(
        """
        insert into pipeline.runs (run_id, job_name, job_version, run_mode, market_code,
                                   idempotency_key, status, as_of_date, data_cutoff)
        values (%s, 'test_stage2', 'test', 'DEV', %s, %s, 'RUNNING', %s, %s)
        """,
        (str(run_id), market, f"test:{run_id}", AS_OF, T0),
    )
    return run_id


def _source(cur, *, discovery=True, verification=False) -> str:
    key = f"test_src_{uuid.uuid4().hex[:8]}"
    cur.execute(
        """
        insert into news.sources (source_key, name, scope, source_kind, access_mechanism,
                                  official_url, discovery_role, verification_role, enabled)
        values (%s, 'Test source', 'JP', 'GOVERNMENT', 'RSS', 'https://example.gov/', %s, %s, true)
        """,
        (key, discovery, verification),
    )
    return key


def _event(cur, *, scope="JP") -> str:
    cur.execute(
        """
        insert into material.events (event_key, event_type, scope, merge_version)
        values (%s, 'EARNINGS_REVISION', %s, 'event-merge-1.0.0')
        returning event_id
        """,
        (f"evt-{uuid.uuid4().hex[:12]}", scope),
    )
    return cur.fetchone()[0]


def _document(cur, source_key: str) -> str:
    cur.execute(
        """
        insert into news.documents (
          source_key, source_document_id, document_type, title,
          system_first_seen_at, ingested_at, available_to_model_at,
          content_sha256, body_storage
        ) values (%s, %s, 'PRESS_RELEASE', 'A release', %s, %s, %s, %s, 'METADATA_ONLY')
        returning document_id
        """,
        (source_key, f"doc-{uuid.uuid4().hex[:8]}", T0, T0, T0, uuid.uuid4().hex),
    )
    return cur.fetchone()[0]


# ------------------------------------------------------------------- material


def test_a_macro_relation_without_a_mechanism_is_refused(conn):
    with conn.cursor() as cur:
        event_id = _event(cur)
        with pytest.raises(psycopg2.errors.CheckViolation):
            cur.execute(
                """
                insert into material.entity_relations
                  (event_id, security_id, relation_type, confidence, extractor, extractor_version)
                values (%s, gen_random_uuid(), 'FX_EXPOSURE', 'PROVISIONAL', 'test', '1.0.0')
                """,
                (event_id,),
            )


def test_a_macro_relation_with_a_mechanism_is_accepted(conn):
    with conn.cursor() as cur:
        event_id = _event(cur)
        cur.execute(
            """
            insert into material.entity_relations
              (event_id, security_id, relation_type, confidence, causal_path, extractor, extractor_version)
            values (%s, gen_random_uuid(), 'FX_EXPOSURE', 'PROVISIONAL',
                    '70% of revenue is USD-denominated', 'test', '1.0.0')
            """,
            (event_id,),
        )
        assert cur.rowcount == 1


def test_a_direct_relation_needs_no_mechanism(conn):
    with conn.cursor() as cur:
        event_id = _event(cur)
        cur.execute(
            """
            insert into material.entity_relations
              (event_id, security_id, relation_type, confidence, extractor, extractor_version)
            values (%s, gen_random_uuid(), 'DIRECT_COMPANY', 'STRONG', 'test', '1.0.0')
            """,
            (event_id,),
        )
        assert cur.rowcount == 1


def test_a_source_cannot_verify_unless_it_is_registered_to(conn):
    with conn.cursor() as cur:
        source_key = _source(cur, discovery=True, verification=False)
        event_id = _event(cur)
        document_id = _document(cur, source_key)
        with pytest.raises(psycopg2.errors.RaiseException, match="not registered for verification"):
            cur.execute(
                """
                insert into material.event_sources
                  (event_id, document_id, source_role, available_to_model_at, source_key)
                values (%s, %s, 'VERIFICATION', %s, %s)
                """,
                (event_id, document_id, T0, source_key),
            )


def test_first_known_at_follows_the_earliest_source(conn):
    """Derived by trigger, because a hand-written knowledge time is a leak."""

    with conn.cursor() as cur:
        source_key = _source(cur)
        event_id = _event(cur)
        later = _document(cur, source_key)
        cur.execute(
            """
            insert into material.event_sources
              (event_id, document_id, source_role, available_to_model_at, source_key)
            values (%s, %s, 'DISCOVERY', %s, %s)
            """,
            (event_id, later, datetime(2026, 9, 17, 9, 0, tzinfo=UTC), source_key),
        )
        cur.execute("select first_known_at from material.events where event_id = %s", (event_id,))
        assert cur.fetchone()[0] == datetime(2026, 9, 17, 9, 0, tzinfo=UTC)

        earlier = _document(cur, source_key)
        cur.execute(
            """
            insert into material.event_sources
              (event_id, document_id, source_role, available_to_model_at, source_key)
            values (%s, %s, 'CORROBORATION', %s, %s)
            """,
            (event_id, earlier, datetime(2026, 9, 17, 6, 0, tzinfo=UTC), source_key),
        )
        cur.execute("select first_known_at from material.events where event_id = %s", (event_id,))
        assert cur.fetchone()[0] == datetime(2026, 9, 17, 6, 0, tzinfo=UTC)


def test_no_material_route_may_accept_weak_association(conn):
    with conn.cursor() as cur:
        cur.execute(
            """
            select count(*) from material.route_definitions
            where 'WEAK_ASSOCIATION' = any (requires_relation_types)
            """
        )
        assert cur.fetchone()[0] == 0

        with pytest.raises(psycopg2.errors.CheckViolation):
            cur.execute(
                """
                insert into material.route_definitions
                  (route_version, route_code, route_name, description, requires_relation_types)
                values ('test-1.0.0', 'MX', 'Bad route', 'accepts weak association',
                        '{DIRECT_COMPANY,WEAK_ASSOCIATION}')
                """
            )


def test_the_stage2_union_keeps_a_candidate_only_one_side_found(conn):
    with conn.cursor() as cur:
        material_run = _run(cur)
        technical_run = _run(cur)
        event_id = _event(cur)
        security_id = uuid.uuid4()

        cur.execute(
            """
            insert into material.candidates
              (run_id, market_code, security_id, as_of_date, route_version, feature_version,
               discovery_routes, route_count, event_ids, knowledge_cutoff, available_at)
            values (%s, 'JP', %s, %s, 'material-route-1.0.0', 'material-feature-1.0.0',
                    '{M1}', 1, array[%s]::uuid[], %s, %s)
            """,
            (str(material_run), str(security_id), AS_OF, event_id, T0, T0),
        )

        cur.execute(
            "select security_id, origin, material_routes, technical_routes "
            "from material.stage2_candidates(%s, %s, %s)",
            (str(technical_run), str(material_run), AS_OF),
        )
        rows = cur.fetchall()

        assert len(rows) == 1
        assert str(rows[0][0]) == str(security_id)
        assert rows[0][1] == "MATERIAL_ONLY"
        assert rows[0][2] == ["M1"]
        assert rows[0][3] == []


def test_a_candidate_must_have_fired_something(conn):
    with conn.cursor() as cur:
        run_id = _run(cur)
        event_id = _event(cur)
        with pytest.raises(psycopg2.errors.CheckViolation):
            cur.execute(
                """
                insert into material.candidates
                  (run_id, market_code, security_id, as_of_date, route_version, feature_version,
                   discovery_routes, route_count, event_ids, knowledge_cutoff, available_at)
                values (%s, 'JP', gen_random_uuid(), %s, 'material-route-1.0.0', 'v', '{}', 0,
                        array[%s]::uuid[], %s, %s)
                """,
                (str(run_id), AS_OF, event_id, T0, T0),
            )


def test_the_route_array_and_its_count_must_agree(conn):
    with conn.cursor() as cur:
        run_id = _run(cur)
        event_id = _event(cur)
        with pytest.raises(psycopg2.errors.CheckViolation):
            cur.execute(
                """
                insert into material.candidates
                  (run_id, market_code, security_id, as_of_date, route_version, feature_version,
                   discovery_routes, route_count, event_ids, knowledge_cutoff, available_at)
                values (%s, 'JP', gen_random_uuid(), %s, 'material-route-1.0.0', 'v', '{M1}', 3,
                        array[%s]::uuid[], %s, %s)
                """,
                (str(run_id), AS_OF, event_id, T0, T0),
            )


# ---------------------------------------------------------------------- chart


def _concept(cur, key: str, *, enabled=False, version="test-1.0.0") -> None:
    cur.execute(
        """
        insert into chart.concepts
          (concept_key, concept_version, name, definition, mechanism, positive_context,
           negative_context, counterexample, numerical_features, enabled)
        values (%s, %s, 'Test concept',
                'A definition long enough to satisfy the constraint on prose.',
                'A mechanism long enough to satisfy the constraint on prose.',
                'When it holds.', 'When it fails.', 'What it is not.',
                '{relative_volume_20d}', %s)
        """,
        (key, version, enabled),
    )


def test_a_concept_cannot_be_enabled_without_a_recorded_failure(conn):
    with conn.cursor() as cur:
        key = f"TEST_{uuid.uuid4().hex[:8].upper()}"
        with pytest.raises(psycopg2.errors.RaiseException, match="not knowledge"):
            _concept(cur, key, enabled=True)


def test_a_success_alone_is_not_enough_to_enable_a_concept(conn):
    with conn.cursor() as cur:
        key = f"TEST_{uuid.uuid4().hex[:8].upper()}"
        _concept(cur, key)
        cur.execute(
            """
            insert into chart.concept_examples
              (concept_key, concept_version, outcome, example_source, measurements, narrative)
            values (%s, 'test-1.0.0', 'SUCCESS', 'SYNTHETIC', '{"relative_volume_20d": 2.0}'::jsonb, 'worked')
            """,
            (key,),
        )
        with pytest.raises(psycopg2.errors.RaiseException, match="1 success and 0 failure"):
            cur.execute(
                "update chart.concepts set enabled = true where concept_key = %s and concept_version = 'test-1.0.0'",
                (key,),
            )


def test_a_concept_with_both_kinds_of_example_can_be_enabled(conn):
    with conn.cursor() as cur:
        key = f"TEST_{uuid.uuid4().hex[:8].upper()}"
        _concept(cur, key)
        cur.execute(
            """
            insert into chart.concept_examples
              (concept_key, concept_version, outcome, example_source, measurements, narrative)
            values
              (%s, 'test-1.0.0', 'SUCCESS', 'SYNTHETIC', '{"relative_volume_20d": 2.0}'::jsonb, 'worked'),
              (%s, 'test-1.0.0', 'FAILURE', 'SYNTHETIC', '{"relative_volume_20d": 1.9}'::jsonb, 'did not')
            """,
            (key, key),
        )
        cur.execute(
            "update chart.concepts set enabled = true where concept_key = %s and concept_version = 'test-1.0.0'",
            (key,),
        )
        assert cur.rowcount == 1


def test_a_concept_needs_a_measurable_definition(conn):
    with conn.cursor() as cur:
        with pytest.raises(psycopg2.errors.CheckViolation):
            cur.execute(
                """
                insert into chart.concepts
                  (concept_key, concept_version, name, definition, mechanism, positive_context,
                   negative_context, counterexample, numerical_features)
                values ('NO_NUMBERS', 'test-1.0.0', 'Vibes',
                        'A definition long enough to satisfy the constraint on prose.',
                        'A mechanism long enough to satisfy the constraint on prose.',
                        'When it holds.', 'When it fails.', 'What it is not.', '{}')
                """
            )


def test_a_historical_example_must_name_the_security_it_came_from(conn):
    with conn.cursor() as cur:
        key = f"TEST_{uuid.uuid4().hex[:8].upper()}"
        _concept(cur, key)
        with pytest.raises(psycopg2.errors.CheckViolation):
            cur.execute(
                """
                insert into chart.concept_examples
                  (concept_key, concept_version, outcome, example_source, measurements, narrative)
                values (%s, 'test-1.0.0', 'SUCCESS', 'HISTORICAL_OBSERVED',
                        '{"relative_volume_20d": 2.0}'::jsonb, 'worked')
                """,
                (key,),
            )


def test_the_seeded_concepts_are_all_graded_synthetic_only(conn):
    """They are enabled on hand-built examples, and that must stay visible.

    A synthetic example demonstrates a mechanism; it does not evidence one. When
    real price data exists these should move to OBSERVED_BOTH_WAYS, and until
    then the grade is the honest label.
    """

    with conn.cursor() as cur:
        cur.execute(
            "select concept_key, evidence_grade from chart.concept_evidence_grade "
            "where concept_version = 'chart-kb-1.0.0' order by concept_key"
        )
        rows = cur.fetchall()

        assert len(rows) == 6
        assert {grade for _key, grade in rows} == {"SYNTHETIC_ONLY"}
