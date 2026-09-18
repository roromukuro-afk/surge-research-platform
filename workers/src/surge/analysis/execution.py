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
from datetime import UTC, datetime, timedelta
from decimal import Decimal
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

    #: Terminal. The adapter is written and has never had a credential, which is
    #: a configuration fact rather than a fact about the security.
    PROVIDER_NOT_CONFIGURED = "PROVIDER_NOT_CONFIGURED"

    #: Terminal. The stored input no longer rebuilds into the prompt that was
    #: sent, so this analysis cannot be resumed as itself. Continuing would
    #: attach an answer to a request nothing recorded.
    INPUT_RECONSTRUCTION_MISMATCH = "INPUT_RECONSTRUCTION_MISMATCH"

    #: Terminal, and reached from a transient failure rather than declared. A
    #: provider that is down for an afternoon is transient on every single
    #: attempt and permanent in effect: retrying without a budget rebuilds the
    #: stranded watch it was introduced to prevent, only slowly enough not to
    #: notice.
    RETRY_EXHAUSTED = "RETRY_EXHAUSTED"
    #: Terminal because of *when*, not *what*. An entry decision is about a
    #: price, and a price from forty minutes ago is a different security. Even a
    #: successful answer this late would be answering a question nobody asked.
    ANALYSIS_DEADLINE_PASSED = "ANALYSIS_DEADLINE_PASSED"

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


#: How many times one execution may be retried before the transient failure is
#: treated as the permanent condition it has turned out to be.
MAX_TRANSIENT_ATTEMPTS = 5

#: How stale the inputs may be and still produce a decision.
#:
#: Measured from ``decision_cutoff_at`` - the moment the information was frozen -
#: rather than from when the row was inserted. That is the question being asked:
#: an answer arriving forty minutes after the cutoff is an answer about a
#: forty-minute-old price however promptly the row was written, and the insert
#: clock is the server's rather than the decision's, so a replay would get a
#: different verdict from the same facts.
#:
#: Not a timeout on the call - a limit on the *answer*. Better to leave the
#: trigger unanswered and let the watch re-arm than to enter on a stale reading.
ANALYSIS_DEADLINE = timedelta(minutes=30)


class ProviderFailure(RuntimeError):
    """A provider failure that says for itself whether trying again could help.

    Written here rather than in any one adapter because the runner must not know
    which provider it is talking to, and read by :func:`classify_provider_failure`
    rather than by comparing exception names - a rename would otherwise turn a
    terminal failure into a retried one without anything failing.
    """

    #: Overridden per subclass. The base is the conservative reading: something
    #: went wrong at the provider and nothing says it was the network.
    failure_class: FailureClass = FailureClass.PROVIDER_ERROR


#: Exceptions that are the network rather than the answer. Retrying these is the
#: whole reason RETRY_PENDING exists.
_TRANSIENT_EXCEPTIONS: tuple[type[BaseException], ...] = (TimeoutError, ConnectionError)


def classify_provider_failure(exc: BaseException) -> FailureClass:
    """What kind of failure the provider just had.

    The default is **terminal**, which inverts what this code did first. An
    earlier version treated every exception from the provider as transient, so a
    model that reached outside the bundle, an exhausted free quota and a missing
    credential were all retried - the external-tool case actively wrongly, since
    retrying means calling the provider again and possibly leaking again.

    Defaulting to terminal is safe now in a way it was not before: a terminal
    failure moves the watch to REARMED rather than leaving it in IN_REANALYSIS,
    so a misjudged blip costs one unanswered trigger rather than a security.
    Misjudging the other way costs five calls and half an hour, and in the
    external-tool case it costs the thing the ban exists to prevent.
    """

    if isinstance(exc, ProviderFailure):
        return exc.failure_class
    if isinstance(exc, _TRANSIENT_EXCEPTIONS):
        return (
            FailureClass.PROVIDER_TIMEOUT
            if isinstance(exc, TimeoutError)
            else FailureClass.PROVIDER_ERROR
        )
    if isinstance(exc, OSError):
        # Socket-level trouble reaching the host.
        return FailureClass.PROVIDER_ERROR
    return FailureClass.CONTRACT_VIOLATION


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


