"""Local production runtime.

Zero-Cost Core means production starts on the operator's own machine. That is a
legitimate place to run from and it has one failure mode a paid scheduler does
not: the machine is sometimes off. The runner records that as RUNTIME_OFFLINE,
detected from the outside after the fact, so a week of an unplugged laptop is
never mistaken for a week of quiet markets.

Nothing here is Windows-specific. Task Scheduler invokes the CLI; the CLI owns
the lock, the heartbeat and the retries, so a move to a cloud runner replaces
the invoker and nothing else.
"""

from surge.runtime.runner import (
    Attempt,
    DailySchedule,
    Heartbeat,
    JobStatus,
    LocalRunner,
    LockHeld,
    ResumePoint,
    RetryPolicy,
    RunLog,
    missed_occurrences,
    record_offline_gaps,
    resume_point,
)

__all__ = [
    "Attempt",
    "DailySchedule",
    "Heartbeat",
    "JobStatus",
    "LocalRunner",
    "LockHeld",
    "ResumePoint",
    "RetryPolicy",
    "RunLog",
    "missed_occurrences",
    "record_offline_gaps",
    "resume_point",
]
