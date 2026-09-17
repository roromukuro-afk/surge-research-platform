"""The crash cases, against the real database.

The in-memory store proves the job behaves. These prove the database enforces
it - which matters because the in-memory store is code I wrote to agree with
code I wrote, and the guarantees that survive a crash are the database's.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from decimal import Decimal

import pytest

psycopg2 = pytest.importorskip("psycopg2")

from surge.analysis.entry_analysis import EntryAnalysisResponse, EntryAnalysisState  # noqa: E402
from surge.analysis.execution import (  # noqa: E402
    ExecutionKey,
    ExecutionStatus,
    FailureClass,
    plan_recovery,
)
from surge.analysis.execution_db import DatabaseExecutionStore  # noqa: E402
from surge.analysis.llm import ProviderKind  # noqa: E402
from surge.entry.models import AnalysisKind  # noqa: E402

# One definition of what a well-formed watch looks like, shared with the
# lifecycle tests. A second copy here would drift from the real one.
from test_db_entry_lifecycle import _move, _watch  # noqa: E402

DSN = os.environ.get("SURGE_TEST_DATABASE_URL")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not DSN, reason="SURGE_TEST_DATABASE_URL is not set"),
]

CUTOFF = datetime(2026, 9, 17, 2, 15, tzinfo=UTC)
LATER = datetime(2026, 9, 17, 2, 17, tzinfo=UTC)


@pytest.fixture()
def conn():
    connection = psycopg2.connect(DSN)
    connection.autocommit = False
    try:
        yield connection
    finally:
        connection.rollback()
        connection.close()


def _triggered_watch(cur) -> tuple[str, str, int]:
    """A watch that has reached its trigger, and the id of that transition."""

    watch_id, security_id = _watch(cur)
    _move(cur, watch_id, "ARMED", "TRIGGER_HIT", kind="WATCH_MONITOR")
    cur.execute(
        "select transition_id from prod.watch_transitions "
        "where watch_id = %s and to_state = 'TRIGGER_HIT' order by transition_id desc limit 1",
        (watch_id,),
    )
    return str(watch_id), str(security_id), cur.fetchone()[0]


def _begin(store, watch_id, security_id, trigger_id, kind=AnalysisKind.REANALYSIS):
    return store.begin(
        ExecutionKey(
            watch_id=watch_id, trigger_transition_id=trigger_id, analysis_kind=kind
        ),
        security_id=security_id,
        decision_cutoff_at=CUTOFF,
        provider_id="groq_hosted",
        provider_kind="HOSTED_LLM",
        model_id="a-model",
        run_id=None,
        now=LATER,
    )


def _answer(**overrides) -> EntryAnalysisResponse:
    base = {
        "state": EntryAnalysisState.ENTRY,
        "rationale": "cleared the level",
        "provider_id": "groq_hosted",
        "provider_kind": ProviderKind.HOSTED_LLM,
        "model_id": "a-model",
        "decision_price_used": Decimal("1000"),
        "proposed_initial_failure_line": Decimal("940"),
    }
    base.update(overrides)
    return EntryAnalysisResponse(**base)


# ------------------------------------------------- the transition and the row


def test_beginning_moves_the_watch_and_records_the_analysis_together(conn):
    with conn.cursor() as cur:
        watch_id, security_id, trigger_id = _triggered_watch(cur)
        store = DatabaseExecutionStore(conn)

        result = _begin(store, watch_id, security_id, trigger_id)

        assert result.created
        assert result.execution.status is ExecutionStatus.STARTED
        cur.execute("select state from prod.watches where watch_id = %s", (watch_id,))
        assert cur.fetchone()[0] == "IN_REANALYSIS"


def test_a_second_begin_for_the_same_trigger_creates_nothing(conn):
    """Case three, at the database level: a retry after a commit must not start
    a second analysis or insert a second transition."""

    with conn.cursor() as cur:
        watch_id, security_id, trigger_id = _triggered_watch(cur)
        store = DatabaseExecutionStore(conn)

        first = _begin(store, watch_id, security_id, trigger_id)
        second = _begin(store, watch_id, security_id, trigger_id)

        assert not second.created
        assert second.execution.analysis_execution_id == first.execution.analysis_execution_id
        cur.execute(
            "select count(*) from prod.watch_transitions "
            "where watch_id = %s and to_state = 'IN_REANALYSIS'",
            (watch_id,),
        )
        assert cur.fetchone()[0] == 1


def test_beginning_from_a_watch_that_is_not_at_its_trigger_is_refused(conn):
    """An ARMED watch has not been triggered, so there is nothing to reanalyse.
    The watch machine says so, and begin_entry_analysis inherits that."""

    with conn.cursor() as cur:
        watch_id, security_id = _watch(cur)
        _move(cur, watch_id, "ARMED", "EXPIRED")
        cur.execute(
            "select transition_id from prod.watch_transitions where watch_id = %s "
            "order by transition_id desc limit 1",
            (watch_id,),
        )
        transition_id = cur.fetchone()[0]

        with pytest.raises(psycopg2.errors.RaiseException, match="illegal watch transition"):
            _begin(DatabaseExecutionStore(conn), str(watch_id), str(security_id), transition_id)


# ------------------------------------------------------------- the answer


def test_a_stored_answer_leaves_the_analysis_started_and_findable(conn):
    """Case two: the model has answered and nothing has been decided."""

    with conn.cursor() as cur:
        watch_id, security_id, trigger_id = _triggered_watch(cur)
        store = DatabaseExecutionStore(conn)
        begun = _begin(store, watch_id, security_id, trigger_id)

        store.record_answer(begun.execution.analysis_execution_id, _answer())

        plan = plan_recovery(store)
        resumable = [
            e
            for e in plan.resume_from_stored_answer
            if e.analysis_execution_id == begun.execution.analysis_execution_id
        ]
        assert len(resumable) == 1
        assert resumable[0].stored_answer.state is EntryAnalysisState.ENTRY
        assert resumable[0].stored_answer.proposed_initial_failure_line == Decimal("940.000000")


def test_the_in_flight_view_shows_whether_the_model_was_already_paid_for(conn):
    with conn.cursor() as cur:
        watch_id, security_id, trigger_id = _triggered_watch(cur)
        store = DatabaseExecutionStore(conn)
        begun = _begin(store, watch_id, security_id, trigger_id)

        cur.execute(
            "select has_a_stored_answer, watch_state from ui.entry_analysis_in_flight "
            "where analysis_execution_id = %s",
            (begun.execution.analysis_execution_id,),
        )
        before = cur.fetchone()
        store.record_answer(begun.execution.analysis_execution_id, _answer())
        cur.execute(
            "select has_a_stored_answer from ui.entry_analysis_in_flight "
            "where analysis_execution_id = %s",
            (begun.execution.analysis_execution_id,),
        )
        after = cur.fetchone()

    assert before == (False, "IN_REANALYSIS")
    assert after == (True,)


# --------------------------------------------------------- one outcome only


def test_a_completed_analysis_cannot_be_changed_again(conn):
    from surge.analysis.entry_analysis import EntryValidation
    from surge.analysis.validate import ValidationStatus

    with conn.cursor() as cur:
        watch_id, security_id, trigger_id = _triggered_watch(cur)
        store = DatabaseExecutionStore(conn)
        begun = _begin(store, watch_id, security_id, trigger_id)
        store.record_answer(begun.execution.analysis_execution_id, _answer())
        store.complete(
            begun.execution.analysis_execution_id,
            validation=EntryValidation(status=ValidationStatus.PASSED),
            now=LATER,
        )

        with pytest.raises(psycopg2.errors.RaiseException, match="already COMPLETED"):
            store.fail(
                begun.execution.analysis_execution_id,
                failure_class=FailureClass.PROVIDER_ERROR,
                failure_detail="after the fact",
                now=LATER,
            )


def test_the_worker_cannot_write_the_table_directly(conn):
    """The transition and the record stay in step because there is no other way
    in. A store that wrote the table could record an analysis whose watch never
    moved, and the row would look exactly like one that did."""

    with conn.cursor() as cur:
        cur.execute(
            "select has_table_privilege('surge_worker_prod', "
            "'prod.entry_analysis_executions', %s)",
            ("INSERT",),
        )
        assert cur.fetchone()[0] is False
        for privilege in ("UPDATE", "DELETE"):
            cur.execute(
                "select has_table_privilege('surge_worker_prod', "
                "'prod.entry_analysis_executions', %s)",
                (privilege,),
            )
            assert cur.fetchone()[0] is False


def test_a_direct_update_is_refused_even_by_an_owner(conn):
    with conn.cursor() as cur:
        watch_id, security_id, trigger_id = _triggered_watch(cur)
        begun = _begin(DatabaseExecutionStore(conn), watch_id, security_id, trigger_id)

        with pytest.raises(psycopg2.errors.RaiseException, match="written through"):
            cur.execute(
                "update prod.entry_analysis_executions set rationale = 'edited' "
                "where analysis_execution_id = %s",
                (begun.execution.analysis_execution_id,),
            )


def test_an_execution_may_not_be_deleted(conn):
    with conn.cursor() as cur:
        watch_id, security_id, trigger_id = _triggered_watch(cur)
        begun = _begin(DatabaseExecutionStore(conn), watch_id, security_id, trigger_id)

        with pytest.raises(psycopg2.errors.RaiseException, match="append-only"):
            cur.execute(
                "delete from prod.entry_analysis_executions where analysis_execution_id = %s",
                (begun.execution.analysis_execution_id,),
            )


def test_an_end_of_day_analysis_cannot_be_an_intraday_execution(conn):
    with conn.cursor() as cur:
        watch_id, security_id, trigger_id = _triggered_watch(cur)

        with pytest.raises(psycopg2.errors.CheckViolation):
            cur.execute(
                """
                insert into prod.entry_analysis_executions
                  (watch_id, trigger_transition_id, security_id, analysis_kind,
                   decision_cutoff_at, provider_id, provider_kind, request_version)
                values (%s, %s, %s, 'EOD', %s, 'p', 'HOSTED_LLM', 'v')
                """,
                (watch_id, trigger_id, security_id, CUTOFF),
            )
