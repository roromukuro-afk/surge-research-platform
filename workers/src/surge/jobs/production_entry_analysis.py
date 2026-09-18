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
from datetime import UTC, datetime

from surge.analysis.entry_analysis import (
    EntryAnalysisResponse,
    EntryAnalysisState,
    EntryContractError,
    EntryGuardFacts,
    IntradayBundle,
    to_entry_request,
    validate_entry_analysis,
)
from surge.analysis.execution import (
    MAX_TRANSIENT_ATTEMPTS,
    ExecutionKey,
    FailureClass,
    classify_provider_failure,
)
from surge.analysis.input_snapshot import (
    InputReconstructionError,
    build_stored_input,
    reconstruct,
)
from surge.analysis.llm import LLMRequest
from surge.entry import db as entry_db
from surge.entry.decision import decide
from surge.entry.models import (
    AnalysisKind,
    EntryAttemptStatus,
    Episode,
    OpenEpisode,
    TransitionKind,
    VerificationStatus,
    WatchState,
)
from surge.entry.price_observer import (
    ENTRY_PRICE_RECOVERY_DELAYED,
    EntryPriceObservation,
    EntryPriceUnavailable,
    NoEntryPriceSource,
    assert_observation_is_usable,
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
    #: When this system accepted the answer, by the runner's own clock. Every
    #: entry price is required to have been observed after it.
    decision_completed_at: datetime | None = None
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
            "decision_completed_at": (
                self.decision_completed_at.isoformat() if self.decision_completed_at else None
            ),
            "retried": self.retried,
            "failure_class": self.failure_class.value if self.failure_class else None,
            "notes": list(self.notes),
        }


