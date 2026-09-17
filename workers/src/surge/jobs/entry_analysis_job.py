"""TRIGGER_HIT to a decision, in one place, with a real model.

This is the join the project has been missing. Until now the intraday half
existed as parts: a watch machine that refuses ``TRIGGER_HIT -> ENTERED``, a
decision function that turns a verdict into a prediction, and an analysis layer
that could not say ENTRY at all. Nothing ran the three of them in sequence
against one security, so the path a real entry would take had never been
travelled end to end.

The sequence, and why each step is where it is:

1. the watch reached its trigger. That is an observation, and nothing follows
   from it (CLAUDE.md 1-5).
2. the watch moves to ``IN_REANALYSIS`` **before** the model is called, and the
   execution is recorded in the same breath. Given an :class:`ExecutionStore`
   backed by the database, that is one transaction and a crash leaves a row
   saying an analysis was under way. Given no store, the move is in memory only
   and survives nothing - so the job says which it is rather than claiming
   durability it does not have.
3. the model answers under the intraday contract, which is the only contract
   with ENTRY in it.
4. the answer is validated against facts this system measured. A failed
   validation ends here: it does not become a quieter verdict, because a
   malformed ENTRY recorded as a REJECT would put a rejection in the ledger that
   the analysis never made.
5. only then does :func:`surge.entry.decision.decide` run, and it re-checks
   everything that matters rather than trusting step 4.

The watch's final state is a function of what the decision came to, and the
mapping is a table below rather than a chain of ifs, because "what happens to the
watch when the entry price came back over the limit" is a rule and should be
readable as one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from surge.analysis.entry_analysis import (
    EntryAnalysisResponse,
    EntryAnalysisState,
    EntryContractError,
    EntryGuardFacts,
    EntryValidation,
    IntradayBundle,
    render_entry_prompt,
    to_entry_request,
    validate_entry_analysis,
)
from surge.analysis.execution import (
    ExecutionKey,
    ExecutionStore,
    FailureClass,
)
from surge.analysis.llm import LLMRequest
from surge.entry.decision import decide
from surge.entry.models import (
    AnalysisKind,
    EntryAttempt,
    EntryAttemptStatus,
    ObservedPrice,
    Prediction,
    VerificationStatus,
    WatchState,
)
from surge.entry.watch import Watch

JOB_VERSION = "entry-analysis-job-1.0.0"


#: What becomes of the watch, given what the decision came to.
#:
#: ``REARMED`` wherever the condition could legitimately fire again - an entry
#: price that came back over 3,000 yen is a fact about this moment, and
#: CLAUDE.md 1-4 says a later move back under the limit is a *new* decision, so
#: the watch has to still be there to make it. ``REJECTED`` only where the answer
#: cannot change today: a security outside the universe is not going to be inside
#: it before the close.
WATCH_AFTER: dict[EntryAttemptStatus, WatchState] = {
    EntryAttemptStatus.PREDICTION_CREATED: WatchState.ENTERED,
    EntryAttemptStatus.REJECTED_NOT_IN_UNIVERSE: WatchState.REJECTED,
    EntryAttemptStatus.REJECTED_BY_ANALYSIS: WatchState.REARMED,
    EntryAttemptStatus.REJECTED_HARD_FILTER_AT_DECISION: WatchState.REARMED,
    EntryAttemptStatus.ENTRY_ABORTED_PRICE_LIMIT: WatchState.REARMED,
    EntryAttemptStatus.NO_ENTRY_REFERENCE_PRICE: WatchState.REARMED,
    EntryAttemptStatus.REAFFIRMED_EXISTING_EPISODE: WatchState.REARMED,
}


@dataclass
class IntradayDecision:
    """Everything one pass produced, whether or not it produced a prediction.

    ``analysis`` is None when the pass stopped before the model was called - the
    coverage precondition below. That is a third outcome alongside "answered" and
    "answered badly", and giving it its own shape keeps a skipped pass from being
    read later as a model that declined.
    """

    security_id: str
    watch_id: str
    bundle_sha256: str
    watch_state_after: WatchState
    analysis: EntryAnalysisResponse | None = None
    validation: EntryValidation | None = None
    prompt_sha256: str | None = None
    attempt: EntryAttempt | None = None
    prediction: Prediction | None = None
    skipped_reason: str | None = None
    analysis_execution_id: str | None = None
    #: True when this pass resumed a crashed one rather than starting fresh.
    resumed: bool = False
    #: Set when a completed execution already existed for this trigger. Nothing
    #: is re-run and nothing is re-decided.
    already_decided: bool = False
    failure_class: FailureClass | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def created_a_prediction(self) -> bool:
        return self.prediction is not None

    @property
    def ran_the_model(self) -> bool:
        return self.analysis is not None

    @property
    def summary(self) -> dict:
        return {
            "security_id": self.security_id,
            "watch_id": self.watch_id,
            "analysis_state": self.analysis.state.value if self.analysis else None,
            "validation": self.validation.status.value if self.validation else None,
            "validation_errors": list(self.validation.errors) if self.validation else [],
            "system_refusals": list(self.validation.system_refusals) if self.validation else [],
            "skipped_reason": self.skipped_reason,
            "analysis_execution_id": self.analysis_execution_id,
            "resumed": self.resumed,
            "already_decided": self.already_decided,
            "failure_class": self.failure_class.value if self.failure_class else None,
            "attempt_status": self.attempt.status.value if self.attempt else None,
            "created_a_prediction": self.created_a_prediction,
            "watch_state_after": self.watch_state_after.value,
            "bundle_sha256": self.bundle_sha256,
            "notes": list(self.notes),
        }


@dataclass
class EntryAnalysisJob:
    """One intraday analysis provider, bound to the canonical prompt it sends."""

    provider: object
    canonical_text: str
    addenda_texts: tuple[str, ...] = ()
    version: str = JOB_VERSION
    #: Where executions are recorded. Without one the watch move is in memory
    #: and a crash loses it; the job says so in its notes rather than letting a
    #: docstring imply otherwise.
    executions: ExecutionStore | None = None

    def run_for_watch(
        self,
        *,
        watch: Watch,
        bundle: IntradayBundle,
        facts: EntryGuardFacts,
        thesis_key: str,
        now: datetime,
        trigger_transition_id: int | None = None,
        entry_price: ObservedPrice | None = None,
        entry_price_method: str | None = None,
        setup_ids: tuple[str, ...] = (),
        open_episode=None,
        run_id: str | None = None,
        analysis_kind: AnalysisKind = AnalysisKind.REANALYSIS,
        verification: VerificationStatus = VerificationStatus.IMPLEMENTED_NOT_LIVE_VERIFIED,
    ) -> IntradayDecision:
        # The watch-state precondition is checked below rather than here, and
        # the order matters. Moving the watch is the *first* thing a run does,
        # so after a crash it is no longer at TRIGGER_HIT - and refusing every
        # recovery with a complaint about a state the crash itself caused would
        # defeat the record entirely. With a store, the record is consulted
        # first; without one there is nothing to consult and the check stands.
        if self.executions is None and watch.state is not WatchState.TRIGGER_HIT:
            raise EntryContractError(
                f"watch {watch.watch_id} is {watch.state.value}; a reanalysis runs from "
                "TRIGGER_HIT. Reaching the trigger is the observation, this is the decision, and "
                "the two must not be collapsed (CLAUDE.md 1-5)"
            )

        # Coverage is a precondition of running at all, not a finding about the
        # security. An entry decided on partial inputs is not comparable with one
        # decided on complete inputs, and afterwards nothing distinguishes them -
        # so the model is not asked, and the watch stays where it is. The trigger
        # has been hit; a later pass with the inputs present can still act on it.
        if not facts.coverage_meets_requirements:
            # No execution row either: the analysis did not start, and a row
            # saying it did would misrepresent what happened.
            return IntradayDecision(
                security_id=bundle.security_id,
                watch_id=watch.watch_id,
                bundle_sha256=bundle.bundle_sha256,
                watch_state_after=watch.state,
                skipped_reason=(
                    f"the coverage an entry requires was not met: {facts.coverage_detail}"
                ),
                notes=[
                    "no analysis was run and no attempt was recorded. The trigger stays hit, so a "
                    "later pass can act on it once the inputs are there"
                ],
            )

        notes: list[str] = []
        prompt = render_entry_prompt(bundle, self.canonical_text, self.addenda_texts)
        request = LLMRequest(prompt=prompt, bundle=bundle)
        provider_id = getattr(self.provider, "provider_id", "?")
        execution_id: str | None = None
        resumed = False
        stored_answer = None

        # The record and the watch move go together, before the model is called.
        # With a database-backed store that is one transaction; a crash after it
        # leaves a row that says an analysis was under way, which is the whole
        # point of doing it in this order.
        if self.executions is not None:
            if trigger_transition_id is None:
                raise EntryContractError(
                    "an execution store needs the trigger transition this analysis is answering. "
                    "Keying on the watch alone would refuse the second analysis of a watch that "
                    "legitimately re-armed and triggered again"
                )
            key = ExecutionKey(
                watch_id=watch.watch_id,
                trigger_transition_id=trigger_transition_id,
                analysis_kind=analysis_kind,
            )
            begun = self.executions.begin(
                key,
                security_id=bundle.security_id,
                decision_cutoff_at=facts.decision_cutoff_at,
                provider_id=provider_id,
                provider_kind=getattr(
                    getattr(self.provider, "provider_kind", None), "value", "UNKNOWN"
                ),
                model_id=getattr(self.provider, "model_id", None),
                run_id=run_id,
                now=now,
            )
            execution_id = begun.execution.analysis_execution_id

            if begun.already_decided:
                # Case three: the decision committed and then the process died.
                # A second prediction from one trigger would make repeating
                # yourself look like being right twice.
                return IntradayDecision(
                    security_id=bundle.security_id,
                    watch_id=watch.watch_id,
                    bundle_sha256=bundle.bundle_sha256,
                    watch_state_after=watch.state,
                    analysis_execution_id=execution_id,
                    already_decided=True,
                    skipped_reason=(
                        f"this trigger was already analysed and the analysis is "
                        f"{begun.execution.status.value}"
                    ),
                    notes=[
                        "nothing was re-run and nothing was re-decided. The idempotency key is "
                        "the watch, the trigger transition and the analysis kind"
                    ],
                )

            if begun.created:
                # A new analysis, so the trigger really does have to be the
                # thing being answered. The watch machine says the same, and
                # this says it in the job's own words.
                if watch.state is not WatchState.TRIGGER_HIT:
                    raise EntryContractError(
                        f"watch {watch.watch_id} is {watch.state.value}; a new reanalysis runs "
                        "from TRIGGER_HIT. Reaching the trigger is the observation, this is the "
                        "decision, and the two must not be collapsed (CLAUDE.md 1-5)"
                    )
                watch.begin_reanalysis(at=now, note=f"{self.version} via {provider_id}")
            else:
                resumed = True
                stored_answer = begun.execution.stored_answer
                notes.append(
                    "resumed an analysis that was started and never finished"
                    + (
                        "; the model had already answered, so that answer is used rather than "
                        "asking again - a second call could return something different, and then "
                        "which one was the analysis for this trigger would have no answer"
                        if stored_answer is not None
                        else "; the model had not answered yet"
                    )
                )
        else:
            watch.begin_reanalysis(at=now, note=f"{self.version} via {provider_id}")
            notes.append(
                "no execution store was supplied, so this watch move is in memory only and a "
                "crash would lose it"
            )

        if stored_answer is not None:
            response = stored_answer
        else:
            try:
                response = self.provider.analyse_entry(request)
            except Exception as exc:
                # Retried, not failed. A call that did not come back says nothing
                # about the security, and failing it would strand the watch at
                # IN_REANALYSIS with nothing left to move it.
                if self.executions is not None and execution_id is not None:
                    self.executions.retry(
                        execution_id, transient_error=f"{type(exc).__name__}: {exc}"
                    )
                raise
            if self.executions is not None and execution_id is not None:
                self.executions.record_answer(
                    execution_id,
                    response,
                    prompt_sha256=request.prompt_sha256,
                    bundle_sha256=bundle.bundle_sha256,
                    canonical_prompt_sha256=bundle.canonical_prompt_sha256,
                )

        validation = validate_entry_analysis(response, facts, bundle=bundle)

        decision = IntradayDecision(
            security_id=bundle.security_id,
            watch_id=watch.watch_id,
            bundle_sha256=bundle.bundle_sha256,
            watch_state_after=watch.state,
            analysis=response,
            validation=validation,
            prompt_sha256=request.prompt_sha256,
            analysis_execution_id=execution_id,
            resumed=resumed,
            notes=notes,
        )

        # --- the answer did not meet the contract ---------------------------
        if not validation.may_become_a_prediction:
            watch.rearm(at=now, note="the intraday answer failed the entry contract")
            decision.watch_state_after = watch.state
            notes.append(
                "the analysis output is stored with validation REJECTED and produced no entry "
                "attempt. It never became a decision, so recording it as one would put a verdict "
                "in the ledger that nothing made"
            )
            decision.failure_class = FailureClass.CONTRACT_VIOLATION
            if self.executions is not None and execution_id is not None:
                # After the rearm above, never before: a terminal failure whose
                # watch is still IN_REANALYSIS strands the security, and the
                # database refuses it for that reason.
                self.executions.fail(
                    execution_id,
                    failure_class=FailureClass.CONTRACT_VIOLATION,
                    failure_detail="; ".join(validation.errors),
                    now=now,
                    validation=validation,
                )
            return decision

        if validation.system_refusals:
            # Well-formed, and the system will not act on it. This is exactly the
            # case the entry ledger exists for, so it goes on to the decision.
            notes.extend(validation.system_refusals)

        # --- a stand-in may exercise the path and may not decide -------------
        if response.is_a_stand_in:
            watch.rearm(at=now, note="a stand-in cannot decide; the watch stays open")
            decision.watch_state_after = watch.state
            notes.append(
                "the provider is the deterministic stand-in. The path ran and no entry attempt was "
                "recorded: its verdicts are evidence the pipeline works and no evidence about this "
                "security"
            )
            decision.failure_class = FailureClass.STAND_IN_PROVIDER
            if self.executions is not None and execution_id is not None:
                self.executions.fail(
                    execution_id,
                    failure_class=FailureClass.STAND_IN_PROVIDER,
                    failure_detail="a deterministic stand-in cannot produce a formal prediction",
                    now=now,
                    validation=validation,
                )
            return decision

        # --- a real verdict, handed to the Phase 8 decision ------------------
        entry_request = to_entry_request(
            response,
            facts,
            security_id=bundle.security_id,
            thesis_key=thesis_key,
            bundle=bundle,
            analysis_kind=analysis_kind,
            entry_price=entry_price,
            entry_price_method=entry_price_method,
            setup_ids=setup_ids,
            watch_id=watch.watch_id,
            open_episode=open_episode,
            run_id=run_id,
            prompt_sha256=request.prompt_sha256,
            verification=verification,
            validation=validation,
        )
        outcome = decide(entry_request)
        decision.attempt = outcome.attempt
        decision.prediction = outcome.prediction
        notes.extend(outcome.notes)

        target = WATCH_AFTER[outcome.attempt.status]
        if target is WatchState.ENTERED:
            watch.enter(at=now, note=f"prediction created at {response.decision_price_used}")
        elif target is WatchState.REJECTED:
            watch.reject(at=now, note=outcome.attempt.reject_reason)
        else:
            watch.rearm(at=now, note=outcome.attempt.reject_reason or outcome.attempt.status.value)
        decision.watch_state_after = watch.state

        if self.executions is not None and execution_id is not None:
            self.executions.complete(
                execution_id,
                validation=validation,
                entry_attempt_id=getattr(outcome.attempt, "attempt_id", None),
                prediction_id=getattr(outcome.prediction, "prediction_id", None),
                now=now,
            )
        return decision


def states_that_can_enter() -> frozenset[EntryAnalysisState]:
    """The one state that can become a prediction, stated so a test can assert it."""

    return frozenset({EntryAnalysisState.ENTRY})


__all__ = ["JOB_VERSION", "WATCH_AFTER", "EntryAnalysisJob", "IntradayDecision", "states_that_can_enter"]
