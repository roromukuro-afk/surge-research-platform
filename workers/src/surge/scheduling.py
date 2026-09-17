"""Scheduler and JobRunner, kept separate from whatever actually runs jobs.

Nothing in this project may assume GitHub Actions, or a cron on a laptop, or a
worker in a queue. The scheduler decides WHAT should run and emits a request;
a runner decides WHERE. Swapping the runner - which will happen, because the
free execution tiers change - must not touch a single job.

Two properties carry the weight:

*The idempotency key is the logical invocation.* It names the job, the market,
the date and the configuration, and deliberately not the run id or the attempt.
Two schedulers that both notice Friday's close is out produce the same key, and
the database's unique constraint makes one of them a no-op.

*A job declares what it needs.* A job that needs a credential the deployment
does not have is SKIPPED with the variable named, not failed and not silently
dropped. That is what lets the zero-cost core run while the optional paid
providers sit unconfigured.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from typing import Any, Protocol

JOB_SCHEDULE_VERSION = "schedule-1.0.0"


class RunMode(StrEnum):
    PRODUCTION = "PRODUCTION"
    RESEARCH = "RESEARCH"
    DEV = "DEV"


class JobStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


@dataclass(frozen=True)
class JobRequest:
    request_id: str
    job_name: str
    job_version: str
    run_mode: RunMode
    market: str | None
    as_of_date: date | None
    params: dict[str, Any]
    idempotency_key: str
    requested_by: str
    not_before: datetime | None = None
    deadline: datetime | None = None
    max_attempts: int = 3
    timeout_seconds: int = 3600

    def as_dict(self) -> dict:
        return {
            "request_id": self.request_id,
            "job_name": self.job_name,
            "job_version": self.job_version,
            "run_mode": str(self.run_mode),
            "market": self.market,
            "as_of_date": self.as_of_date.isoformat() if self.as_of_date else None,
            "params": self.params,
            "idempotency_key": self.idempotency_key,
            "requested_by": self.requested_by,
        }


@dataclass(frozen=True)
class JobResult:
    request: JobRequest
    status: JobStatus
    detail: str = ""
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RunnerCapabilities:
    runner_id: str
    max_duration_seconds: int
    persistent: bool
    concurrency: int
    has_network: bool


@dataclass(frozen=True)
class JobDefinition:
    """What a job is, independently of when or where it runs."""

    job_name: str
    description: str
    markets: tuple[str, ...]
    required_env: tuple[str, ...] = ()
    provider_role: str | None = None
    # Days of the week the job is meant to run, Monday = 0. Empty means daily.
    weekdays: tuple[int, ...] = ()
    # Minutes after midnight UTC at which it becomes due.
    due_at_utc_minutes: int = 0

    def missing_credentials(self, env: dict[str, str] | None = None) -> list[str]:
        source = env if env is not None else dict(os.environ)
        return [name for name in self.required_env if not source.get(name)]


# The Phase 2 job set. Every one of these runs on free sources except the two
# marked as needing an optional paid credential, which are SKIPPED when it is
# absent rather than failing the schedule.
JOB_DEFINITIONS: dict[str, JobDefinition] = {
    "daily_universe_sync": JobDefinition(
        "daily_universe_sync",
        "Rebuild the security master and universe evaluations from the free registry sources.",
        markets=("JP", "US"),
        weekdays=(0, 1, 2, 3, 4),
        due_at_utc_minutes=21 * 60,
    ),
    "jp_eod_fetch": JobDefinition(
        "jp_eod_fetch",
        "Fetch one Japanese trading day of end-of-day bars.",
        markets=("JP",),
        provider_role="EOD_CURRENT_JP",
        weekdays=(0, 1, 2, 3, 4),
        due_at_utc_minutes=8 * 60,  # after the 15:00 JST close
    ),
    "us_eod_fetch": JobDefinition(
        "us_eod_fetch",
        "Fetch one US trading day of end-of-day bars.",
        markets=("US",),
        provider_role="EOD_CURRENT_US",
        weekdays=(0, 1, 2, 3, 4),
        due_at_utc_minutes=22 * 60,  # after the 16:00 ET close
    ),
    "fx_fetch": JobDefinition(
        "fx_fetch",
        "Fetch the ECB euro reference rates and derive USD/JPY.",
        markets=("GLOBAL",),
        provider_role="FX_USDJPY",
        weekdays=(0, 1, 2, 3, 4),
        due_at_utc_minutes=15 * 60,  # after the ~16:00 CET publication
    ),
    "revision_window_refetch": JobDefinition(
        "revision_window_refetch",
        "Re-read recent days and compare digests, because corrections are silent.",
        markets=("JP", "US"),
        weekdays=(0, 1, 2, 3, 4),
        due_at_utc_minutes=23 * 60,
    ),
    "price_eligibility": JobDefinition(
        "price_eligibility",
        "Apply the 3,000 JPY filter across the universe for one trading date.",
        markets=("JP", "US"),
        weekdays=(0, 1, 2, 3, 4),
        due_at_utc_minutes=23 * 60 + 30,
    ),
    "stage1_screening": JobDefinition(
        "stage1_screening",
        "Compute daily features and run Routes A-H over the priced universe.",
        markets=("JP", "US"),
        weekdays=(0, 1, 2, 3, 4),
        due_at_utc_minutes=23 * 60 + 45,
    ),
}


def config_hash(config: dict[str, Any]) -> str:
    """Order-independent fingerprint of the configuration a run used."""

    payload = json.dumps(config, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_idempotency_key(
    *,
    job_name: str,
    run_mode: RunMode,
    market: str | None,
    as_of_date: date | None,
    config_fingerprint: str,
    job_version: str,
) -> str:
    """The logical identity of an invocation. No run id, no attempt, no clock."""

    payload = json.dumps(
        {
            "job": job_name,
            "run_mode": str(run_mode),
            "market": market or "GLOBAL",
            "as_of": as_of_date.isoformat() if as_of_date else None,
            "job_version": job_version,
            "config": config_fingerprint,
        },
        sort_keys=True,
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    return f"{job_name}:{market or 'GLOBAL'}:{as_of_date.isoformat() if as_of_date else 'none'}:{digest}"


class JobRunner(Protocol):
    runner_id: str

    def capabilities(self) -> RunnerCapabilities: ...

    def submit(self, request: JobRequest) -> JobResult: ...


class Scheduler(Protocol):
    scheduler_id: str

    def tick(self, now: datetime) -> list[JobRequest]: ...


class CalendarScheduler:
    """Emits the jobs that are due, and says why the others are not.

    Holidays are not modelled here. A job that runs on a holiday finds no bars
    and reports an empty day, which is the honest outcome until a real trading
    calendar exists - inventing one now would hide real outages behind a guess.
    """

    scheduler_id = "calendar-scheduler-1.0.0"

    def __init__(
        self,
        *,
        definitions: dict[str, JobDefinition] | None = None,
        run_mode: RunMode = RunMode.PRODUCTION,
        config: dict[str, Any] | None = None,
        env: dict[str, str] | None = None,
        job_version: str = JOB_SCHEDULE_VERSION,
        requested_by: str = "calendar-scheduler",
        id_factory: Callable[[], str] = lambda: str(uuid.uuid4()),
    ) -> None:
        self._definitions = definitions or JOB_DEFINITIONS
        self._run_mode = run_mode
        self._config = config or {}
        self._env = env
        self._job_version = job_version
        self._requested_by = requested_by
        self._id_factory = id_factory
        self.skipped: list[tuple[str, str]] = []

    def tick(self, now: datetime) -> list[JobRequest]:
        self.skipped = []
        requests: list[JobRequest] = []
        minutes = now.hour * 60 + now.minute
        fingerprint = config_hash(self._config)

        for definition in self._definitions.values():
            if definition.weekdays and now.weekday() not in definition.weekdays:
                continue
            if minutes < definition.due_at_utc_minutes:
                continue

            missing = definition.missing_credentials(self._env)
            if missing:
                self.skipped.append(
                    (definition.job_name, f"missing credentials: {', '.join(missing)}")
                )
                continue

            for market in definition.markets:
                as_of = _previous_weekday(now.date())
                requests.append(
                    JobRequest(
                        request_id=self._id_factory(),
                        job_name=definition.job_name,
                        job_version=self._job_version,
                        run_mode=self._run_mode,
                        market=None if market == "GLOBAL" else market,
                        as_of_date=as_of,
                        params={"provider_role": definition.provider_role},
                        idempotency_key=build_idempotency_key(
                            job_name=definition.job_name,
                            run_mode=self._run_mode,
                            market=market,
                            as_of_date=as_of,
                            config_fingerprint=fingerprint,
                            job_version=self._job_version,
                        ),
                        requested_by=self._requested_by,
                    )
                )

        return requests


class LocalJobRunner:
    """Runs a job in this process. Development, tests, and a laptop cron.

    The handlers are injected, so the runner knows nothing about the jobs and
    the jobs know nothing about the runner.
    """

    runner_id = "local-runner-1.0.0"

    def __init__(self, handlers: dict[str, Callable[[JobRequest], dict[str, Any]]]) -> None:
        self._handlers = handlers
        self._seen: set[str] = set()

    def capabilities(self) -> RunnerCapabilities:
        return RunnerCapabilities(
            runner_id=self.runner_id,
            max_duration_seconds=6 * 3600,
            persistent=False,
            concurrency=1,
            has_network=True,
        )

    def submit(self, request: JobRequest) -> JobResult:
        if request.idempotency_key in self._seen:
            # The database enforces this for real; the runner refusing early
            # keeps a double tick from doing the work twice before it finds out.
            return JobResult(request, JobStatus.SKIPPED, "already run in this process")
        handler = self._handlers.get(request.job_name)
        if handler is None:
            return JobResult(request, JobStatus.SKIPPED, f"no handler for {request.job_name}")

        self._seen.add(request.idempotency_key)
        try:
            payload = handler(request)
        except Exception as exc:  # noqa: BLE001 - a runner reports, it does not raise
            return JobResult(request, JobStatus.FAILED, f"{type(exc).__name__}: {exc}")
        return JobResult(request, JobStatus.SUCCEEDED, payload=payload or {})


def _previous_weekday(day: date) -> date:
    previous = day - timedelta(days=1)
    while previous.weekday() >= 5:
        previous -= timedelta(days=1)
    return previous


def trading_days(start: date, end: date, *, holidays: Sequence[date] = ()) -> list[date]:
    """Weekdays in a range, minus any holidays the caller knows about.

    The holiday list is an argument rather than a table: this project has no
    verified trading calendar yet, and a wrong one would silently skip real
    sessions. Passing none means weekdays, and a missing day then shows up as an
    empty ingest rather than as a day nobody looked at.
    """

    excluded = set(holidays)
    days: list[date] = []
    current = start
    while current <= end:
        if current.weekday() < 5 and current not in excluded:
            days.append(current)
        current += timedelta(days=1)
    return days


def next_due(definition: JobDefinition, after: datetime) -> datetime:
    """When this job is next due, strictly after ``after``."""

    candidate = datetime.combine(
        after.date(), time(definition.due_at_utc_minutes // 60, definition.due_at_utc_minutes % 60), UTC
    )
    while candidate <= after or (definition.weekdays and candidate.weekday() not in definition.weekdays):
        candidate += timedelta(days=1)
        candidate = datetime.combine(
            candidate.date(),
            time(definition.due_at_utc_minutes // 60, definition.due_at_utc_minutes % 60),
            UTC,
        )
    return candidate
