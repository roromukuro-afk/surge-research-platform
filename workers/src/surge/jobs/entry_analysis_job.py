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
2. the watch moves to ``IN_REANALYSIS`` **before** the model is called. If the
   process dies mid-analysis, the stored state says an analysis was under way
   rather than that a trigger was never acted on.
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

    def run_for_watch(
        self,
        *,
        watch: Watch,
        bundle: IntradayBundle,
        facts: EntryGuardFacts,
        thesis_key: str,
        now: datetime,
        entry_price: ObservedPrice | None = None,
        entry_price_method: str | None = None,
        setup_ids: tuple[str, ...] = (),
        open_episode=None,
        run_id: str | None = None,
        analysis_kind: AnalysisKind = AnalysisKind.REANALYSIS,
        verification: VerificationStatus = VerificationStatus.IMPLEMENTED_NOT_LIVE_VERIFIED,
    ) -> IntradayDecision:
        if watch.state not in (WatchState.TRIGGER_HIT,):
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

        # Moved before the call, not after it. A crash inside the model call
        # should leave a watch that says "an analysis was running", not one that
        # says "the trigger was never acted on".
        watch.begin_reanalysis(at=now, note=f"{self.version} via {getattr(self.provider, 'provider_id', '?')}")

        prompt = render_entry_prompt(bundle, self.canonical_text, self.addenda_texts)
        request = LLMRequest(prompt=prompt, bundle=bundle)
        response = self.provider.analyse_entry(request)
        validation = validate_entry_analysis(response, facts, bundle=bundle)

        notes: list[str] = []
        decision = IntradayDecision(
            security_id=bundle.security_id,
            watch_id=watch.watch_id,
            bundle_sha256=bundle.bundle_sha256,
            watch_state_after=watch.state,
            analysis=response,
            validation=validation,
            prompt_sha256=request.prompt_sha256,
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
        return decision


def states_that_can_enter() -> frozenset[EntryAnalysisState]:
    """The one state that can become a prediction, stated so a test can assert it."""

    return frozenset({EntryAnalysisState.ENTRY})


__all__ = ["JOB_VERSION", "WATCH_AFTER", "EntryAnalysisJob", "IntradayDecision", "states_that_can_enter"]
