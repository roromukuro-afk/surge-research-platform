"""A local production runner: one machine, one instance, and an honest record.

Zero-Cost Core means production starts on the operator's own machine rather than
on a scheduler somebody pays for. That is a legitimate place to run from, and it
has one failure mode a cloud scheduler does not: **the machine is sometimes
off**. A run that never happened must not be indistinguishable from a run that
happened and found nothing.

So this records three things that are easy to leave out:

``RUNTIME_OFFLINE``
    A scheduled occurrence that had no attempt at all. Detected by comparing the
    schedule against the attempts, not by anything the runner does at the time -
    a process that is not running cannot log that it is not running.
``the heartbeat``
    Updated while a job is in flight, so a crash is distinguishable from a job
    that is merely slow.
``the single-instance lock``
    Two copies of a daily pipeline running at once would both write, and the
    second would fail on a unique key somewhere far from the cause.

Nothing here is Windows-specific. Task Scheduler (or cron, or a systemd timer)
invokes the CLI; the CLI owns the lock, the heartbeat and the retry policy, so
moving to a cloud runner later replaces the invoker and nothing else.
"""

from __future__ import annotations

import json
import os
import socket
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from datetime import time as clock_time
from enum import StrEnum
from pathlib import Path

RUNNER_VERSION = "local-runner-1.0.0"

#: How stale a heartbeat has to be before a lock is treated as abandoned. Long
#: enough that a slow job is not killed, short enough that a crash does not block
#: the next day.
STALE_LOCK_AFTER = timedelta(minutes=30)


class JobStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    #: Another instance held the lock. Not a failure of the job.
    SKIPPED_LOCKED = "SKIPPED_LOCKED"
    #: The schedule said this should have run and nothing attempted it. Only ever
    #: written by the gap detector, after the fact.
    RUNTIME_OFFLINE = "RUNTIME_OFFLINE"


class RunnerError(RuntimeError):
    pass


class LockHeld(RunnerError):
    """Another instance is running this job."""


@dataclass(frozen=True)
class Attempt:
    job_name: str
    scheduled_for: datetime
    started_at: datetime
    finished_at: datetime | None
    status: JobStatus
    attempt_number: int = 1
    error: str | None = None
    detail: dict = field(default_factory=dict)

    @property
    def duration_seconds(self) -> float | None:
        if self.finished_at is None:
            return None
        return (self.finished_at - self.started_at).total_seconds()

    def as_json(self) -> dict:
        return {
            "job": self.job_name,
            "scheduled_for": self.scheduled_for.isoformat(),
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "status": self.status.value,
            "attempt": self.attempt_number,
            "duration_seconds": self.duration_seconds,
            "error": self.error,
            "detail": self.detail,
            "runner_version": RUNNER_VERSION,
        }


@dataclass(frozen=True)
class RetryPolicy:
    """Bounded, and it gives up loudly.

    A job that retries forever looks healthy from the outside while never
    finishing, which is the worst of both states.
    """

    attempts: int = 3
    initial_delay_seconds: float = 5.0
    multiplier: float = 3.0
    max_delay_seconds: float = 300.0

    def delay_before(self, attempt_number: int) -> float:
        if attempt_number <= 1:
            return 0.0
        delay = self.initial_delay_seconds * (self.multiplier ** (attempt_number - 2))
        return min(delay, self.max_delay_seconds)


@dataclass(frozen=True)
class DailySchedule:
    """When a job is supposed to run, in its own timezone.

    Weekday-only by default, because the pipelines this runs follow trading
    sessions. It is a *schedule*, not a trading calendar: it says when to try,
    and the job itself decides whether there was a session (D-135).
    """

    job_name: str
    at: clock_time
    weekdays_only: bool = True

    def occurrences_between(self, start: datetime, end: datetime) -> list[datetime]:
        out: list[datetime] = []
        day = start.date()
        while day <= end.date():
            if not (self.weekdays_only and day.weekday() >= 5):
                moment = datetime.combine(day, self.at, tzinfo=start.tzinfo or UTC)
                if start <= moment <= end:
                    out.append(moment)
            day += timedelta(days=1)
        return out


