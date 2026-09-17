"""The record of one intraday analysis, kept while it happens rather than after.

The version this replaces moved a Python object and claimed durability it did
not have. A process that died between the trigger and the answer left a watch at
``TRIGGER_HIT``, which reads as "nothing acted on this trigger" - the exact
misreading the ordering existed to prevent.

So an execution is a row, written in the same transaction as the watch's move to
``IN_REANALYSIS`` and before the model is called. Three crashes are survivable
because of it, and they need three different responses:

*crashed before the model answered*
    The row says STARTED with no stored answer. Recovery calls the model.
*crashed after the answer was stored*
    The row says STARTED with an answer. Recovery resumes from **that** answer
    rather than asking again - not only to avoid paying twice, but because a
    second call could return something different, and then which one was "the"
    analysis for this trigger would have no answer.
*crashed after the decision committed*
    The row says COMPLETED. Recovery stops. A second prediction from one trigger
    would make repeating yourself look like being right twice.

The key is (watch, trigger transition, kind). Keyed on the watch alone, a watch
that legitimately re-armed and triggered again would have its second analysis
refused as a duplicate of its first.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from surge.analysis.entry_analysis import EntryAnalysisResponse, EntryValidation
from surge.entry.models import AnalysisKind, EntryError

EXECUTION_VERSION = "entry-analysis-execution-1.0.0"


class ExecutionStatus(StrEnum):
    """Mirrors ``prod.analysis_execution_status``."""

    STARTED = "STARTED"
    #: A transient failure. Still open, still the same execution.
    RETRY_PENDING = "RETRY_PENDING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"

    @property
    def is_open(self) -> bool:
        return self in (ExecutionStatus.STARTED, ExecutionStatus.RETRY_PENDING)


class FailureClass(StrEnum):
    """Why an analysis produced no decision, and whether it can be tried again.

    The distinction is not bookkeeping. A failed execution is never looked at by
    a recovery pass, and nothing else moves a watch out of ``IN_REANALYSIS`` - so
    marking a network timeout as FAILED strands that security permanently. A
    blip would quietly remove a stock from the system.
    """

    #: Transient. The same execution is retried; the watch stays IN_REANALYSIS
    #: because it genuinely still is under analysis.
    PROVIDER_ERROR = "PROVIDER_ERROR"
    PROVIDER_TIMEOUT = "PROVIDER_TIMEOUT"
    PROVIDER_RATE_LIMITED = "PROVIDER_RATE_LIMITED"

    #: Terminal. Trying again would produce the same answer, so the watch has to
    #: be moved somewhere it can rest.
    CONTRACT_VIOLATION = "CONTRACT_VIOLATION"
    QUOTA_BLOCKED = "QUOTA_BLOCKED"
    PRIVACY_POLICY_BLOCKED = "PRIVACY_POLICY_BLOCKED"
    COVERAGE_NOT_MET = "COVERAGE_NOT_MET"
    STAND_IN_PROVIDER = "STAND_IN_PROVIDER"
    EXTERNAL_TOOL_USED = "EXTERNAL_TOOL_USED"

    @property
    def is_transient(self) -> bool:
        return self in TRANSIENT_FAILURES


#: Retried rather than failed. Deliberately a short, explicit list: anything not
#: named here is terminal, because assuming an unrecognised failure is worth
#: retrying is how a permanent problem becomes an infinite loop.
TRANSIENT_FAILURES = frozenset(
    {
        FailureClass.PROVIDER_ERROR,
        FailureClass.PROVIDER_TIMEOUT,
        FailureClass.PROVIDER_RATE_LIMITED,
    }
)


class ExecutionError(EntryError):
    """The execution record could not be kept, so the analysis must not proceed."""


@dataclass(frozen=True)
class ExecutionKey:
    """What makes one analysis distinct from another."""

    watch_id: str
    trigger_transition_id: int
    analysis_kind: AnalysisKind

    def __post_init__(self) -> None:
        if self.analysis_kind not in (AnalysisKind.ENTRY_DECISION, AnalysisKind.REANALYSIS):
            raise ExecutionError(
                f"{self.analysis_kind.value} is not an intraday analysis; only ENTRY_DECISION and "
                "REANALYSIS run against a live price"
            )


@dataclass
class AnalysisExecution:
    """One row of ``prod.entry_analysis_executions``, as the job sees it."""

    analysis_execution_id: str
    key: ExecutionKey
    security_id: str
    status: ExecutionStatus
    decision_cutoff_at: datetime
    provider_id: str
    provider_kind: str
    started_at: datetime
    model_id: str | None = None
    prompt_sha256: str | None = None
    bundle_sha256: str | None = None
    canonical_prompt_sha256: str | None = None
    run_id: str | None = None
    request_version: str = EXECUTION_VERSION
    #: The answer, once there is one. Its presence is what tells a recovery pass
    #: whether the model has already been paid for.
    stored_answer: EntryAnalysisResponse | None = None
    response_sha256: str | None = None
    raw_response_ref: str | None = None
    validation: EntryValidation | None = None
    entry_attempt_id: str | None = None
    prediction_id: str | None = None
    completed_at: datetime | None = None
    failure_class: FailureClass | None = None
    failure_detail: str | None = None
    attempt_count: int = 0
    last_transient_error: str | None = None

    @property
    def is_finished(self) -> bool:
        return not self.status.is_open

    @property
    def has_a_stored_answer(self) -> bool:
        return self.stored_answer is not None

    @property
    def needs_the_model(self) -> bool:
        """Whether recovery has to call the provider, or can resume from disk."""

        return self.status.is_open and not self.has_a_stored_answer

    @property
    def summary(self) -> dict:
        return {
            "analysis_execution_id": self.analysis_execution_id,
            "watch_id": self.key.watch_id,
            "trigger_transition_id": self.key.trigger_transition_id,
            "analysis_kind": self.key.analysis_kind.value,
            "status": self.status.value,
            "has_a_stored_answer": self.has_a_stored_answer,
            "entry_attempt_id": self.entry_attempt_id,
            "prediction_id": self.prediction_id,
            "failure_class": self.failure_class.value if self.failure_class else None,
        }


@dataclass(frozen=True)
class BeginResult:
    """What ``begin`` found or created.

    ``created`` is the difference between "this is a new analysis" and "somebody
    already started this one", and the caller behaves differently in each case -
    most importantly, it only moves the watch when it created the row.
    """

    execution: AnalysisExecution
    created: bool

    @property
    def already_decided(self) -> bool:
        return self.execution.is_finished


class ExecutionStore(Protocol):
    """Where executions are kept. The database in production, memory in tests."""

    def begin(
        self,
        key: ExecutionKey,
        *,
        security_id: str,
        decision_cutoff_at: datetime,
        provider_id: str,
        provider_kind: str,
        model_id: str | None,
        run_id: str | None,
        now: datetime,
    ) -> BeginResult: ...

    def record_answer(
        self,
        execution_id: str,
        response: EntryAnalysisResponse,
        *,
        prompt_sha256: str,
        bundle_sha256: str,
        canonical_prompt_sha256: str,
        raw_response_ref: str | None = None,
    ) -> None: ...

    def retry(
        self, execution_id: str, *, transient_error: str, retry_not_before: datetime | None = None
    ) -> None: ...

    def complete(
        self,
        execution_id: str,
        *,
        validation: EntryValidation,
        entry_attempt_id: str | None,
        prediction_id: str | None,
        now: datetime,
    ) -> None: ...

    def fail(
        self,
        execution_id: str,
        *,
        failure_class: FailureClass,
        failure_detail: str,
        now: datetime,
        validation: EntryValidation | None = None,
    ) -> None: ...

    def in_flight(self) -> list[AnalysisExecution]: ...


class InMemoryExecutionStore:
    """A store that behaves like the database, for tests and for dry runs.

    Enforces the same two rules the database enforces, because a test against a
    laxer store proves nothing about production: an execution has one outcome,
    and its identity never changes.
    """

    def __init__(self) -> None:
        self._rows: dict[str, AnalysisExecution] = {}
        self._by_key: dict[ExecutionKey, str] = {}
        self._next = 0

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
    ) -> BeginResult:
        existing_id = self._by_key.get(key)
        if existing_id is not None:
            return BeginResult(execution=self._rows[existing_id], created=False)

        self._next += 1
        execution_id = f"exec-{self._next}"
        execution = AnalysisExecution(
            analysis_execution_id=execution_id,
            key=key,
            security_id=security_id,
            status=ExecutionStatus.STARTED,
            decision_cutoff_at=decision_cutoff_at,
            provider_id=provider_id,
            provider_kind=provider_kind,
            started_at=now,
            model_id=model_id,
            run_id=run_id,
        )
        self._rows[execution_id] = execution
        self._by_key[key] = execution_id
        return BeginResult(execution=execution, created=True)

    def _started(self, execution_id: str) -> AnalysisExecution:
        execution = self._rows.get(execution_id)
        if execution is None:
            raise ExecutionError(f"no analysis execution {execution_id}")
        if execution.is_finished:
            raise ExecutionError(
                f"analysis execution {execution_id} is already {execution.status.value} and may "
                "not be changed again; an analysis has one outcome"
            )
        return execution

    def record_answer(
        self,
        execution_id: str,
        response: EntryAnalysisResponse,
        *,
        prompt_sha256: str,
        bundle_sha256: str,
        canonical_prompt_sha256: str,
        raw_response_ref: str | None = None,
    ) -> None:
        if not (prompt_sha256 and bundle_sha256 and canonical_prompt_sha256):
            raise ExecutionError(
                "an answer is stored with the hashes of what produced it; without them the "
                "stored answer belongs to no particular request (CLAUDE.md 1-18)"
            )
        execution = self._started(execution_id)
        execution.stored_answer = response
        execution.response_sha256 = response.response_sha256
        execution.prompt_sha256 = prompt_sha256
        execution.bundle_sha256 = bundle_sha256
        execution.canonical_prompt_sha256 = canonical_prompt_sha256
        execution.raw_response_ref = raw_response_ref
        execution.status = ExecutionStatus.STARTED
        execution.last_transient_error = None

    def retry(
        self, execution_id: str, *, transient_error: str, retry_not_before: datetime | None = None
    ) -> None:
        """A transient failure. The execution stays open and so does the watch.

        Failing it instead would strand the watch at IN_REANALYSIS forever: no
        recovery pass looks at a finished execution, and nothing else moves a
        watch out of that state.
        """

        execution = self._started(execution_id)
        execution.status = ExecutionStatus.RETRY_PENDING
        execution.attempt_count += 1
        execution.last_transient_error = transient_error

    def complete(
        self,
        execution_id: str,
        *,
        validation: EntryValidation,
        entry_attempt_id: str | None = None,
        prediction_id: str | None = None,
        now: datetime,
    ) -> None:
        execution = self._started(execution_id)
        if prediction_id is not None and entry_attempt_id is None:
            raise ExecutionError(
                "a prediction cannot be recorded without the attempt that produced it"
            )
        execution.status = ExecutionStatus.COMPLETED
        execution.validation = validation
        execution.entry_attempt_id = entry_attempt_id
        execution.prediction_id = prediction_id
        execution.completed_at = now

    def fail(
        self,
        execution_id: str,
        *,
        failure_class: FailureClass,
        failure_detail: str,
        now: datetime,
        validation: EntryValidation | None = None,
    ) -> None:
        execution = self._started(execution_id)
        execution.status = ExecutionStatus.FAILED
        execution.failure_class = failure_class
        execution.failure_detail = failure_detail
        execution.validation = validation
        execution.completed_at = now

    def in_flight(self) -> list[AnalysisExecution]:
        return [replace(row) for row in self._rows.values() if row.status.is_open]

    def get(self, execution_id: str) -> AnalysisExecution | None:
        return self._rows.get(execution_id)


@dataclass
class RecoveryPlan:
    """What to do with the analyses that were started and never finished."""

    call_the_model: list[AnalysisExecution] = field(default_factory=list)
    resume_from_stored_answer: list[AnalysisExecution] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.call_the_model) + len(self.resume_from_stored_answer)

    @property
    def summary(self) -> dict:
        return {
            "call_the_model": [e.analysis_execution_id for e in self.call_the_model],
            "resume_from_stored_answer": [
                e.analysis_execution_id for e in self.resume_from_stored_answer
            ],
            "note": (
                "a sweep that looked for watches at TRIGGER_HIT would find none of these: by the "
                "time the model is called the watch has already moved to IN_REANALYSIS"
            ),
        }


def plan_recovery(store: ExecutionStore) -> RecoveryPlan:
    """Split the unfinished analyses by what they still need.

    IN_REANALYSIS is a recovery state, not a resting one. A runner that only
    picked up TRIGGER_HIT would walk past every analysis that crashed, because
    crashing is what leaves them one state further on.
    """

    plan = RecoveryPlan()
    for execution in store.in_flight():
        if execution.needs_the_model:
            plan.call_the_model.append(execution)
        else:
            plan.resume_from_stored_answer.append(execution)
    return plan


__all__ = [
    "EXECUTION_VERSION",
    "AnalysisExecution",
    "BeginResult",
    "ExecutionError",
    "ExecutionKey",
    "ExecutionStatus",
    "ExecutionStore",
    "TRANSIENT_FAILURES",
    "FailureClass",
    "InMemoryExecutionStore",
    "RecoveryPlan",
    "plan_recovery",
]