#: Bumped when the shape of the stored input changes, so a row written by an
#: older runner is recognisable rather than silently misread.
INPUT_VERSION = "entry-input-1.0.0"


@dataclass(frozen=True)
class StoredInput:
    """Exactly what an analysis was asked, written at TX1 before anything is sent.

    The reason this exists: without it, "the record survives a crash" was true
    of the *fact* that an analysis started and false of everything needed to
    finish one. The bundle, the guard facts and the thesis lived in the caller's
    memory, and the recovery pass took them back as arguments - so recovery
    depended on the process that died still being alive.

    A recovery pass that rebuilt the bundle instead would be building a
    different one. Twenty minutes on, an intraday bundle is a different price, a
    different tape and possibly a different universe verdict, and an answer to
    it recorded against the original trigger would be a decision attributed to
    inputs it never saw.

    ``prompt_sha256`` is the proof. It is fixed here, before the call, and a
    resumed analysis rebuilds the prompt and requires the hash to match.
    """

    bundle_serialized: str
    prompt_sha256: str
    bundle_sha256: str
    canonical_prompt_sha256: str
    addenda_sha256: tuple[str, ...] = ()
    thesis_key: str | None = None
    setup_ids: tuple[str, ...] = ()
    decision_price: Decimal | None = None
    decision_price_currency: str | None = None
    decision_price_observed_at: datetime | None = None
    fx_rate: Decimal | None = None
    fx_observed_at: datetime | None = None
    price_limit_jpy: Decimal | None = None
    universe_decision: str | None = None
    universe_reason_code: str | None = None
    coverage_meets_requirements: bool | None = None
    coverage_detail: str | None = None
    input_version: str = INPUT_VERSION

    def __post_init__(self) -> None:
        for name in ("prompt_sha256", "bundle_sha256", "canonical_prompt_sha256"):
            value = getattr(self, name)
            if not value or len(value) != 64:
                raise ExecutionError(
                    f"{name} must be a full SHA-256 hex digest; an analysis whose input is only "
                    "partly identified cannot be proved to be the same analysis later"
                )
        if not self.bundle_serialized:
            raise ExecutionError(
                "an analysis is started from a stored bundle; without it a crash leaves a row "
                "nothing can resume, which is the failure this record exists to remove"
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
    #: What this analysis was asked. Present from TX1 for anything a production
    #: runner started; absent only for the in-memory store's older callers.
    stored_input: StoredInput | None = None
    #: The runner's own clock, not a caller's opinion. ``analysis_answered_at``
    #: is when the model replied; ``decision_completed_at`` is when this system
    #: accepted the answer, after semantic validation - which is the moment an
    #: entry price becomes observable.
    analysis_answered_at: datetime | None = None
    decision_completed_at: datetime | None = None
    entry_price_observed_at: datetime | None = None

    @property
    def is_finished(self) -> bool:
        return not self.status.is_open

    def retry_budget_spent(self, now: datetime) -> FailureClass | None:
        """Whether retrying again would be pretending the problem is temporary.

        Returns the terminal failure class to use, or None while there is still
        budget. Two limits, because they catch different shapes of the same
        thing: a provider erroring quickly many times, and enough time passing
        that the answer would be about a different market.
        """

        if self.attempt_count >= MAX_TRANSIENT_ATTEMPTS:
            return FailureClass.RETRY_EXHAUSTED
        cutoff = self.decision_cutoff_at
        if cutoff.tzinfo is None:
            cutoff = cutoff.replace(tzinfo=UTC)
        reference = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
        if reference - cutoff > ANALYSIS_DEADLINE:
            return FailureClass.ANALYSIS_DEADLINE_PASSED
        return None

    @property
    def has_a_stored_answer(self) -> bool:
        return self.stored_answer is not None

    @property
    def needs_the_model(self) -> bool:
        """Whether recovery has to call the provider, or can resume from disk."""

        return self.status.is_open and not self.has_a_stored_answer

    @property
    def can_be_resumed(self) -> bool:
        """Whether a recovery pass can act on this at all.

        An open execution with no stored input holds its watch at IN_REANALYSIS
        and cannot be finished by anybody, because nothing records what it was
        asked. Reporting that is the point: it is a stuck security, not an
        absence of work.
        """

        return self.stored_input is not None

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
        stored_input: StoredInput,
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
        answered_at: datetime,
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
        decision_completed_at: datetime | None = None,
        entry_price: object | None = None,
        entry_price_method: str | None = None,
        entry_price_evidence: tuple[str, ...] = (),
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

    def find(self, key: ExecutionKey) -> AnalysisExecution | None: ...


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
        stored_input: StoredInput,
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
            stored_input=stored_input,
            #: Fixed here, with the request, rather than when the answer comes
            #: back. That is what lets a recovery pass rebuild the prompt and
            #: prove it is rebuilding the same one.
            prompt_sha256=stored_input.prompt_sha256,
            bundle_sha256=stored_input.bundle_sha256,
            canonical_prompt_sha256=stored_input.canonical_prompt_sha256,
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
        answered_at: datetime,
        raw_response_ref: str | None = None,
    ) -> None:
        if not (prompt_sha256 and bundle_sha256 and canonical_prompt_sha256):
            raise ExecutionError(
                "an answer is stored with the hashes of what produced it; without them the "
                "stored answer belongs to no particular request (CLAUDE.md 1-18)"
            )
        execution = self._started(execution_id)
        # Checked, not written. The hashes were fixed when the request was
        # recorded; a runner that rendered a different prompt in between would
        # otherwise overwrite the record of what it started with, leaving the
        # stored input describing a request that produced no answer.
        for name, given in (
            ("prompt_sha256", prompt_sha256),
            ("bundle_sha256", bundle_sha256),
            ("canonical_prompt_sha256", canonical_prompt_sha256),
        ):
            stored = getattr(execution, name)
            if stored is not None and stored != given:
                raise ExecutionError(
                    f"the answer to {execution_id} was produced from a different request than the "
                    f"one recorded at its start: {name} was {stored} and is now {given}"
                )
        execution.stored_answer = response
        execution.response_sha256 = response.response_sha256
        execution.prompt_sha256 = prompt_sha256
        execution.bundle_sha256 = bundle_sha256
        execution.canonical_prompt_sha256 = canonical_prompt_sha256
        execution.raw_response_ref = raw_response_ref
        execution.analysis_answered_at = answered_at
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
        decision_completed_at: datetime | None = None,
        entry_price: object | None = None,
        entry_price_method: str | None = None,
        entry_price_evidence: tuple[str, ...] = (),
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
        execution.decision_completed_at = decision_completed_at or execution.decision_completed_at
        if entry_price is not None:
            execution.entry_price_observed_at = entry_price.observed_at
        del entry_price_method, entry_price_evidence  # recorded by the database store

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

    def find(self, key: ExecutionKey) -> AnalysisExecution | None:
        """The execution for this trigger, if one was ever started.

        Asked before anything is built, so that a resumed analysis uses the
        input it was started with rather than one assembled from the session as
        it is now.
        """

        execution_id = self._by_key.get(key)
        return self._rows.get(execution_id) if execution_id else None


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
    "ANALYSIS_DEADLINE",
    "EXECUTION_VERSION",
    "MAX_TRANSIENT_ATTEMPTS",
    "AnalysisExecution",
    "BeginResult",
    "ExecutionError",
    "ExecutionKey",
    "ExecutionStatus",
    "ExecutionStore",
    "TRANSIENT_FAILURES",
    "FailureClass",
    "INPUT_VERSION",
    "InMemoryExecutionStore",
    "StoredInput",
    "ProviderFailure",
    "classify_provider_failure",
    "RecoveryPlan",
    "plan_recovery",
]
