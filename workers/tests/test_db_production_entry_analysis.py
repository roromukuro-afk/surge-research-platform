"""The three-transaction runner, with real commits and real crashes.

Everything else in the suite rolls back, and rolling back is exactly what these
cannot do: the property under test is *what survives a commit*. A test that
rolled back would prove the rows can be written, which was never in doubt, and
prove nothing about what a killed process leaves behind.

Committing means these tests can only run against a disposable database, and
:func:`_assert_disposable` refuses to start otherwise. Pointing
``SURGE_TEST_DATABASE_URL`` at the production project would otherwise put
synthetic predictions into the teacher population, where nothing afterwards
could tell them from real ones.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

psycopg2 = pytest.importorskip("psycopg2")

from surge.analysis.entry_analysis import (  # noqa: E402
    EntryAnalysisResponse,
    EntryAnalysisState,
    EntryGuardFacts,
    IntradayBundle,
)
from surge.analysis.execution import (  # noqa: E402
    ANALYSIS_DEADLINE,
    MAX_TRANSIENT_ATTEMPTS,
    ExecutionKey,
    FailureClass,
)
from surge.analysis.execution_db import DatabaseExecutionStore  # noqa: E402
from surge.analysis.llm import ProviderKind, ZoneBasisKind  # noqa: E402
from surge.entry.models import (  # noqa: E402
    AnalysisKind,
    EntryAttemptStatus,
    ObservedPrice,
    UniverseVerdict,
    WatchState,
)
from surge.jobs.production_entry_analysis import (  # noqa: E402
    ProductionEntryAnalysis,
    resume_all,
)
from test_db_entry_lifecycle import _move, _watch  # noqa: E402

DSN = os.environ.get("SURGE_TEST_DATABASE_URL")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not DSN, reason="SURGE_TEST_DATABASE_URL is not set"),
]

CUTOFF = datetime(2026, 9, 17, 2, 15, tzinfo=UTC)
COMPLETED_AT = datetime(2026, 9, 17, 2, 16, tzinfo=UTC)
LATER = datetime(2026, 9, 17, 2, 17, tzinfo=UTC)
CANONICAL = "c" * 64


def _assert_disposable(conn) -> None:
    """Refuse to commit into a database that holds anything real.

    The teacher tables start at zero and stay there until a real episode
    resolves (D-163). A committing test against production would put rows in
    that population which are indistinguishable from real ones afterwards -
    there is no undo for that, so the check is before rather than after.
    """

    with conn.cursor() as cur:
        cur.execute(
            """
            select (select count(*) from labels.objective_labels)
                 + (select count(*) from labels.interpretive_labels)
                 + (select count(*) from labels.datasets)
            """
        )
        teacher = cur.fetchone()[0]
    if teacher:
        pytest.skip(
            f"this database holds {teacher} teacher row(s), so it is not disposable and these "
            "committing tests will not run against it"
        )


#: The watches each test created, so the fixture can remove them afterwards.
#: A module-level list rather than an attribute on the connection, because a
#: psycopg2 connection is a C object and will not carry one.
_CREATED: list[str] = []


@pytest.fixture()
def conn():
    connection = psycopg2.connect(DSN)
    connection.autocommit = False
    _assert_disposable(connection)
    created = _CREATED
    created.clear()
    try:
        yield connection
    finally:
        # These tests commit, so they clean up after themselves. Predictions and
        # episodes are append-only for the worker; the test runs as owner.
        try:
            with connection.cursor() as cur:
                for watch_id in created:
                    cur.execute(
                        "delete from prod.predictions where attempt_id in "
                        "(select attempt_id from prod.entry_attempts where watch_id = %s)",
                        (watch_id,),
                    )
                    cur.execute(
                        "delete from prod.entry_analysis_executions where watch_id = %s",
                        (watch_id,),
                    )
                    cur.execute("delete from prod.entry_attempts where watch_id = %s", (watch_id,))
                    cur.execute("delete from prod.watch_transitions where watch_id = %s", (watch_id,))
                    cur.execute("delete from prod.watches where watch_id = %s", (watch_id,))
            connection.commit()
        except Exception:  # noqa: BLE001 - cleanup must not mask a real failure
            connection.rollback()
        connection.close()


def _triggered(conn) -> tuple[str, str, int]:
    with conn.cursor() as cur:
        watch_id, security_id = _watch(cur)
        _move(cur, watch_id, "ARMED", "TRIGGER_HIT", kind="WATCH_MONITOR")
        cur.execute(
            "select transition_id from prod.watch_transitions where watch_id = %s "
            "and to_state = 'TRIGGER_HIT' order by transition_id desc limit 1",
            (watch_id,),
        )
        trigger_id = cur.fetchone()[0]
    conn.commit()
    _CREATED.append(str(watch_id))
    return str(watch_id), str(security_id), trigger_id


def _bundle(security_id: str) -> IntradayBundle:
    return IntradayBundle(
        security_id=security_id,
        market_code="JP",
        session_date=date(2026, 9, 17),
        decision_cutoff_at=CUTOFF,
        canonical_prompt_sha256=CANONICAL,
        sections={
            "live_price": {"price": "1000"},
            "stage3_setup": {"state": "WATCH_BREAKOUT"},
            "coverage": {"materials": 1.0},
        },
    )


def _facts(**overrides) -> EntryGuardFacts:
    base = {
        "universe": UniverseVerdict(decision="INCLUDED"),
        "decision_price": ObservedPrice(
            amount=Decimal("1000"), currency="JPY", observed_at=CUTOFF
        ),
        "decision_cutoff_at": CUTOFF,
        "decision_completed_at": COMPLETED_AT,
        "coverage_meets_requirements": True,
        "coverage_detail": "all collectors reported",
    }
    base.update(overrides)
    return EntryGuardFacts(**base)


def _answer(**overrides) -> EntryAnalysisResponse:
    base = {
        "state": EntryAnalysisState.ENTRY,
        "rationale": "cleared the level on expanding volume",
        "provider_id": "groq_hosted",
        "provider_kind": ProviderKind.HOSTED_LLM,
        "model_id": "a-model",
        "decision_price_used": Decimal("1000"),
        "proposed_initial_failure_line": Decimal("940"),
        "reachable_zone_low": Decimal("1010"),
        "reachable_zone_high": Decimal("1150"),
        "reachable_zone_basis_kinds": (ZoneBasisKind.VOLUME_STRUCTURE,),
        "reachable_zone_basis": "three sessions of expanding volume",
        "concepts_considered": ("VOLUME_EXPANSION",),
        "raw_text": '{"state": "ENTRY"}',
    }
    base.update(overrides)
    return EntryAnalysisResponse(**base)


class _Provider:
    provider_id = "groq_hosted"
    provider_kind = ProviderKind.HOSTED_LLM
    model_id = "a-model"

    def __init__(self, response=None, raises=None):
        self._response = response or _answer()
        self._raises = raises
        self.calls = 0

    def analyse_entry(self, request):
        self.calls += 1
        if self._raises:
            raise self._raises
        return self._response


def _runner(conn, provider) -> ProductionEntryAnalysis:
    return ProductionEntryAnalysis(
        conn=conn,
        store=DatabaseExecutionStore(conn),
        provider=provider,
        canonical_text="CANONICAL",
    )


def _run(runner, watch_id, security_id, trigger_id, **overrides):
    kwargs = {
        "watch_id": watch_id,
        "trigger_transition_id": trigger_id,
        "bundle": _bundle(security_id),
        "facts": _facts(),
        "thesis_key": "WATCH_BREAKOUT|R_A",
        "now": LATER,
        "entry_price": ObservedPrice(
            amount=Decimal("1005"), currency="JPY", observed_at=LATER
        ),
        "entry_price_method": "LAST_TRADE",
    }
    kwargs.update(overrides)
    return runner.run_for_trigger(**kwargs)


def _status(conn, execution_id) -> tuple[str, str]:
    conn.rollback()
    with conn.cursor() as cur:
        cur.execute(
            "select e.status::text, w.state::text from prod.entry_analysis_executions e "
            "join prod.watches w on w.watch_id = e.watch_id "
            "where e.analysis_execution_id = %s",
            (execution_id,),
        )
        return cur.fetchone()


# ---------------------------------------------- 1. crash after TX1 commits


def test_a_crash_after_tx1_leaves_a_committed_started_row(conn):
    """Committed, not merely written. This is the difference the three-phase
    split exists for: an uncommitted row survives nothing."""

    watch_id, security_id, trigger_id = _triggered(conn)
    provider = _Provider(raises=SystemExit("killed mid-call"))
    runner = _runner(conn, provider)

    with pytest.raises(SystemExit):
        _run(runner, watch_id, security_id, trigger_id)

    # A different connection, which can only see committed data.
    other = psycopg2.connect(DSN)
    try:
        with other.cursor() as cur:
            cur.execute(
                "select e.status::text, w.state::text from prod.entry_analysis_executions e "
                "join prod.watches w on w.watch_id = e.watch_id where e.watch_id = %s",
                (watch_id,),
            )
            status, watch_state = cur.fetchone()
    finally:
        other.close()

    assert status == "STARTED"
    assert watch_state == "IN_REANALYSIS"


def test_a_transient_provider_failure_is_retried_rather_than_failed(conn):
    """The watch stays IN_REANALYSIS because it genuinely is. Failing it would
    strand the security: nothing else moves a watch out of that state."""

    watch_id, security_id, trigger_id = _triggered(conn)
    runner = _runner(conn, _Provider(raises=TimeoutError("connection timed out")))

    result = _run(runner, watch_id, security_id, trigger_id)

    assert result.retried
    assert result.prediction_id is None
    status, watch_state = _status(conn, result.analysis_execution_id)
    assert status == "RETRY_PENDING"
    assert watch_state == "IN_REANALYSIS"


def test_a_provider_that_never_answers_stops_being_transient(conn):
    """The fix for the stranded watch, applied without a budget, rebuilds it in
    slow motion: every individual attempt is transient, the condition is not,
    and the watch sits at IN_REANALYSIS for as long as the provider is down.

    When the budget runs out the failure becomes terminal, which means the watch
    is moved first - the database refuses to fail an execution whose watch is
    still IN_REANALYSIS, so the escalation cannot forget it."""

    watch_id, security_id, trigger_id = _triggered(conn)
    provider = _Provider(raises=TimeoutError("connection timed out"))
    runner = _runner(conn, provider)

    for _ in range(MAX_TRANSIENT_ATTEMPTS):
        result = _run(runner, watch_id, security_id, trigger_id)
        assert result.retried

    final = _run(runner, watch_id, security_id, trigger_id)

    assert final.failure_class is FailureClass.RETRY_EXHAUSTED
    status, watch_state = _status(conn, final.analysis_execution_id)
    assert status == "FAILED"
    # Not IN_REANALYSIS. The trigger goes unanswered and the watch re-arms.
    assert watch_state == "REARMED"
    assert provider.calls == MAX_TRANSIENT_ATTEMPTS + 1
    # And nothing was decided from a provider that never answered.
    assert final.prediction_id is None
    assert final.attempt_id is None


def test_an_analysis_that_ran_past_its_deadline_is_not_retried_again(conn):
    """One slow attempt spends the budget as surely as five fast ones, and it is
    the cutoff that is measured from, not the insert: the answer would be about
    a price the session has already left behind."""

    watch_id, security_id, trigger_id = _triggered(conn)
    runner = _runner(conn, _Provider(raises=TimeoutError("still hanging")))

    result = _run(
        runner,
        watch_id,
        security_id,
        trigger_id,
        now=CUTOFF + ANALYSIS_DEADLINE + timedelta(minutes=1),
    )

    assert result.failure_class is FailureClass.ANALYSIS_DEADLINE_PASSED
    status, watch_state = _status(conn, result.analysis_execution_id)
    assert status == "FAILED"
    assert watch_state == "REARMED"


def test_recovery_reports_what_it_could_not_resume(conn):
    """A sweep that cannot resume an execution leaves a watch at IN_REANALYSIS,
    and nothing else moves one. Skipping it silently made "nothing was stuck"
    and "several things were stuck and I walked past them" look identical."""

    watch_id, security_id, trigger_id = _triggered(conn)
    runner = _runner(conn, _Provider(raises=TimeoutError("connection timed out")))
    started = _run(runner, watch_id, security_id, trigger_id)
    assert started.retried

    recovery = resume_all(
        runner,
        bundles={},
        facts_for=lambda _e: _facts(),
        now=LATER,
        thesis_for=lambda _e: "WATCH_BREAKOUT|R_A",
    )

    assert recovery.resumed == []
    assert len(recovery.unresumable) == 1
    stuck = recovery.unresumable[0]
    assert stuck["watch_id"] == watch_id
    assert "IN_REANALYSIS" in stuck["why"]
    assert recovery.summary["stuck"] == [started.analysis_execution_id]


# ------------------------------------------- 2. crash after TX2 commits


def test_a_crash_after_tx2_resumes_from_the_stored_answer(conn):
    """And the model is not called a second time - not only to avoid paying
    twice, but because a different answer would leave no fact of the matter
    about which one was the analysis for this trigger."""

    watch_id, security_id, trigger_id = _triggered(conn)
    store = DatabaseExecutionStore(conn)

    # First pass: answer stored, then the process dies before deciding.
    first = ProductionEntryAnalysis(
        conn=conn, store=store, provider=_Provider(), canonical_text="CANONICAL"
    )
    begun = store.begin(
        ExecutionKey(
            watch_id=watch_id,
            trigger_transition_id=trigger_id,
            analysis_kind=AnalysisKind.REANALYSIS,
        ),
        decision_cutoff_at=CUTOFF,
        provider_id="groq_hosted",
        provider_kind="HOSTED_LLM",
        model_id="a-model",
        run_id=None,
        now=LATER,
    )
    store.record_answer(
        begun.execution.analysis_execution_id,
        _answer(),
        prompt_sha256="p" * 64,
        bundle_sha256="b" * 64,
        canonical_prompt_sha256=CANONICAL,
    )
    conn.commit()

    # Restart, with a provider that would answer differently.
    second_provider = _Provider(response=_answer(rationale="a different answer entirely"))
    second = _runner(conn, second_provider)
    result = _run(second, watch_id, security_id, trigger_id)

    assert second_provider.calls == 0
    assert result.resumed
    assert result.created_a_prediction
    assert first is not second  # the runner is stateless; the record is what carries over


def test_the_stored_answer_round_trips_without_losing_the_zone_basis(conn):
    """The validator requires the basis for an ENTRY. Losing it would mean a
    resumed analysis could no longer pass the contract the original passed."""

    watch_id, security_id, trigger_id = _triggered(conn)
    store = DatabaseExecutionStore(conn)
    original = _answer()
    begun = store.begin(
        ExecutionKey(
            watch_id=watch_id,
            trigger_transition_id=trigger_id,
            analysis_kind=AnalysisKind.REANALYSIS,
        ),
        decision_cutoff_at=CUTOFF,
        provider_id="groq_hosted",
        provider_kind="HOSTED_LLM",
        model_id="a-model",
        run_id=None,
        now=LATER,
    )
    store.record_answer(
        begun.execution.analysis_execution_id,
        original,
        prompt_sha256="p" * 64,
        bundle_sha256="b" * 64,
        canonical_prompt_sha256=CANONICAL,
    )
    conn.commit()

    # A fresh store on a fresh connection: a restart, not a cache.
    other = psycopg2.connect(DSN)
    try:
        reloaded = DatabaseExecutionStore(other)._load(begun.execution.analysis_execution_id)
    finally:
        other.close()

    answer = reloaded.stored_answer
    assert answer.state is original.state
    assert answer.rationale == original.rationale
    assert answer.decision_price_used == original.decision_price_used
    assert answer.proposed_initial_failure_line == original.proposed_initial_failure_line
    assert answer.reachable_zone_low == original.reachable_zone_low
    assert answer.reachable_zone_high == original.reachable_zone_high
    assert answer.reachable_zone_basis_kinds == original.reachable_zone_basis_kinds
    assert answer.reachable_zone_basis == original.reachable_zone_basis
    assert answer.concepts_considered == original.concepts_considered
    assert answer.raw_text == original.raw_text
    assert reloaded.prompt_sha256 == "p" * 64
    assert reloaded.bundle_sha256 == "b" * 64
    assert reloaded.canonical_prompt_sha256 == CANONICAL


# ------------------------------------------- 3. crash after TX3 commits


def test_the_decision_is_actually_persisted(conn):
    """The job used to call decide() and write nothing. An attempt that exists
    only in Python is not a ledger."""

    watch_id, security_id, trigger_id = _triggered(conn)
    result = _run(_runner(conn, _Provider()), watch_id, security_id, trigger_id)

    assert result.attempt_status is EntryAttemptStatus.PREDICTION_CREATED
    assert result.attempt_id and result.prediction_id and result.episode_id

    other = psycopg2.connect(DSN)
    try:
        with other.cursor() as cur:
            cur.execute(
                "select count(*) from prod.predictions where prediction_id = %s",
                (result.prediction_id,),
            )
            assert cur.fetchone()[0] == 1
            cur.execute(
                "select entry_attempt_id, prediction_id, status::text "
                "from prod.entry_analysis_executions where analysis_execution_id = %s",
                (result.analysis_execution_id,),
            )
            attempt_id, prediction_id, status = cur.fetchone()
    finally:
        other.close()

    # The ids on the execution are the ones the database returned, not
    # attributes read off a dataclass.
    assert str(attempt_id) == result.attempt_id
    assert str(prediction_id) == result.prediction_id
    assert status == "COMPLETED"


def test_a_retry_after_tx3_does_not_write_a_second_prediction(conn):
    watch_id, security_id, trigger_id = _triggered(conn)
    provider = _Provider()
    first = _run(_runner(conn, provider), watch_id, security_id, trigger_id)

    second = _run(_runner(conn, provider), watch_id, security_id, trigger_id)

    assert second.already_decided
    assert provider.calls == 1
    other = psycopg2.connect(DSN)
    try:
        with other.cursor() as cur:
            cur.execute(
                "select count(*) from prod.entry_attempts where watch_id = %s", (watch_id,)
            )
            assert cur.fetchone()[0] == 1
    finally:
        other.close()
    assert second.prediction_id == first.prediction_id


# ------------------------------- 4. a failure inside TX3 rolls all of it back


def test_a_prediction_failure_rolls_back_the_attempt_and_the_watch(conn):
    """The attempt, the episode, the watch move and the completion go with it.
    A prediction whose attempt rolled back would be a claim with no decision."""

    watch_id, security_id, trigger_id = _triggered(conn)
    runner = _runner(conn, _Provider())

    from surge.entry import db as entry_db

    original = entry_db.write_prediction

    def exploding(*args, **kwargs):
        raise RuntimeError("the prediction insert failed")

    entry_db.write_prediction = exploding
    try:
        with pytest.raises(RuntimeError, match="prediction insert failed"):
            _run(runner, watch_id, security_id, trigger_id)
    finally:
        entry_db.write_prediction = original

    other = psycopg2.connect(DSN)
    try:
        with other.cursor() as cur:
            cur.execute("select count(*) from prod.entry_attempts where watch_id = %s", (watch_id,))
            assert cur.fetchone()[0] == 0
            cur.execute("select state::text from prod.watches where watch_id = %s", (watch_id,))
            assert cur.fetchone()[0] == "IN_REANALYSIS"
            cur.execute(
                "select status::text from prod.entry_analysis_executions where watch_id = %s",
                (watch_id,),
            )
            assert cur.fetchone()[0] == "STARTED"
    finally:
        other.close()


# --------------------------------------------- terminal failures move the watch


def test_a_contract_violation_moves_the_watch_and_then_fails(conn):
    watch_id, security_id, trigger_id = _triggered(conn)
    runner = _runner(conn, _Provider(response=_answer(proposed_initial_failure_line=None)))

    result = _run(runner, watch_id, security_id, trigger_id)

    assert result.failure_class is FailureClass.CONTRACT_VIOLATION
    assert result.watch_state_after is WatchState.REARMED
    status, watch_state = _status(conn, result.analysis_execution_id)
    assert status == "FAILED"
    assert watch_state == "REARMED"


def test_the_database_refuses_to_strand_a_watch(conn):
    """Failing an execution whose watch is still IN_REANALYSIS would leave the
    security stuck forever, because nothing else moves a watch out of it."""

    watch_id, security_id, trigger_id = _triggered(conn)
    store = DatabaseExecutionStore(conn)
    begun = store.begin(
        ExecutionKey(
            watch_id=watch_id,
            trigger_transition_id=trigger_id,
            analysis_kind=AnalysisKind.REANALYSIS,
        ),
        decision_cutoff_at=CUTOFF,
        provider_id="groq_hosted",
        provider_kind="HOSTED_LLM",
        model_id="a-model",
        run_id=None,
        now=LATER,
    )
    conn.commit()

    with pytest.raises(psycopg2.errors.RaiseException, match="stuck forever"):
        store.fail(
            begun.execution.analysis_execution_id,
            failure_class=FailureClass.QUOTA_BLOCKED,
            failure_detail="no quota",
        )
    conn.rollback()


def test_a_transient_class_cannot_be_used_to_fail(conn):
    """Calling fail() with a transient class is the mistake that strands a
    watch, so it is refused in Python before it reaches the database."""

    from surge.analysis.execution import ExecutionError

    store = DatabaseExecutionStore(conn)
    with pytest.raises(ExecutionError, match="transient"):
        store.fail(
            str(uuid.uuid4()),
            failure_class=FailureClass.PROVIDER_TIMEOUT,
            failure_detail="timed out",
        )


# ------------------------------------------------------ causal binding


def test_another_watchs_trigger_cannot_start_this_analysis(conn):
    watch_id, security_id, _ = _triggered(conn)
    other_watch, _, other_trigger = _triggered(conn)

    store = DatabaseExecutionStore(conn)
    with pytest.raises(psycopg2.errors.RaiseException, match="belongs to watch"):
        store.begin(
            ExecutionKey(
                watch_id=watch_id,
                trigger_transition_id=other_trigger,
                analysis_kind=AnalysisKind.REANALYSIS,
            ),
            decision_cutoff_at=CUTOFF,
            provider_id="groq_hosted",
            provider_kind="HOSTED_LLM",
            run_id=None,
            now=LATER,
        )
    conn.rollback()
    assert other_watch != watch_id


def test_a_superseded_trigger_cannot_be_answered(conn):
    """A watch that re-armed and triggered again has a newer trigger. Answering
    the old one would attach this analysis to an event already closed out - the
    timestamps would look right and the causality would be wrong."""

    watch_id, security_id, first_trigger = _triggered(conn)
    with conn.cursor() as cur:
        _move(cur, watch_id, "TRIGGER_HIT", "IN_REANALYSIS", kind="REANALYSIS")
        _move(cur, watch_id, "IN_REANALYSIS", "REARMED", kind="REANALYSIS")
        _move(cur, watch_id, "REARMED", "TRIGGER_HIT", kind="WATCH_MONITOR")
    conn.commit()

    store = DatabaseExecutionStore(conn)
    with pytest.raises(psycopg2.errors.RaiseException, match="superseded"):
        store.begin(
            ExecutionKey(
                watch_id=watch_id,
                trigger_transition_id=first_trigger,
                analysis_kind=AnalysisKind.REANALYSIS,
            ),
            decision_cutoff_at=CUTOFF,
            provider_id="groq_hosted",
            provider_kind="HOSTED_LLM",
            run_id=None,
            now=LATER,
        )
    conn.rollback()


def test_the_security_comes_from_the_watch_not_the_caller(conn):
    watch_id, security_id, trigger_id = _triggered(conn)
    store = DatabaseExecutionStore(conn)
    key = ExecutionKey(
        watch_id=watch_id, trigger_transition_id=trigger_id, analysis_kind=AnalysisKind.REANALYSIS
    )

    with pytest.raises(psycopg2.errors.RaiseException, match="is on security"):
        store.begin(
            key,
            security_id=str(uuid.uuid4()),
            decision_cutoff_at=CUTOFF,
            provider_id="groq_hosted",
            provider_kind="HOSTED_LLM",
            run_id=None,
            now=LATER,
        )
    conn.rollback()

    begun = store.begin(
        key,
        decision_cutoff_at=CUTOFF,
        provider_id="groq_hosted",
        provider_kind="HOSTED_LLM",
        run_id=None,
        now=LATER,
    )
    conn.commit()
    assert begun.execution.security_id == security_id


# -------------------------------------------------- hashes are not optional


def test_completion_without_the_hashes_is_refused_for_a_hosted_model(conn):
    from surge.analysis.entry_analysis import EntryValidation
    from surge.analysis.execution import ExecutionKey
    from surge.analysis.validate import ValidationStatus
    from surge.entry.models import AnalysisKind

    watch_id, security_id, trigger_id = _triggered(conn)
    store = DatabaseExecutionStore(conn)
    begun = store.begin(
        ExecutionKey(
            watch_id=watch_id,
            trigger_transition_id=trigger_id,
            analysis_kind=AnalysisKind.REANALYSIS,
        ),
        decision_cutoff_at=CUTOFF,
        provider_id="groq_hosted",
        provider_kind="HOSTED_LLM",
        run_id=None,
        now=LATER,
    )
    with conn.cursor() as cur:
        _move(cur, watch_id, "IN_REANALYSIS", "REARMED", kind="REANALYSIS")

    with pytest.raises(psycopg2.errors.RaiseException, match="without the prompt, bundle"):
        store.complete(
            begun.execution.analysis_execution_id,
            validation=EntryValidation(status=ValidationStatus.PASSED),
        )
    conn.rollback()