@dataclass
class ProductionEntryAnalysis:
    """One provider, one connection, three transactions."""

    conn: object
    store: object
    provider: object
    canonical_text: str
    addenda_texts: tuple[str, ...] = ()
    #: Where the entry reference price comes from, asked only after a decision
    #: exists. The default refuses, because no intraday source is bound (D-06b /
    #: D-103-LIVE) and a runner that invented one would produce predictions
    #: scored against a price nothing observed.
    price_observer: object = field(default_factory=NoEntryPriceSource)
    #: The runner's own clock. Injectable for tests and for nothing else: a
    #: caller supplying decision_completed_at is the thing this replaced.
    clock: object = None
    version: str = RUNNER_VERSION

    def _now(self) -> datetime:
        return self.clock() if self.clock is not None else datetime.now(UTC)

    # ------------------------------------------------------------------ run

    def run_for_trigger(
        self,
        *,
        watch_id: str,
        trigger_transition_id: int,
        bundle: IntradayBundle | None = None,
        facts: EntryGuardFacts | None = None,
        thesis_key: str | None = None,
        now: datetime,
        setup_ids: tuple[str, ...] = (),
        run_id: str | None = None,
        analysis_kind: AnalysisKind = AnalysisKind.REANALYSIS,
        verification: VerificationStatus = VerificationStatus.IMPLEMENTED_NOT_LIVE_VERIFIED,
    ) -> RunResult:
        result = RunResult()

        # This entrance belongs to the watch machine, so the kind is fixed. An
        # ENTRY_DECISION recorded against a trigger would put an attempt saying
        # "entered directly" beside a watch transition saying "reanalysis" - one
        # event described two incompatible ways. A direct-entry path, when there
        # is one, gets its own entrance rather than borrowing this one.
        if analysis_kind is not AnalysisKind.REANALYSIS:
            raise EntryContractError(
                f"a watch trigger is answered by a REANALYSIS, not by {analysis_kind.value}; "
                "this runner is the watch path and a direct entry needs its own"
            )

        key = ExecutionKey(
            watch_id=watch_id,
            trigger_transition_id=trigger_transition_id,
            analysis_kind=analysis_kind,
        )

        # A resumed analysis brings its own inputs back from disk. Building new
        # ones would be answering a different question: twenty minutes on, an
        # intraday bundle is a different price and a different tape.
        existing = self.store.find(key) if hasattr(self.store, "find") else None
        if existing is not None and existing.stored_input is not None and bundle is None:
            try:
                rebuilt = reconstruct(
                    existing.stored_input,
                    canonical_text=self.canonical_text,
                    addenda_texts=self.addenda_texts,
                )
            except InputReconstructionError as exc:
                return self._fail_terminally(
                    existing,
                    result,
                    failure=FailureClass.INPUT_RECONSTRUCTION_MISMATCH,
                    detail=str(exc),
                    now=now,
                )
            bundle = rebuilt.bundle
            facts = rebuilt.facts
            thesis_key = rebuilt.thesis_key or thesis_key
            setup_ids = tuple(existing.stored_input.setup_ids) or setup_ids
            result.notes.append(
                "the inputs came from the stored snapshot, not from the current session: a bundle "
                "assembled now would be a different price and a different tape"
            )

        if bundle is None or facts is None:
            raise EntryContractError(
                "an analysis needs its bundle and its guard facts, either supplied or recovered "
                "from the stored input of an execution already under way"
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
        # The prompt is rendered here, before anything is sent, and the record
        # of what was asked is committed with its hash. The same prompt object
        # is the one that goes out: rendering a second one to send would usually
        # produce identical text, and "usually" is the failure.
        stored_input, prompt = build_stored_input(
            bundle=bundle,
            facts=facts,
            canonical_text=self.canonical_text,
            addenda_texts=self.addenda_texts,
            thesis_key=thesis_key,
            setup_ids=setup_ids,
        )
        begun = self._tx1(
            key, facts=facts, now=now, run_id=run_id, stored_input=stored_input
        )
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
            thesis_key=thesis_key or "",
            now=now,
            setup_ids=setup_ids,
            run_id=run_id,
            analysis_kind=analysis_kind,
            verification=verification,
        )

    # ----------------------------------------------------------------- TX1

    def _tx1(self, key: ExecutionKey, *, facts, now, run_id, stored_input):
        """Move the watch, record the analysis and its input. Commit, then call."""

        try:
            begun = self.store.begin(
                key,
                stored_input=stored_input,
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
            # What kind of failure this was comes from the exception itself, not
            # from where it was caught. Catching everything here and calling it
            # transient is what made a model that reached outside the bundle,
            # an exhausted quota and a missing credential all worth retrying.
            failure = classify_provider_failure(exc)
            detail = f"{type(exc).__name__}: {exc}"
            terminal = None if failure.is_transient else failure
            if terminal is None:
                terminal = execution.retry_budget_spent(now)
            if terminal is None:
                try:
                    self.store.retry(execution_id, transient_error=detail)
                    self.conn.commit()
                except Exception:
                    self.conn.rollback()
                    raise
                result.retried = True
                result.failure_class = failure
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
                # When the model replied, by this runner's clock. Separate from
                # decision_completed_at on purpose: this is the reply arriving,
                # not the system accepting it.
                answered_at=self._now(),
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
        setup_ids,
        run_id,
        analysis_kind,
        verification,
    ) -> RunResult:
        """Validate, fix the clock, price it, decide, write it all. Once."""

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
            # The decision is complete *here*: the model answered and the answer
            # met the contract. This is the runner's clock, not a caller's
            # declaration, because it is the line that decides which prices count
            # as having been available afterwards.
            decision_completed_at = execution.decision_completed_at or self._now()
            result.decision_completed_at = decision_completed_at

            entry_price, entry_price_method, price_evidence = self._observe_entry_price(
                execution,
                response,
                result,
                decision_completed_at=decision_completed_at,
            )

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
                decision_completed_at=decision_completed_at,
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
                    # The same value the attempt and the prediction carry. A
                    # LIVE_VERIFIED prediction inside an unverified episode would
                    # be scored as real evidence, and the episode is the unit
                    # scoring counts.
                    verification=verification,
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
                decision_completed_at=decision_completed_at,
                entry_price=entry_price,
                entry_price_method=entry_price_method,
                entry_price_evidence=price_evidence,
            )
            self.conn.commit()
            return result

        except Exception:
            # Everything in TX3 or nothing. A prediction whose attempt rolled
            # back would be a claim with no decision behind it.
            self.conn.rollback()
            raise

    # ------------------------------------------------------------- helpers

    def _observe_entry_price(self, execution, response, result, *, decision_completed_at):
        """The price the entry would have been made at, fetched after the fact.

        Only an ENTRY asks. A REJECT, a WATCH or a reaffirmation does not need a
        price, and observing one spends a provider call on a number nothing
        uses.

        Failure here is a finding, not a fault: the attempt is still written,
        with status NO_ENTRY_REFERENCE_PRICE. A decision that happened and could
        not be priced is a different fact from a decision that did not happen,
        and collapsing the two would quietly remove the awkward ones from the
        ledger.
        """

        if response.state is not EntryAnalysisState.ENTRY:
            return None, None, ()

        evidence: list[str] = []
        # A price observed during recovery is late, and says so. The alternative
        # is back-dating it to the decision, which would claim the system could
        # have traded at a price it was not running to see.
        if execution.decision_completed_at is not None and execution.decision_completed_at < (
            decision_completed_at
        ):
            evidence.append(ENTRY_PRICE_RECOVERY_DELAYED)
        if result.resumed:
            evidence.append(ENTRY_PRICE_RECOVERY_DELAYED)

        try:
            observation: EntryPriceObservation = self.price_observer.observe(
                security_id=execution.security_id,
                market_code=execution.key.watch_id and self._market_code(execution),
                not_before=decision_completed_at,
            )
            assert_observation_is_usable(
                observation, decision_completed_at=decision_completed_at
            )
        except EntryPriceUnavailable as exc:
            result.notes.append(
                f"no entry reference price could be observed after "
                f"{decision_completed_at.isoformat()}: {exc}. The decision is recorded and no "
                "prediction is made - a prediction needs a price it could have been made at"
            )
            return None, None, tuple(dict.fromkeys(evidence))

        evidence.extend(observation.evidence)
        return (
            observation.price,
            observation.method,
            tuple(dict.fromkeys(evidence)),
        )

    def _market_code(self, execution) -> str:
        """The market the bundle was assembled for, from the stored input."""

        stored = execution.stored_input
        if stored is None:
            return "JP"
        from surge.analysis.input_snapshot import rebuild_bundle

        return rebuild_bundle(stored).market_code

    def _fail_terminally(self, execution, result, *, failure, detail, now):
        """Move the watch out of IN_REANALYSIS, then fail. In that order.

        The database refuses to fail an execution whose watch is still
        IN_REANALYSIS, which is what makes this order impossible to forget.
        """

        try:
            entry_db.write_watch_transition(
                self.conn,
                watch_id=execution.key.watch_id,
                from_state=WatchState.IN_REANALYSIS,
                to_state=WatchState.REARMED,
                occurred_at=now,
                analysis_kind=AnalysisKind.REANALYSIS,
                note=f"{failure.value}: {detail}"[:200],
            )
            self.store.fail(
                execution.analysis_execution_id,
                failure_class=failure,
                failure_detail=detail,
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        result.analysis_execution_id = execution.analysis_execution_id
        result.security_id = execution.security_id
        result.failure_class = failure
        result.watch_state_after = WatchState.REARMED
        result.notes.append(detail)
        return result

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


def resume_all(runner: ProductionEntryAnalysis, *, now: datetime) -> Recovery:
    """Pick up every analysis that was started and never finished.

    It takes no bundles, no facts and no thesis keys any more, and that is the
    point. Taking them meant recovery depended on the process that built them
    still being alive - which is the one thing a crash rules out - and a caller
    that supplied fresh ones would have been resuming a *different* analysis
    under the original trigger's name.

    Reads the executions rather than the watches. By the time the model is
    called the watch has already moved to IN_REANALYSIS, so a sweep for
    TRIGGER_HIT walks past exactly the ones that crashed.
    """

    recovery = Recovery()
    for execution in runner.store.in_flight():
        if not execution.can_be_resumed:
            # Reported, not skipped. This is a watch sitting at IN_REANALYSIS
            # that nothing can move, because nothing recorded what it was asked.
            # Skipping it silently made "nothing was stuck" and "several things
            # were stuck and I walked past them" produce the same empty result.
            recovery.unresumable.append(
                {
                    "analysis_execution_id": execution.analysis_execution_id,
                    "watch_id": execution.key.watch_id,
                    "status": execution.status.value,
                    "started_at": execution.started_at.isoformat(),
                    "why": (
                        "no stored input, so what this analysis was asked is not recorded "
                        "anywhere; the watch is still IN_REANALYSIS and nothing can finish it"
                    ),
                }
            )
            continue
        recovery.resumed.append(
            runner.run_for_trigger(
                watch_id=execution.key.watch_id,
                trigger_transition_id=execution.key.trigger_transition_id,
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
