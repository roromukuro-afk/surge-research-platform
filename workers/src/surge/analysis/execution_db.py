"""The execution store, backed by the database.

Everything here goes through a function rather than a statement. The worker has
no INSERT, UPDATE or DELETE on ``prod.entry_analysis_executions``, so the only
way in is ``prod.begin_entry_analysis`` and its siblings - which is what keeps
the watch transition and the execution record in step. A store that wrote the
table directly could record an analysis whose watch never moved, and that row
would look exactly like one that did.

**This does not commit.** Committing is the caller's, and it matters where: the
durability the record exists for comes from *when* the transaction ends, not
from the row existing in some uncommitted snapshot. See
:mod:`surge.jobs.production_entry_analysis`, which commits between the three
phases and is the only thing that should be calling this in production.

**The answer is stored losslessly.** Every semantic field of the response has a
column, including ``reachable_zone_basis_kinds`` and ``reachable_zone_basis`` -
which the entry validator *requires* for an ENTRY. Dropping them would mean a
resumed analysis could no longer pass the contract the original answer passed,
and the failure would look like the model having changed its mind.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from surge.analysis.entry_analysis import (
    EntryAnalysisResponse,
    EntryAnalysisState,
    EntryValidation,
)
from surge.analysis.execution import (
    EXECUTION_VERSION,
    INPUT_VERSION,
    AnalysisExecution,
    BeginResult,
    ExecutionError,
    ExecutionKey,
    ExecutionStatus,
    FailureClass,
    StoredInput,
)
from surge.analysis.llm import ProviderKind, ZoneBasisKind
from surge.entry.models import AnalysisKind, ObservedPrice

#: Every column the loader reads, in one place so the tuple indices below cannot
#: drift apart from the query.
_COLUMNS = (
    "analysis_execution_id", "watch_id", "trigger_transition_id", "analysis_kind",
    "security_id", "status", "decision_cutoff_at", "provider_id", "provider_kind",
    "model_id", "run_id", "started_at", "response_sha256", "raw_response_ref",
    "returned_state", "rationale", "decision_price_used", "initial_failure_line",
    "reachable_zone_low", "reachable_zone_high", "reachable_zone_basis_kinds",
    "reachable_zone_basis", "concepts_considered", "watch_trigger_description",
    "reject_reason", "raw_response", "entry_attempt_id", "prediction_id", "completed_at",
    "failure_class", "failure_detail", "request_version", "prompt_sha256",
    "bundle_sha256", "canonical_prompt_sha256", "attempt_count", "last_transient_error",
    "bundle_serialized", "addenda_sha256", "thesis_key", "setup_ids", "decision_price",
    "decision_price_currency", "decision_price_observed_at", "fx_rate", "fx_observed_at",
    "price_limit_jpy", "universe_decision", "universe_reason_code",
    "coverage_meets_requirements", "coverage_detail", "input_version",
    "analysis_answered_at", "decision_completed_at", "entry_price_observed_at",
)


def _decimal(value) -> Decimal | None:
    return None if value is None else Decimal(str(value))


class DatabaseExecutionStore:
    """``prod.entry_analysis_executions``, through its functions."""

    def __init__(self, conn) -> None:
        self._conn = conn

    # ------------------------------------------------------------- begin

    def begin(
        self,
        key: ExecutionKey,
        *,
        stored_input: StoredInput,
        security_id: str | None = None,
        decision_cutoff_at: datetime,
        provider_id: str,
        provider_kind: str,
        model_id: str | None = None,
        run_id: str | None = None,
        now: datetime,
        request_version: str = EXECUTION_VERSION,
    ) -> BeginResult:
        """Move the watch, record the analysis *and its input*, in one statement.

        ``security_id`` is optional and is only ever a cross-check: the database
        derives it from the watch. Passing a different one is an error rather
        than an override, because the caller's copy is the one that can be stale.

        ``stored_input`` is not optional. It is what makes the row resumable: a
        crash between this call and the answer leaves behind everything needed
        to finish the analysis, rather than a note that one had begun.
        """

        with self._conn.cursor() as cur:
            cur.execute(
                """
                select analysis_execution_id, status, created, security_id
                  from prod.begin_entry_analysis(
                    %(watch_id)s, %(trigger_transition_id)s,
                    %(analysis_kind)s::prod.analysis_kind,
                    %(decision_cutoff_at)s, %(provider_id)s,
                    %(provider_kind)s::analysis.provider_kind, %(request_version)s,
                    %(prompt_sha256)s, %(bundle_sha256)s, %(canonical_prompt_sha256)s,
                    %(bundle_serialized)s, %(addenda_sha256)s,
                    %(thesis_key)s, %(setup_ids)s,
                    %(decision_price)s, %(decision_price_currency)s,
                    %(decision_price_observed_at)s, %(fx_rate)s, %(fx_observed_at)s,
                    %(price_limit_jpy)s,
                    %(universe_decision)s, %(universe_reason_code)s,
                    %(coverage_meets_requirements)s, %(coverage_detail)s, %(input_version)s,
                    %(security_id)s, %(model_id)s, %(run_id)s, %(occurred_at)s
                  )
                """,
                {
                    "watch_id": key.watch_id,
                    "trigger_transition_id": key.trigger_transition_id,
                    "analysis_kind": key.analysis_kind.value,
                    "decision_cutoff_at": decision_cutoff_at,
                    "provider_id": provider_id,
                    "provider_kind": provider_kind,
                    "request_version": request_version,
                    "prompt_sha256": stored_input.prompt_sha256,
                    "bundle_sha256": stored_input.bundle_sha256,
                    "canonical_prompt_sha256": stored_input.canonical_prompt_sha256,
                    "bundle_serialized": stored_input.bundle_serialized,
                    "addenda_sha256": list(stored_input.addenda_sha256),
                    "thesis_key": stored_input.thesis_key,
                    "setup_ids": list(stored_input.setup_ids),
                    "decision_price": stored_input.decision_price,
                    "decision_price_currency": stored_input.decision_price_currency,
                    "decision_price_observed_at": stored_input.decision_price_observed_at,
                    "fx_rate": stored_input.fx_rate,
                    "fx_observed_at": stored_input.fx_observed_at,
                    "price_limit_jpy": stored_input.price_limit_jpy,
                    "universe_decision": stored_input.universe_decision,
                    "universe_reason_code": stored_input.universe_reason_code,
                    "coverage_meets_requirements": stored_input.coverage_meets_requirements,
                    "coverage_detail": stored_input.coverage_detail,
                    "input_version": stored_input.input_version,
                    "security_id": security_id,
                    "model_id": model_id,
                    "run_id": run_id,
                    "occurred_at": now,
                },
            )
            execution_id, _status, created, _security = cur.fetchone()

        execution = self._load(str(execution_id))
        if execution is None:  # pragma: no cover - the function just created it
            raise ExecutionError(f"analysis execution {execution_id} vanished after begin")
        return BeginResult(execution=execution, created=bool(created))

    # ---------------------------------------------------------- the answer

    def record_answer(
        self,
        execution_id: str,
        response: EntryAnalysisResponse,
        *,
        prompt_sha256: str,
        bundle_sha256: str,
        canonical_prompt_sha256: str,
        answered_at: datetime,
        raw_response_ref: str | None = None,
    ) -> None:
        """Store the answer, when it arrived, and the hashes it belongs to.

        The hashes are passed and *checked* rather than written: they were fixed
        when the request was recorded, so a runner that rendered a different
        prompt in between would otherwise overwrite the record of what it
        started with. The database raises on a mismatch.

        ``answered_at`` is the runner's clock, and it is separate from
        ``decision_completed_at`` on purpose: this is when the model replied, not
        when the system accepted the reply.
        """

        with self._conn.cursor() as cur:
            cur.execute(
                """
                select prod.record_entry_analysis_answer(
                  %(id)s, %(state)s::prod.decision_state, %(rationale)s,
                  %(response_sha256)s, %(prompt_sha256)s, %(bundle_sha256)s,
                  %(canonical_sha256)s, %(answered_at)s, %(raw_response)s, %(raw_ref)s,
                  %(decision_price_used)s, %(initial_failure_line)s,
                  %(zone_low)s, %(zone_high)s, %(zone_basis_kinds)s, %(zone_basis)s,
                  %(concepts)s, %(trigger_description)s, %(reject_reason)s
                )
                """,
                {
                    "id": execution_id,
                    "state": response.state.value,
                    "rationale": response.rationale,
                    "response_sha256": response.response_sha256,
                    "prompt_sha256": prompt_sha256,
                    "bundle_sha256": bundle_sha256,
                    "canonical_sha256": canonical_prompt_sha256,
                    "answered_at": answered_at,
                    "raw_response": response.raw_text or None,
                    "raw_ref": raw_response_ref,
                    "decision_price_used": response.decision_price_used,
                    "initial_failure_line": response.proposed_initial_failure_line,
                    "zone_low": response.reachable_zone_low,
                    "zone_high": response.reachable_zone_high,
                    "zone_basis_kinds": [k.value for k in response.reachable_zone_basis_kinds],
                    "zone_basis": response.reachable_zone_basis,
                    "concepts": list(response.concepts_considered),
                    "trigger_description": response.watch_trigger_description,
                    "reject_reason": response.reject_reason,
                },
            )

    # ------------------------------------------------------------- finish

    def complete(
        self,
        execution_id: str,
        *,
        validation: EntryValidation,
        entry_attempt_id: str | None = None,
        prediction_id: str | None = None,
        now: datetime | None = None,
        decision_completed_at: datetime | None = None,
        entry_price: ObservedPrice | None = None,
        entry_price_method: str | None = None,
        entry_price_evidence: tuple[str, ...] = (),
    ) -> None:
        """Close the analysis with what it actually produced.

        The ids come from the caller because the caller is what inserted the
        rows and read their returned keys. The database checks that the watch
        has already been moved: completing while the watch is still
        IN_REANALYSIS would leave a finished analysis pointing at a watch nobody
        will look at again.
        """

        with self._conn.cursor() as cur:
            cur.execute(
                """
                select prod.complete_entry_analysis(
                  %(id)s, %(validation_status)s::analysis.validation_status,
                  %(errors)s, %(refusals)s, %(warnings)s, %(attempt_id)s, %(prediction_id)s,
                  %(decision_completed_at)s, %(entry_price)s, %(entry_currency)s,
                  %(entry_observed_at)s, %(entry_method)s, %(entry_evidence)s
                )
                """,
                {
                    "id": execution_id,
                    "validation_status": validation.status.value,
                    "errors": list(validation.errors),
                    "refusals": list(validation.system_refusals),
                    "warnings": list(validation.warnings),
                    "attempt_id": entry_attempt_id,
                    "prediction_id": prediction_id,
                    "decision_completed_at": decision_completed_at,
                    "entry_price": entry_price.amount if entry_price else None,
                    "entry_currency": entry_price.currency if entry_price else None,
                    "entry_observed_at": entry_price.observed_at if entry_price else None,
                    "entry_method": entry_price_method,
                    "entry_evidence": list(entry_price_evidence),
                },
            )

    def retry(
        self,
        execution_id: str,
        *,
        transient_error: str,
        retry_not_before: datetime | None = None,
    ) -> None:
        """A transient failure. The execution stays open and so does the watch."""

        with self._conn.cursor() as cur:
            cur.execute(
                "select prod.retry_entry_analysis(%s, %s, %s)",
                (execution_id, transient_error, retry_not_before),
            )

    def fail(
        self,
        execution_id: str,
        *,
        failure_class: FailureClass,
        failure_detail: str,
        now: datetime | None = None,
        validation: EntryValidation | None = None,
    ) -> None:
        """A terminal failure. The watch must already have been moved.

        The database refuses to fail an analysis whose watch is still
        IN_REANALYSIS, because nothing else moves a watch out of that state: the
        security would be stuck for good, and a network blip would have removed
        it from the system.
        """

        if failure_class.is_transient:
            raise ExecutionError(
                f"{failure_class.value} is a transient failure; use retry() so the execution stays "
                "open. Failing it would strand the watch at IN_REANALYSIS forever"
            )

        with self._conn.cursor() as cur:
            cur.execute(
                """
                select prod.fail_entry_analysis(
                  %(id)s, %(failure_class)s, %(failure_detail)s,
                  %(validation_status)s::analysis.validation_status, %(errors)s
                )
                """,
                {
                    "id": execution_id,
                    "failure_class": failure_class.value,
                    "failure_detail": failure_detail,
                    "validation_status": validation.status.value if validation else None,
                    "errors": list(validation.errors) if validation else [],
                },
            )

    # ----------------------------------------------------------- recovery

    def in_flight(self) -> list[AnalysisExecution]:
        with self._conn.cursor() as cur:
            cur.execute(
                "select analysis_execution_id from prod.entry_analysis_executions "
                "where status in ('STARTED', 'RETRY_PENDING') order by started_at"
            )
            ids = [str(row[0]) for row in cur.fetchall()]
        return [e for e in (self._load(i) for i in ids) if e is not None]

    def find(self, key: ExecutionKey) -> AnalysisExecution | None:
        """The execution for this trigger, if one was ever started.

        A plain read, with no side effects: ``begin`` is what creates and locks.
        This exists so a runner can discover that it is resuming *before* it
        builds a bundle, and use the stored one instead.
        """

        with self._conn.cursor() as cur:
            cur.execute(
                "select analysis_execution_id from prod.entry_analysis_executions "
                "where watch_id = %s and trigger_transition_id = %s "
                "and analysis_kind = %s::prod.analysis_kind",
                (key.watch_id, key.trigger_transition_id, key.analysis_kind.value),
            )
            row = cur.fetchone()
        return self._load(str(row[0])) if row else None

    # --------------------------------------------------------------- read

    def _load(self, execution_id: str) -> AnalysisExecution | None:
        with self._conn.cursor() as cur:
            cur.execute(
                f"select {', '.join(_COLUMNS)} from prod.entry_analysis_executions "  # noqa: S608
                "where analysis_execution_id = %s",
                (execution_id,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        r = dict(zip(_COLUMNS, row, strict=True))

        stored_answer = None
        if r["response_sha256"] is not None:
            # Rebuilt from the columns rather than from a blob, because the
            # columns are what the database can be queried on. Every semantic
            # field is here - in particular the zone basis, without which a
            # resumed ENTRY could not pass the validator the original passed.
            stored_answer = EntryAnalysisResponse(
                state=EntryAnalysisState(r["returned_state"]),
                rationale=r["rationale"] or "",
                provider_id=r["provider_id"],
                provider_kind=ProviderKind(r["provider_kind"]),
                model_id=r["model_id"],
                decision_price_used=_decimal(r["decision_price_used"]),
                proposed_initial_failure_line=_decimal(r["initial_failure_line"]),
                reachable_zone_low=_decimal(r["reachable_zone_low"]),
                reachable_zone_high=_decimal(r["reachable_zone_high"]),
                reachable_zone_basis_kinds=tuple(
                    ZoneBasisKind(k) for k in (r["reachable_zone_basis_kinds"] or ())
                ),
                reachable_zone_basis=r["reachable_zone_basis"],
                concepts_considered=tuple(r["concepts_considered"] or ()),
                watch_trigger_description=r["watch_trigger_description"],
                reject_reason=r["reject_reason"],
                raw_text=r["raw_response"] or "",
            )

        stored_input = None
        if r["bundle_serialized"]:
            # Rebuilt in full, including the hashes it is proved against. A
            # partial one would let a resumed analysis look resumable and then
            # fail the reconstruction check for a reason that has nothing to do
            # with the input having changed.
            stored_input = StoredInput(
                bundle_serialized=r["bundle_serialized"],
                prompt_sha256=r["prompt_sha256"],
                bundle_sha256=r["bundle_sha256"],
                canonical_prompt_sha256=r["canonical_prompt_sha256"],
                addenda_sha256=tuple(r["addenda_sha256"] or ()),
                thesis_key=r["thesis_key"],
                setup_ids=tuple(r["setup_ids"] or ()),
                decision_price=_decimal(r["decision_price"]),
                decision_price_currency=r["decision_price_currency"],
                decision_price_observed_at=r["decision_price_observed_at"],
                fx_rate=_decimal(r["fx_rate"]),
                fx_observed_at=r["fx_observed_at"],
                price_limit_jpy=_decimal(r["price_limit_jpy"]),
                universe_decision=r["universe_decision"],
                universe_reason_code=r["universe_reason_code"],
                coverage_meets_requirements=r["coverage_meets_requirements"],
                coverage_detail=r["coverage_detail"],
                input_version=r["input_version"] or INPUT_VERSION,
            )

        return AnalysisExecution(
            analysis_execution_id=str(r["analysis_execution_id"]),
            key=ExecutionKey(
                watch_id=str(r["watch_id"]),
                trigger_transition_id=int(r["trigger_transition_id"]),
                analysis_kind=AnalysisKind(r["analysis_kind"]),
            ),
            security_id=str(r["security_id"]),
            status=ExecutionStatus(r["status"]),
            decision_cutoff_at=r["decision_cutoff_at"],
            provider_id=r["provider_id"],
            provider_kind=r["provider_kind"],
            started_at=r["started_at"],
            model_id=r["model_id"],
            prompt_sha256=r["prompt_sha256"],
            bundle_sha256=r["bundle_sha256"],
            canonical_prompt_sha256=r["canonical_prompt_sha256"],
            run_id=str(r["run_id"]) if r["run_id"] else None,
            request_version=r["request_version"],
            stored_answer=stored_answer,
            response_sha256=r["response_sha256"],
            raw_response_ref=r["raw_response_ref"],
            entry_attempt_id=str(r["entry_attempt_id"]) if r["entry_attempt_id"] else None,
            prediction_id=str(r["prediction_id"]) if r["prediction_id"] else None,
            completed_at=r["completed_at"],
            failure_class=FailureClass(r["failure_class"]) if r["failure_class"] else None,
            failure_detail=r["failure_detail"],
            stored_input=stored_input,
            analysis_answered_at=r["analysis_answered_at"],
            decision_completed_at=r["decision_completed_at"],
            entry_price_observed_at=r["entry_price_observed_at"],
            attempt_count=int(r["attempt_count"] or 0),
            last_transient_error=r["last_transient_error"],
        )


__all__ = ["DatabaseExecutionStore"]
