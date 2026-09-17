"""The execution store, backed by the database.

Everything here goes through a function rather than a statement. The worker has
no INSERT, UPDATE or DELETE on ``prod.entry_analysis_executions``, so the only
way in is ``prod.begin_entry_analysis`` and friends - which is what keeps the
watch transition and the execution record in step. A store that wrote the table
directly could record an analysis whose watch never moved, and that row would
look exactly like one that did.

The connection is the caller's. This does not commit: the whole point of
``begin`` is that the transition and the record land in one transaction, and
deciding where that transaction ends belongs to whoever owns the connection.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from surge.analysis.entry_analysis import EntryAnalysisResponse, EntryValidation
from surge.analysis.execution import (
    EXECUTION_VERSION,
    AnalysisExecution,
    BeginResult,
    ExecutionError,
    ExecutionKey,
    ExecutionStatus,
    FailureClass,
)
from surge.entry.models import AnalysisKind


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
        security_id: str,
        decision_cutoff_at: datetime,
        provider_id: str,
        provider_kind: str,
        model_id: str | None = None,
        run_id: str | None = None,
        now: datetime,
        request_version: str = EXECUTION_VERSION,
    ) -> BeginResult:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                select analysis_execution_id, status, created
                  from prod.begin_entry_analysis(
                    %(watch_id)s, %(trigger_transition_id)s,
                    %(analysis_kind)s::prod.analysis_kind, %(security_id)s,
                    %(decision_cutoff_at)s, %(provider_id)s,
                    %(provider_kind)s::analysis.provider_kind, %(request_version)s,
                    %(model_id)s, %(run_id)s, %(occurred_at)s
                  )
                """,
                {
                    "watch_id": key.watch_id,
                    "trigger_transition_id": key.trigger_transition_id,
                    "analysis_kind": key.analysis_kind.value,
                    "security_id": security_id,
                    "decision_cutoff_at": decision_cutoff_at,
                    "provider_id": provider_id,
                    "provider_kind": provider_kind,
                    "request_version": request_version,
                    "model_id": model_id,
                    "run_id": run_id,
                    "occurred_at": now,
                },
            )
            execution_id, status, created = cur.fetchone()

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
        raw_response_ref: str | None = None,
    ) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                select prod.record_entry_analysis_answer(
                  %(id)s, %(state)s::prod.decision_state, %(rationale)s, %(response_sha256)s,
                  %(raw_ref)s, null, null, null,
                  %(decision_price_used)s, %(initial_failure_line)s,
                  %(zone_low)s, %(zone_high)s, %(trigger_description)s
                )
                """,
                {
                    "id": execution_id,
                    "state": response.state.value,
                    "rationale": response.rationale,
                    "response_sha256": response.response_sha256,
                    "raw_ref": raw_response_ref,
                    "decision_price_used": response.decision_price_used,
                    "initial_failure_line": response.proposed_initial_failure_line,
                    "zone_low": response.reachable_zone_low,
                    "zone_high": response.reachable_zone_high,
                    "trigger_description": response.watch_trigger_description,
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
        now: datetime,
    ) -> None:
        execution = self._load(execution_id)
        if execution is None:
            raise ExecutionError(f"no analysis execution {execution_id}")

        answer = execution.stored_answer
        with self._conn.cursor() as cur:
            cur.execute(
                """
                select prod.complete_entry_analysis(
                  %(id)s, %(state)s::prod.decision_state, %(rationale)s,
                  %(validation_status)s::analysis.validation_status,
                  null, null, null, null, null,
                  %(decision_price_used)s, %(initial_failure_line)s,
                  %(zone_low)s, %(zone_high)s, %(trigger_description)s,
                  %(errors)s, %(refusals)s, %(warnings)s,
                  %(attempt_id)s, %(prediction_id)s, null
                )
                """,
                {
                    "id": execution_id,
                    "state": answer.state.value if answer else None,
                    "rationale": answer.rationale if answer else None,
                    "validation_status": validation.status.value,
                    "decision_price_used": answer.decision_price_used if answer else None,
                    "initial_failure_line": (
                        answer.proposed_initial_failure_line if answer else None
                    ),
                    "zone_low": answer.reachable_zone_low if answer else None,
                    "zone_high": answer.reachable_zone_high if answer else None,
                    "trigger_description": answer.watch_trigger_description if answer else None,
                    "errors": list(validation.errors),
                    "refusals": list(validation.system_refusals),
                    "warnings": list(validation.warnings),
                    "attempt_id": entry_attempt_id,
                    "prediction_id": prediction_id,
                },
            )

    def fail(
        self,
        execution_id: str,
        *,
        failure_class: FailureClass,
        failure_detail: str,
        now: datetime,
        validation: EntryValidation | None = None,
    ) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                select prod.fail_entry_analysis(
                  %(id)s, %(failure_class)s, %(failure_detail)s,
                  %(validation_status)s::analysis.validation_status, %(errors)s, null, null
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
                "where status = 'STARTED' order by started_at"
            )
            ids = [str(row[0]) for row in cur.fetchall()]
        return [e for e in (self._load(i) for i in ids) if e is not None]

    # --------------------------------------------------------------- read

    def _load(self, execution_id: str) -> AnalysisExecution | None:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                select analysis_execution_id, watch_id, trigger_transition_id, analysis_kind,
                       security_id, status, decision_cutoff_at, provider_id, provider_kind,
                       model_id, run_id, started_at, response_sha256, raw_response_ref,
                       returned_state, rationale, decision_price_used, initial_failure_line,
                       reachable_zone_low, reachable_zone_high, watch_trigger_description,
                       entry_attempt_id, prediction_id, completed_at, failure_class,
                       failure_detail, request_version
                  from prod.entry_analysis_executions
                 where analysis_execution_id = %s
                """,
                (execution_id,),
            )
            row = cur.fetchone()
        if row is None:
            return None

        stored_answer = None
        if row[12] is not None:
            # Rebuilt from the columns rather than from a serialised blob: the
            # columns are what the database can be queried on, and a blob that
            # disagreed with them would be invisible.
            from surge.analysis.entry_analysis import EntryAnalysisState
            from surge.analysis.llm import ProviderKind

            stored_answer = EntryAnalysisResponse(
                state=EntryAnalysisState(row[14]),
                rationale=row[15] or "",
                provider_id=row[7],
                provider_kind=ProviderKind(row[8]),
                model_id=row[9],
                decision_price_used=_decimal(row[16]),
                proposed_initial_failure_line=_decimal(row[17]),
                reachable_zone_low=_decimal(row[18]),
                reachable_zone_high=_decimal(row[19]),
                watch_trigger_description=row[20],
            )

        return AnalysisExecution(
            analysis_execution_id=str(row[0]),
            key=ExecutionKey(
                watch_id=str(row[1]),
                trigger_transition_id=int(row[2]),
                analysis_kind=AnalysisKind(row[3]),
            ),
            security_id=str(row[4]),
            status=ExecutionStatus(row[5]),
            decision_cutoff_at=row[6],
            provider_id=row[7],
            provider_kind=row[8],
            started_at=row[11],
            model_id=row[9],
            run_id=str(row[10]) if row[10] else None,
            request_version=row[26],
            stored_answer=stored_answer,
            response_sha256=row[12],
            raw_response_ref=row[13],
            entry_attempt_id=str(row[21]) if row[21] else None,
            prediction_id=str(row[22]) if row[22] else None,
            completed_at=row[23],
            failure_class=FailureClass(row[24]) if row[24] else None,
            failure_detail=row[25],
        )


__all__ = ["DatabaseExecutionStore"]
