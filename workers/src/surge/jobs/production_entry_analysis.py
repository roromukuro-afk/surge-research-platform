"""The intraday analysis as production runs it: three transactions, in order.

:mod:`surge.jobs.entry_analysis_job` composes the pieces and is where the rules
live. It is not, on its own, durable: it calls a store that never commits, so
"the record exists before the model is called" was true of an uncommitted
snapshot and of nothing else. A process killed at that moment left no row.

Durability comes from where the transaction *ends*, so the work is split into
three, and each boundary is a point a crash can be survived from:

``TX1``  verify the trigger, move the watch to IN_REANALYSIS, record a STARTED
         execution, **commit**. The external model is called only after this
         returns. A crash now leaves a findable row.
``TX2``  store the answer and the hashes of what produced it, **commit**. A
         crash now resumes from the stored answer rather than paying for - and
         risking a different - second one.
``TX3``  validate, decide, write the attempt, the episode and the prediction,
         move the watch to where the decision put it, complete the execution,
         **commit**. All of it or none of it: a prediction whose attempt rolled
         back would be a claim with no decision behind it.

TX3 is deliberately one transaction rather than several. The audit's fourth
crash case is the reason: if the prediction insert fails, the attempt, the
episode, the watch move and the completion have to go with it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime

from surge.analysis.entry_analysis import (
    EntryAnalysisResponse,
    EntryContractError,
    EntryGuardFacts,
    IntradayBundle,
    render_entry_prompt,
    to_entry_request,
    validate_entry_analysis,
)
from surge.analysis.execution import (
    MAX_TRANSIENT_ATTEMPTS,
    ExecutionKey,
    FailureClass,
)
from surge.analysis.llm import LLMRequest
from surge.entry import db as entry_db
from surge.entry.decision import decide
from surge.entry.models import (
    AnalysisKind,
    EntryAttemptStatus,
    Episode,
    ObservedPrice,
    OpenEpisode,
    TransitionKind,
    VerificationStatus,
    WatchState,
)
from surge.jobs.entry_analysis_job import WATCH_AFTER

RUNNER_VERSION = "production-entry-analysis-1.0.0"


class ProductionRunError(EntryContractError):
    """The run could not proceed, and nothing was left half-written."""


@dataclass
class RunResult:
    """What one production pass committed."""

    analysis_execution_id: str | None = None
    security_id: str | None = None
    watch_state_after: WatchState | None = None
    attempt_id: str | None = None
    attempt_status: EntryAttemptStatus | None = None
    prediction_id: str | None = None
    episode_id: str | None = None
    already_decided: bool = False
    resumed: bool = False
    retried: bool = False
    failure_class: FailureClass | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def created_a_prediction(self) -> bool:
        return self.prediction_id is not None

    @property
    def summary(self) -> dict:
        return {
            "analysis_execution_id": self.analysis_execution_id,
            "security_id": self.security_id,
            "attempt_id": self.attempt_id,
            "attempt_status": self.attempt_status.value if self.attempt_status else None,
            "prediction_id": self.prediction_id,
            "episode_id": self.episode_id,
            "watch_state_after": (
                self.watch_state_after.value if self.watch_state_after else None
            ),
            "already_decided": self.already_decided,
            "resumed": self.resumed,
            "retried": self.retried,
            "failure_class": self.failure_class.value if self.failure_class else None,
            "notes": list(self.notes),
        }


def _transient_class(exc: BaseException) -> FailureClass:
    """Which transient failure this was, for the record rather than the flow.

    All three are retried identically, so this changes nothing about what
    happens - but "PROVIDER_ERROR" on every timeout makes the one column that
    could tell an outage from a rate limit say the same thing either way.
    """

    if isinstance(exc, TimeoutError):
        return FailureClass.PROVIDER_TIMEOUT
    if "rate limit" in str(exc).lower() or type(exc).__name__ == "RateLimited":
        return FailureClass.PROVIDER_RATE_LIMITED
    return FailureClass.PROVIDER_ERROR


@dataclass
class ProductionEntryAnalysis:
    """One provider, one connection, three transactions."""

    conn: object
    store: object
    provider: object
    canonical_text: str
    addenda_texts: tuple[str, ...] = ()
    version: str = RUNNER_VERSION

    # ------------------------------------------------------------------ run

    def run_for_trigger(
        self,
        *,
        watch_id: str,
        trigger_transition_id: int,
        bundle: IntradayBundle,
        facts: EntryGuardFacts,
        thesis_key: str,
        now: datetime,
        entry_price: ObservedPrice | None = None,
        entry_price_method: str | None = None,
        setup_ids: tuple[str, ...] = (),
        run_id: str | None = None,
        analysis_kind: AnalysisKind = AnalysisKind.REANALYSIS,
        verification: VerificationStatus = VerificationStatus.IMPLEMENTED_NOT_LIVE_VERIFIED,
    ) -> RunResult:
        result = RunResult()
        key = ExecutionKey(
            watch_id=watch_id,
            trigger_transition_id=trigger_transition_id,
            analysis_kind=analysis_kind,
        )

        # Coverage is a precondition of running at all, and it is checked before
        # TX1 so that a pass which cannot legitimately proceed leaves no trace of
        # having started. An execution row saying an analysis began would be a
        # record of something that did not happen.
        if not facts.coverage_meets_requirements:
            result.notes.append(
                f"the coverage an entry requires was not met: {facts.coverage_detail}. No "
                "analysis was started and the trigger stays unanswered"
            )
            result.failure_class = FailureClass.COVERAGE_NOT_MET
            return result

        # ---------------------------------------------------------- TX1
        begun = self._tx1(key, facts=facts, now=now, run_id=run_id)
        execution = begun.execution
        result.analysis_execution_id = execution.analysis_execution_id
        result.security_id = execution.security_id

        if begun.already_decided:
            result.already_decided = True
            result.attempt_id = execution.entry_attempt_id
            result.prediction_id = execution.prediction_id
            result.notes.append(
                f"this trigger was already analysed and the analysis is "
                f"{execution.status.value}; nothing was re-run and nothing was re-decided"
            )
            return result

        # ---------------------------------------------------------- TX2
        response = execution.stored_answer
        if response is None:
            prompt = render_entry_prompt(bundle, self.canonical_text, self.addenda_texts)
            request = LLMRequest(prompt=prompt, bundle=bundle)
            answer = self._tx2(execution, request, bundle, now=now, result=result)
            if answer is None:
                # _tx2 has already recorded which of the two it was - a retry
                # that leaves the execution open, or a terminal failure that
                # moved the watch. Setting it again here is how the result came
                # to say "retried, transient" about an execution the database
                # had recorded as FAILED.
                return result
            response = answer
        else:
            result.resumed = True
            result.notes.append(
                "resumed from the stored answer rather than asking again; a second call could "
                "return something different, and then which one was the analysis for this "
                "trigger would have no answer"
            )

        # ---------------------------------------------------------- TX3
        return self._tx3(
            result,
            execution=execution,
            response=response,
            bundle=bundle,
            facts=facts,
            thesis_key=thesis_key,
            now=now,
            entry_price=entry_price,
            entry_price_method=entry_price_method,
            setup_ids=setup_ids,
            run_id=run_id,
            analysis_kind=analysis_kind,
            verification=verification,
        )

    # ----------------------------------------------------------------- TX1

    def _tx1(self, key: ExecutionKey, *, facts, now, run_id):
        """Move the watch and record the analysis. Commit before the model."""

        try:
            begun = self.store.begin(
                key,
                decision_cutoff_at=facts.decision_cutoff_at,
                provider_id=getattr(self.provider, "provider_id", "?"),
                provider_kind=getattr(
                    getattr(self.provider, "provider_kind", None), "value", "UNKNOWN"
                ),
                model_id=getattr(self.provider, "model_id", None),
                run_id=run_id,
                now=now,
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return begun

    # ----------------------------------------------------------------- TX2

    def _tx2(self, execution, request, bundle, *, now, result) -> EntryAnalysisResponse | None:
        """Call the model and store the answer. Returns None when it did not answer."""

        execution_id = execution.analysis_execution_id
        try:
            response = self.provider.analyse_entry(request)
        except Exception as exc:
            # Transient by default at this boundary: a call that did not come
            # back tells us nothing about the security, and failing it would
            # strand the watch at IN_REANALYSIS with nothing to move it.
            #
            # But only while there is budget. A provider that is down all
            # afternoon is transient on every individual attempt and permanent
            # in effect, and retrying it forever rebuilds the stranded watch in
            # slow motion. When the budget is gone the failure becomes terminal,
            # which means moving the watch first - the database refuses to fail
            # an execution whose watch is still IN_REANALYSIS, so this cannot be
            # skipped by forgetting it.
            terminal = execution.retry_budget_spent(now)
            detail = f"{type(exc).__name__}: {exc}"
            if terminal is None:
                try:
                    self.store.retry(execution_id, transient_error=detail)
                    self.conn.commit()
                except Exception:
                    self.conn.rollback()
                    raise
                result.retried = True
                result.failure_class = _transient_class(exc)
                result.notes.append(
                    f"a transient provider failure ({detail}); the execution stays open and the "
                    f"watch stays IN_REANALYSIS, because it is. Retry "
                    f"{execution.attempt_count + 1} of {MAX_TRANSIENT_ATTEMPTS}"
                )
                return None

            try:
                entry_db.write_watch_transition(
                    self.conn,
                    watch_id=execution.key.watch_id,
                    from_state=WatchState.IN_REANALYSIS,
                    to_state=WatchState.REARMED,
                    occurred_at=now,
                    analysis_kind=AnalysisKind.REANALYSIS,
                    note=f"{terminal.value}: {detail}"[:200],
                )
                self.store.fail(
                    execution_id,
                    failure_class=terminal,
                    failure_detail=detail,
                )
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
            result.failure_class = terminal
            result.watch_state_after = WatchState.REARMED
            result.notes.append(
                "the trigger goes unanswered and the watch re-arms. A decision made from a "
                "provider that never answered would be a decision about nothing"
            )
            return None

        try:
            self.store.record_answer(
                execution_id,
                response,
                prompt_sha256=request.prompt_sha256,
                bundle_sha256=bundle.bundle_sha256,
                canonical_prompt_sha256=bundle.canonical_prompt_sha256,
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return response

    # ----------------------------------------------------------------- TX3

    def _tx3(
        self,
        result: RunResult,
        *,
        execution,
        response,
        bundle,
        facts,
        thesis_key,
        now,
        entry_price,
        entry_price_method,
        setup_ids,
        run_id,
        analysis_kind,
        verification,
    ) -> RunResult:
        """Validate, decide, write everything, move the watch, complete. Once."""

        watch_id = execution.key.watch_id
        try:
            validation = validate_entry_analysis(response, facts, bundle=bundle)

            # --- an answer that cannot become a decision ---------------------
            if not validation.may_become_a_prediction or response.is_a_stand_in:
                failure = (
                    FailureClass.STAND_IN_PROVIDER
                    if response.is_a_stand_in
                    else FailureClass.CONTRACT_VIOLATION
                )
                detail = (
                    "a deterministic stand-in cannot produce a formal prediction"
                    if response.is_a_stand_in
                    else "; ".join(validation.errors)
                )
                # The watch is moved first. Failing an execution whose watch is
                # still IN_REANALYSIS is refused by the database, precisely so
                # this cannot be forgotten.
                entry_db.write_watch_transition(
                    self.conn,
                    watch_id=watch_id,
                    from_state=WatchState.IN_REANALYSIS,
                    to_state=WatchState.REARMED,
                    occurred_at=now,
                    analysis_kind=AnalysisKind.REANALYSIS,
                    note=detail[:200],
                )
                self.store.fail(
                    execution.analysis_execution_id,
                    failure_class=failure,
                    failure_detail=detail,
                    validation=validation,
                )
                self.conn.commit()
                result.failure_class = failure
                result.watch_state_after = WatchState.REARMED
                result.notes.append(
                    "no entry attempt was recorded: the analysis never became a decision, and "
                    "recording one would put a verdict in the ledger that nothing made"
                )
                return result

            # --- a real verdict ----------------------------------------------
            open_episode = self._open_episode(execution.security_id, thesis_key)
            request_for_decision = to_entry_request(
                response,
                facts,
                security_id=execution.security_id,
                thesis_key=thesis_key,
                bundle=bundle,
                analysis_kind=analysis_kind,
                entry_price=entry_price,
                entry_price_method=entry_price_method,
                setup_ids=setup_ids,
                watch_id=watch_id,
                open_episode=open_episode,
                run_id=run_id,
                prompt_sha256=execution.prompt_sha256,
                verification=verification,
                validation=validation,
            )
            outcome = decide(request_for_decision)

            attempt_id = entry_db.write_attempt(self.conn, outcome.attempt)
            result.attempt_id = attempt_id
            result.attempt_status = outcome.attempt.status
            result.notes.extend(outcome.notes)

            episode_id = None
            prediction_id = None

            if outcome.prediction is not None:
                episode = Episode(
                    episode_id=str(uuid.uuid4()),
                    security_id=execution.security_id,
                    thesis_key=thesis_key,
                    entry_price_observed_at=outcome.prediction.entry_price_observed_at,
                    opened_at=now,
                )
                episode_id = entry_db.write_episode(self.conn, episode)
                prediction_id = entry_db.write_prediction(
                    self.conn,
                    outcome.prediction,
                    episode_id=episode_id,
                    attempt_id=attempt_id,
                )
                entry_db.write_transition(
                    self.conn,
                    episode_id=episode_id,
                    kind=TransitionKind.EPISODE_OPENED,
                    occurred_at=now,
                    analysis_kind=analysis_kind,
                )
            elif outcome.reaffirmed_episode_id is not None:
                episode_id = outcome.reaffirmed_episode_id
                entry_db.write_transition(
                    self.conn,
                    episode_id=episode_id,
                    kind=TransitionKind.REAFFIRMED,
                    occurred_at=now,
                    analysis_kind=analysis_kind,
                    note="the horizon still runs from the original entry (CLAUDE.md 1-6)",
                )

            result.episode_id = episode_id
            result.prediction_id = prediction_id

            target = WATCH_AFTER[outcome.attempt.status]
            entry_db.write_watch_transition(
                self.conn,
                watch_id=watch_id,
                from_state=WatchState.IN_REANALYSIS,
                to_state=target,
                occurred_at=now,
                analysis_kind=AnalysisKind.REANALYSIS,
                note=(outcome.attempt.reject_reason or outcome.attempt.status.value)[:200],
            )
            result.watch_state_after = target

            # The ids are the ones the database returned, not attributes read
            # off a dataclass: a Python object's idea of its own id is a guess
            # until an INSERT has answered.
            self.store.complete(
                execution.analysis_execution_id,
                validation=validation,
                entry_attempt_id=attempt_id,
                prediction_id=prediction_id,
            )
            self.conn.commit()
            return result

        except Exception:
            # Everything in TX3 or nothing. A prediction whose attempt rolled
            # back would be a claim with no decision behind it.
            self.conn.rollback()
            raise

    # ------------------------------------------------------------- helpers

    def _open_episode(self, security_id: str, thesis_key: str) -> OpenEpisode | None:
        row = entry_db.read_open_episode(
            self.conn, security_id=security_id, thesis_key=thesis_key
        )
        if row is None:
            return None
        return OpenEpisode(
            episode_id=str(row["episode_id"]),
            security_id=security_id,
            thesis_key=thesis_key,
            entry_price_observed_at=row["entry_price_observed_at"],
            opened_at=row["opened_at"],
        )


@dataclass
class Recovery:
    """What one sweep did, including what it could not do.

    ``unresumable`` is the part worth naming. Each of those is a watch sitting
    at IN_REANALYSIS that this pass did not move, and nothing else moves one. An
    earlier version skipped them with ``continue``, which made "nothing was
    stuck" and "several things were stuck and I walked past them" produce the
    same empty result.
    """

    resumed: list[RunResult] = field(default_factory=list)
    unresumable: list[dict] = field(default_factory=list)

    @property
    def summary(self) -> dict:
        return {
            "resumed": len(self.resumed),
            "unresumable": len(self.unresumable),
            "stuck": [u["analysis_execution_id"] for u in self.unresumable],
        }


def resume_all(runner: ProductionEntryAnalysis, *, bundles, facts_for, now, thesis_for) -> Recovery:
    """Pick up every analysis that was started and never finished.

    Reads the executions rather than the watches. By the time the model is
    called the watch has already moved to IN_REANALYSIS, so a sweep for
    TRIGGER_HIT walks past exactly the ones that crashed.
    """

    recovery = Recovery()
    for execution in runner.store.in_flight():
        bundle = bundles.get(execution.analysis_execution_id)
        if bundle is None:
            # Reported, not skipped. Without the bundle this pass cannot resume
            # the analysis, and the watch stays where the crash left it - which
            # is a finding, not a non-event.
            recovery.unresumable.append(
                {
                    "analysis_execution_id": execution.analysis_execution_id,
                    "watch_id": execution.key.watch_id,
                    "status": execution.status.value,
                    "started_at": execution.started_at.isoformat(),
                    "why": (
                        "no intraday bundle was supplied for this execution, so the analysis "
                        "cannot be resumed and the watch is still IN_REANALYSIS"
                    ),
                }
            )
            continue
        recovery.resumed.append(
            runner.run_for_trigger(
                watch_id=execution.key.watch_id,
                trigger_transition_id=execution.key.trigger_transition_id,
                bundle=bundle,
                facts=facts_for(execution),
                thesis_key=thesis_for(execution),
                now=now,
                analysis_kind=execution.key.analysis_kind,
            )
        )
    return recovery


__all__ = [
    "RUNNER_VERSION",
    "ProductionEntryAnalysis",
    "ProductionRunError",
    "Recovery",
    "RunResult",
    "resume_all",
]