class RunLog:
    """One JSON-lines file per day, written as things happen.

    Deliberately files rather than the database: the runner has to be able to
    record that it could not reach the database.
    """

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def path_for(self, day: date) -> Path:
        return self.directory / f"{day.isoformat()}.jsonl"

    def write(self, attempt: Attempt) -> None:
        line = json.dumps(attempt.as_json(), ensure_ascii=False, sort_keys=True)
        with self.path_for(attempt.scheduled_for.date()).open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def read(self, day: date) -> list[dict]:
        path = self.path_for(day)
        if not path.exists():
            return []
        with path.open(encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]

    def attempts_for(self, job_name: str, *, since: datetime, until: datetime) -> list[dict]:
        out: list[dict] = []
        day = since.date()
        while day <= until.date():
            out.extend(row for row in self.read(day) if row["job"] == job_name)
            day += timedelta(days=1)
        return out

    def last_success(self, job_name: str, *, lookback_days: int = 30) -> datetime | None:
        today = datetime.now(UTC).date()
        for offset in range(lookback_days + 1):
            rows = [
                row
                for row in self.read(today - timedelta(days=offset))
                if row["job"] == job_name and row["status"] == JobStatus.SUCCEEDED.value
            ]
            if rows:
                return datetime.fromisoformat(max(row["finished_at"] for row in rows))
        return None


