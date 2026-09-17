"""The three crashes an intraday analysis has to survive.

The claim these exist to make true: "a crash mid-analysis leaves stored state
saying an analysis was under way". It was previously false - the job moved a
Python object and nothing was written - and the failure mode was specific. A
process that died between the trigger and the answer left a watch at
TRIGGER_HIT, which reads as "nothing acted on this trigger", so a recovery pass
looking for TRIGGER_HIT would either miss it or, worse, run it a second time.

Each test below kills the process at a different point and restarts.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from surge.analysis.entry_analysis import (
    DeterministicIntradayStandIn,
    EntryAnalysisResponse,
    EntryAnalysisState,
    EntryContractError,
    EntryGuardFacts,
    IntradayBundle,
)
from surge.analysis.execution import (
    ANALYSIS_DEADLINE,
    MAX_TRANSIENT_ATTEMPTS,
    AnalysisExecution,
    ExecutionError,
    ExecutionKey,
    ExecutionStatus,
    FailureClass,
    InMemoryExecutionStore,
    plan_recovery,
)
from surge.analysis.llm import ProviderKind
from surge.entry.models import AnalysisKind, EntryAttemptStatus, ObservedPrice, UniverseVerdict, WatchState
from surge.entry.watch import Watch
from surge.jobs.entry_analysis_job import EntryAnalysisJob

CUTOFF = datetime(2026, 9, 17, 2, 15, tzinfo=UTC)
COMPLETED = datetime(2026, 9, 17, 2, 16, tzinfo=UTC)
LATER = datetime(2026, 9, 17, 2, 17, tzinfo=UTC)
CANONICAL = "0" * 64
TRIGGER = 4242


def _bundle() -> IntradayBundle:
    return IntradayBundle(
        security_id="JP:LOCAL:1234",
        market_code="JP",
        session_date=date(2026, 9, 17),
        decision_cutoff_at=CUTOFF,
        canonical_prompt_sha256=CANONICAL,
        sections={
            "live_price": {"price": "1000"},
            "stage3_setup": {"state": "WATCH_BREAKOUT"},
            "coverage": {"materials": 1.0},
            "watch": {"trigger_hit": True},
        },
    )


def _facts(**overrides) -> EntryGuardFacts:
    base = {
        "universe": UniverseVerdict(decision="INCLUDED"),
        "decision_price": ObservedPrice(
            amount=Decimal("1000"), currency="JPY", observed_at=CUTOFF
        ),
        "decision_cutoff_at": CUTOFF,
        "decision_completed_at": COMPLETED,
        "coverage_meets_requirements": True,
        "coverage_detail": "all required collectors reported",
    }
    base.update(overrides)
    return EntryGuardFacts(**base)


def _entry(**overrides) -> EntryAnalysisResponse:
    base = {
        "state": EntryAnalysisState.ENTRY,
        "rationale": "cleared the level on expanding volume; enter now",
        "provider_id": "groq_hosted",
        "provider_kind": ProviderKind.HOSTED_LLM,
        "model_id": "a-model",
        "decision_price_used": Decimal("1000"),
        "proposed_initial_failure_line": Decimal("940"),
        "reachable_zone_low": Decimal("1010"),
        "reachable_zone_high": Decimal("1150"),
        "reachable_zone_basis_kinds": (),
        "reachable_zone_basis": "expanding volume through the level",
    }
    base.update(overrides)
    from surge.analysis.llm import ZoneBasisKind

    base.setdefault("reachable_zone_basis_kinds", (ZoneBasisKind.VOLUME_STRUCTURE,))
    if not base["reachable_zone_basis_kinds"]:
        base["reachable_zone_basis_kinds"] = (ZoneBasisKind.VOLUME_STRUCTURE,)
    return EntryAnalysisResponse(**base)


def _watch() -> Watch:
    watch = Watch(
        watch_id="w-1",
        security_id="JP:LOCAL:1234",
        setup_id="s-1",
        trigger_description="clears the breakout level",
        opened_at=CUTOFF,
    )
    watch.trigger(at=CUTOFF, price=Decimal("1000"))
    return watch


class _Provider:
    provider_id = "groq_hosted"
    provider_kind = ProviderKind.HOSTED_LLM
    model_id = "a-model"

    def __init__(self, response=None, raises=None):
        self._response = response or _entry()
        self._raises = raises
        self.calls = 0

    def analyse_entry(self, request):
        self.calls += 1
        if self._raises:
            raise self._raises
        return self._response


def _job(provider, store) -> EntryAnalysisJob:
    return EntryAnalysisJob(provider=provider, canonical_text="CANONICAL", executions=store)


def _run(job, watch, **overrides):
    kwargs = {
        "watch": watch,
        "bundle": _bundle(),
        "facts": _facts(),
        "thesis_key": "t",
        "now": LATER,
        "trigger_transition_id": TRIGGER,
        "entry_price": ObservedPrice(amount=Decimal("1005"), currency="JPY", observed_at=LATER),
    }
    kwargs.update(overrides)
    return job.run_for_watch(**kwargs)


# ------------------------------------------------------- the record exists


def test_the_execution_is_recorded_before_the_model_is_called():
    """The ordering is the whole claim: by the time the provider is asked, a row
    already says an analysis is under way."""

    store = InMemoryExecutionStore()
    seen = {}

    class _Checks(_Provider):
        def analyse_entry(self, request):
            seen["in_flight"] = [e.status for e in store.in_flight()]
            seen["watch"] = watch.state
            return _entry()

    watch = _watch()
    _run(_job(_Checks(), store), watch)

    assert seen["in_flight"] == [ExecutionStatus.STARTED]
    assert seen["watch"] is WatchState.IN_REANALYSIS


def test_an_execution_store_needs_the_trigger_it_is_answering():
    """Keyed on the watch alone, a watch that re-armed and triggered again would
    have its second analysis refused as a duplicate of its first."""

    with pytest.raises(EntryContractError, match="trigger transition"):
        _run(_job(_Provider(), InMemoryExecutionStore()), _watch(), trigger_transition_id=None)


def test_without_a_store_the_job_says_the_move_is_not_durable():
    """The claim that was false is now a note rather than a docstring."""

    decision = _run(EntryAnalysisJob(provider=_Provider(), canonical_text="C"), _watch())

    assert any("in memory only" in note for note in decision.notes)
    assert decision.analysis_execution_id is None


# ------------------------------- case 1: crashed before the model answered


def test_a_crash_before_the_answer_leaves_a_row_a_recovery_pass_can_find():
    store = InMemoryExecutionStore()
    watch = _watch()
    provider = _Provider(raises=RuntimeError("the provider timed out"))

    with pytest.raises(RuntimeError):
        _run(_job(provider, store), watch)

    # The watch has moved on, so a sweep for TRIGGER_HIT would walk straight
    # past this. The execution row is what makes it findable.
    assert watch.state is WatchState.IN_REANALYSIS
    plan = plan_recovery(store)
    # RETRY_PENDING, not FAILED. A call that did not come back says nothing
    # about the security, and failing it would strand the watch at
    # IN_REANALYSIS with nothing left to move it - a network blip would quietly
    # remove a stock from the system.
    assert plan.total == 1
    assert [e.status for e in store._rows.values()] == [ExecutionStatus.RETRY_PENDING]
    row = next(iter(store._rows.values()))
    assert row.attempt_count == 1
    assert "timed out" in row.last_transient_error
    assert row.status.is_open


def test_a_process_that_simply_died_is_still_in_flight_and_needs_the_model():
    """A provider error is recorded. A killed process records nothing, which is
    exactly the case recovery has to handle."""

    store = InMemoryExecutionStore()
    key = ExecutionKey(watch_id="w-1", trigger_transition_id=TRIGGER,
                       analysis_kind=AnalysisKind.REANALYSIS)
    store.begin(
        key,
        security_id="JP:LOCAL:1234",
        decision_cutoff_at=CUTOFF,
        provider_id="groq_hosted",
        provider_kind="HOSTED_LLM",
        model_id="a-model",
        run_id=None,
        now=LATER,
    )

    plan = plan_recovery(store)

    assert len(plan.call_the_model) == 1
    assert plan.resume_from_stored_answer == []
    assert "TRIGGER_HIT" in plan.summary["note"]


# -------------------------- case 2: crashed after the answer was stored


def test_a_stored_answer_is_resumed_rather_than_asked_for_again():
    """Not only to avoid paying twice: a second call could return a different
    answer, and then which one was the analysis for this trigger has no answer."""

    store = InMemoryExecutionStore()

    # First attempt: the answer is stored and then the process dies before the
    # decision is made. Written out by hand rather than through the job, because
    # the job does not offer a way to die halfway.
    watch = _watch()
    key = ExecutionKey(watch_id="w-1", trigger_transition_id=TRIGGER,
                       analysis_kind=AnalysisKind.REANALYSIS)
    begun = store.begin(
        key,
        security_id="JP:LOCAL:1234",
        decision_cutoff_at=CUTOFF,
        provider_id="groq_hosted",
        provider_kind="HOSTED_LLM",
        model_id="a-model",
        run_id=None,
        now=LATER,
    )
    watch.begin_reanalysis(at=LATER)
    store.record_answer(
        begun.execution.analysis_execution_id,
        _entry(),
        prompt_sha256="p" * 64,
        bundle_sha256="b" * 64,
        canonical_prompt_sha256=CANONICAL,
    )

    plan = plan_recovery(store)
    assert len(plan.resume_from_stored_answer) == 1
    assert watch.state is WatchState.IN_REANALYSIS

    # Restart. A provider that would answer differently is deliberately used.
    second = _Provider(response=_entry(rationale="a different answer entirely"))
    decision = _run(_job(second, store), watch)

    assert second.calls == 0
    assert decision.resumed
    assert decision.analysis.rationale.startswith("cleared the level")
    assert decision.created_a_prediction


# --------------------------- case 3: crashed after the decision committed


def test_a_completed_analysis_is_not_run_again():
    """Two predictions from one trigger would make repeating yourself look like
    being right twice."""

    store = InMemoryExecutionStore()
    provider = _Provider()
    watch = _watch()

    first = _run(_job(provider, store), watch)
    assert first.created_a_prediction
    assert provider.calls == 1

    # The process dies after the commit and the same trigger comes round again.
    # The watch is ENTERED by now - which is exactly why the record has to be
    # consulted before the watch state.
    assert watch.state is WatchState.ENTERED
    second = _run(_job(provider, store), watch)

    assert second.already_decided
    assert second.prediction is None
    assert second.attempt is None
    assert provider.calls == 1
    assert "nothing was re-run" in " ".join(second.notes)


def test_the_idempotency_key_lets_a_rearmed_watch_be_analysed_again():
    """A watch legitimately re-arms and triggers again, and that second trigger
    deserves its own analysis."""

    store = InMemoryExecutionStore()
    provider = _Provider()
    watch = _watch()

    # The first analysis rejects, so the watch re-arms rather than entering.
    rejecting = _Provider(
        response=EntryAnalysisResponse(
            state=EntryAnalysisState.REJECT,
            rationale="already priced",
            provider_id="groq_hosted",
            provider_kind=ProviderKind.HOSTED_LLM,
        )
    )
    first_id = _run(_job(rejecting, store), watch).analysis_execution_id
    assert watch.state is WatchState.REARMED

    # A new trigger on the same watch: different transition id.
    watch.trigger(at=LATER, price=Decimal("1010"))
    second = _run(_job(provider, store), watch, trigger_transition_id=TRIGGER + 1)

    assert not second.already_decided
    # A second, distinct analysis really did run for the second trigger.
    assert provider.calls == 1
    assert second.analysis_execution_id != first_id
    assert len(store._rows) == 2


# ------------------------------------------------------ outcomes recorded


def test_a_failed_contract_records_a_failed_execution():
    store = InMemoryExecutionStore()
    watch = _watch()

    decision = _run(
        _job(_Provider(response=_entry(proposed_initial_failure_line=None)), store), watch
    )

    assert decision.failure_class is FailureClass.CONTRACT_VIOLATION
    execution = store.get(decision.analysis_execution_id)
    assert execution.status is ExecutionStatus.FAILED
    assert "initial failure line" in execution.failure_detail


def test_a_stand_in_records_a_failed_execution_and_no_attempt():
    store = InMemoryExecutionStore()
    watch = _watch()
    job = EntryAnalysisJob(
        provider=DeterministicIntradayStandIn(), canonical_text="C", executions=store
    )

    decision = _run(job, watch)

    assert decision.attempt is None
    assert store.get(decision.analysis_execution_id).failure_class is FailureClass.STAND_IN_PROVIDER


def test_a_completed_execution_carries_what_it_produced():
    store = InMemoryExecutionStore()

    decision = _run(_job(_Provider(), store), _watch())
    execution = store.get(decision.analysis_execution_id)

    assert execution.status is ExecutionStatus.COMPLETED
    assert execution.validation is not None
    assert execution.completed_at == LATER


def test_an_execution_has_one_outcome():
    """The in-memory store enforces what the database enforces; a test against a
    laxer store would prove nothing about production."""

    store = InMemoryExecutionStore()
    decision = _run(_job(_Provider(), store), _watch())

    with pytest.raises(ExecutionError, match="already COMPLETED"):
        store.fail(
            decision.analysis_execution_id,
            failure_class=FailureClass.PROVIDER_ERROR,
            failure_detail="after the fact",
            now=LATER,
        )


def test_a_prediction_cannot_be_recorded_without_its_attempt():
    store = InMemoryExecutionStore()
    key = ExecutionKey(watch_id="w-9", trigger_transition_id=1,
                       analysis_kind=AnalysisKind.ENTRY_DECISION)
    begun = store.begin(
        key,
        security_id="s",
        decision_cutoff_at=CUTOFF,
        provider_id="p",
        provider_kind="HOSTED_LLM",
        model_id=None,
        run_id=None,
        now=LATER,
    )

    from surge.analysis.entry_analysis import EntryValidation
    from surge.analysis.validate import ValidationStatus

    with pytest.raises(ExecutionError, match="without the attempt"):
        store.complete(
            begun.execution.analysis_execution_id,
            validation=EntryValidation(status=ValidationStatus.PASSED),
            entry_attempt_id=None,
            prediction_id="pred-1",
            now=LATER,
        )


def test_an_end_of_day_kind_is_not_an_intraday_execution():
    with pytest.raises(ExecutionError, match="not an intraday analysis"):
        ExecutionKey(watch_id="w", trigger_transition_id=1, analysis_kind=AnalysisKind.EOD)


def test_the_attempt_status_still_decides_the_watch_state_with_a_store():
    store = InMemoryExecutionStore()
    watch = _watch()

    decision = _run(
        _job(_Provider(), store),
        watch,
        entry_price=ObservedPrice(amount=Decimal("3100"), currency="JPY", observed_at=LATER),
    )

    assert decision.attempt.status is EntryAttemptStatus.ENTRY_ABORTED_PRICE_LIMIT
    assert decision.watch_state_after is WatchState.REARMED
    assert store.get(decision.analysis_execution_id).status is ExecutionStatus.COMPLETED


# ----------------------------------------- the budget on "transient"


def _open_execution(*, attempts: int = 0, started: datetime | None = None) -> AnalysisExecution:
    return AnalysisExecution(
        analysis_execution_id="e-1",
        key=ExecutionKey(watch_id="w-1", trigger_transition_id=TRIGGER,
                         analysis_kind=AnalysisKind.REANALYSIS),
        security_id="JP:LOCAL:1234",
        status=ExecutionStatus.RETRY_PENDING,
        decision_cutoff_at=CUTOFF,
        provider_id="groq_hosted",
        provider_kind="HOSTED_LLM",
        started_at=started or LATER,
        attempt_count=attempts,
    )


def test_a_transient_failure_has_a_budget():
    """Otherwise the fix for the stranded watch rebuilds it slowly. A provider
    that is down all afternoon is transient on every single attempt and
    permanent in effect, and the watch sits at IN_REANALYSIS throughout."""

    assert _open_execution(attempts=0).retry_budget_spent(LATER) is None
    assert _open_execution(attempts=MAX_TRANSIENT_ATTEMPTS - 1).retry_budget_spent(LATER) is None
    assert (
        _open_execution(attempts=MAX_TRANSIENT_ATTEMPTS).retry_budget_spent(LATER)
        is FailureClass.RETRY_EXHAUSTED
    )


def test_an_analysis_that_took_too_long_is_terminal_however_few_attempts_it_made():
    """One slow attempt spends the budget as surely as five fast ones, and the
    clock that matters is the cutoff: an entry decision is about a price, and
    half an hour after the inputs were frozen it is about a different
    security."""

    late = CUTOFF + ANALYSIS_DEADLINE + timedelta(seconds=1)

    assert _open_execution(attempts=1).retry_budget_spent(late) is (
        FailureClass.ANALYSIS_DEADLINE_PASSED
    )
    assert _open_execution(attempts=1).retry_budget_spent(
        CUTOFF + ANALYSIS_DEADLINE - timedelta(seconds=1)
    ) is None


def test_the_escalated_classes_are_terminal():
    """They have to be, or the escalation would retry again."""

    assert not FailureClass.RETRY_EXHAUSTED.is_transient
    assert not FailureClass.ANALYSIS_DEADLINE_PASSED.is_transient