class Heartbeat:
    """A file holding the owner and the last time it was alive.

    The lock and the heartbeat are the same file on purpose. A lock without a
    heartbeat cannot tell a running job from a crashed one, and then the only
    safe policy is to block forever or to ignore the lock - both wrong.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def read(self) -> dict | None:
        if not self.path.exists():
            return None
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            # A half-written heartbeat is not a held lock.
            return None

    def write(self, *, job_name: str, started_at: datetime, now: datetime | None = None) -> None:
        """Record the beat. ``now`` is the beat time, defaulting to the wall clock.

        Taking it as an argument rather than reading the clock directly is what
        lets staleness be reasoned about at all: a heartbeat that always stamps
        "now" is always fresh, which is the one thing it must not be.
        """

        self.path.write_text(
            json.dumps(
                {
                    "job": job_name,
                    "pid": os.getpid(),
                    "host": socket.gethostname(),
                    "started_at": started_at.isoformat(),
                    "beat_at": (now or datetime.now(UTC)).isoformat(),
                    "runner_version": RUNNER_VERSION,
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)

    def is_stale(self, *, now: datetime | None = None) -> bool:
        current = self.read()
        if current is None:
            return True
        now = now or datetime.now(UTC)
        beat = datetime.fromisoformat(current["beat_at"])
        return now - beat > STALE_LOCK_AFTER


@contextmanager
def single_instance(heartbeat: Heartbeat, *, job_name: str, now: datetime | None = None):
    """Hold the lock, or refuse to start.

    An abandoned lock - one whose heartbeat has gone stale - is taken over, and
    the takeover is visible in the log rather than silent.
    """

    now = now or datetime.now(UTC)
    held = heartbeat.read()
    if held is not None and not heartbeat.is_stale(now=now):
        raise LockHeld(
            f"{job_name} is already running as pid {held.get('pid')} on {held.get('host')} "
            f"(last beat {held.get('beat_at')}). Two copies would both write, and the second would "
            "fail on a unique key a long way from the cause"
        )

    took_over = held is not None
    heartbeat.write(job_name=job_name, started_at=now, now=now)
    try:
        yield took_over
    finally:
        heartbeat.clear()


@dataclass
class JobResult:
    status: JobStatus
    detail: dict = field(default_factory=dict)
    error: str | None = None


class LocalRunner:
    """Runs one job, with a lock, retries, a heartbeat and a log."""

    def __init__(
        self,
        *,
        log: RunLog,
        heartbeat: Heartbeat,
        retry: RetryPolicy | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.log = log
        self.heartbeat = heartbeat
        self.retry = retry or RetryPolicy()
        self._sleep = sleep

    def run(
        self,
        job_name: str,
        job: Callable[[], dict],
        *,
        scheduled_for: datetime | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> Attempt:
        scheduled_for = scheduled_for or now()

        try:
            with single_instance(self.heartbeat, job_name=job_name, now=now()) as took_over:
                return self._attempt_with_retries(
                    job_name, job, scheduled_for=scheduled_for, now=now, took_over=took_over
                )
        except LockHeld as held:
            attempt = Attempt(
                job_name=job_name,
                scheduled_for=scheduled_for,
                started_at=now(),
                finished_at=now(),
                status=JobStatus.SKIPPED_LOCKED,
                error=str(held),
            )
            self.log.write(attempt)
            return attempt

    def _attempt_with_retries(self, job_name, job, *, scheduled_for, now, took_over) -> Attempt:
        last: Attempt | None = None
        for number in range(1, self.retry.attempts + 1):
            delay = self.retry.delay_before(number)
            if delay:
                self._sleep(delay)

            started = now()
            self.heartbeat.write(job_name=job_name, started_at=started, now=started)
            try:
                detail = job() or {}
                attempt = Attempt(
                    job_name=job_name,
                    scheduled_for=scheduled_for,
                    started_at=started,
                    finished_at=now(),
                    status=JobStatus.SUCCEEDED,
                    attempt_number=number,
                    detail={**detail, "took_over_a_stale_lock": took_over} if took_over else detail,
                )
                self.log.write(attempt)
                return attempt
            except Exception as exc:  # noqa: BLE001 - the runner records every failure
                last = Attempt(
                    job_name=job_name,
                    scheduled_for=scheduled_for,
                    started_at=started,
                    finished_at=now(),
                    status=JobStatus.FAILED,
                    attempt_number=number,
                    error=f"{type(exc).__name__}: {exc}",
                )
                self.log.write(last)

        return last  # type: ignore[return-value]


#: An occurrence that only just came due has not been missed; it has not been
#: reached yet. Without this, running the detector at the scheduled minute would
#: mark today's run offline before it had a chance to start.
OFFLINE_GRACE = timedelta(hours=1)


def missed_occurrences(
    schedule: DailySchedule,
    log: RunLog,
    *,
    since: datetime,
    until: datetime,
    grace: timedelta = timedelta(0),
) -> list[datetime]:
    """Scheduled moments with no attempt of any kind.

    This is the machine-was-off detector, and it has to work from the outside: a
    process that is not running cannot record that it is not running. Run it at
    the start of the next session so an offline day is a known gap rather than a
    quiet one.

    ``grace`` excludes occurrences within that long of ``until``, which is what
    keeps a run that is merely imminent from being recorded as one that never
    happened.
    """

    attempted = {
        datetime.fromisoformat(row["scheduled_for"])
        for row in log.attempts_for(schedule.job_name, since=since, until=until)
    }
    cutoff = until - grace
    return [
        moment
        for moment in schedule.occurrences_between(since, until)
        if moment not in attempted and moment <= cutoff
    ]


def record_offline_gaps(
    schedule: DailySchedule,
    log: RunLog,
    *,
    since: datetime,
    until: datetime,
    now: datetime | None = None,
) -> list[Attempt]:
    """Write a RUNTIME_OFFLINE row for each missed occurrence.

    The row exists so that a day with no data is distinguishable from a day on
    which nothing happened. Without it, a week of an unplugged laptop looks
    exactly like a week of quiet markets.
    """

    now = now or datetime.now(UTC)
    written: list[Attempt] = []
    for moment in missed_occurrences(
        schedule, log, since=since, until=until, grace=OFFLINE_GRACE
    ):
        attempt = Attempt(
            job_name=schedule.job_name,
            scheduled_for=moment,
            started_at=now,
            finished_at=now,
            status=JobStatus.RUNTIME_OFFLINE,
            error=(
                "the schedule expected a run and nothing attempted one; the runner was not "
                "running. Recorded after the fact, because a process that is not running cannot "
                "log that it is not running"
            ),
        )
        log.write(attempt)
        written.append(attempt)
    return written


@dataclass(frozen=True)
class ResumePoint:
    """Where to pick up, and what was skipped getting there."""

    last_success_at: datetime | None
    missed: list[datetime]

    @property
    def has_a_gap(self) -> bool:
        return bool(self.missed)


def resume_point(
    log: RunLog,
    schedule: DailySchedule,
    *,
    now: datetime | None = None,
    lookback_days: int = 30,
) -> ResumePoint:
    """The last success and every scheduled moment missed since it.

    Returned together because they are one question. A caller given only the
    last success would resume from it and skip the gap in silence - which is the
    whole failure the offline record exists to prevent.
    """

    now = now or datetime.now(UTC)
    last = log.last_success(schedule.job_name, lookback_days=lookback_days)
    since = last or (now - timedelta(days=lookback_days))
    return ResumePoint(
        last_success_at=last,
        missed=missed_occurrences(schedule, log, since=since, until=now, grace=OFFLINE_GRACE),
    )


__all__ = [
    "RUNNER_VERSION",
    "STALE_LOCK_AFTER",
    "Attempt",
    "DailySchedule",
    "Heartbeat",
    "JobResult",
    "JobStatus",
    "OFFLINE_GRACE",
    "LocalRunner",
    "LockHeld",
    "RetryPolicy",
    "RunLog",
    "ResumePoint",
    "RunnerError",
    "missed_occurrences",
    "record_offline_gaps",
    "resume_point",
    "single_instance",
]
