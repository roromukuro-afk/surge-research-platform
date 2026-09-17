"""`python -m surge.jobs.runner_cli` - what the scheduler actually invokes.

Task Scheduler, cron and a systemd timer all do the same thing: start a process
at a time. Everything that makes a daily pipeline survivable - the single
instance lock, the heartbeat, the bounded retry, the daily log, the record of
days the machine was off - lives on this side of that boundary, so binding to a
different scheduler later replaces the invoker and nothing else.

Five subcommands, and three of them exist because a scheduled job that can only
be tested by waiting until tomorrow is a job nobody tests:

``run``        acquire the lock and run a job, with retries and a log.
``dry-run``    say exactly what ``run`` would do, and do none of it.
``status``     last success, gaps since, and whether a lock is held.
``heartbeat``  exit non-zero if the heartbeat is stale. A monitor's check.
``gaps``       write the RUNTIME_OFFLINE rows for occurrences nothing attempted.

Nothing here is Windows-specific. ``ops/windows`` holds the registration
scripts, and they call this.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from datetime import time as clock_time
from pathlib import Path

from surge.runtime.runner import (
    STALE_LOCK_AFTER,
    DailySchedule,
    Heartbeat,
    JobStatus,
    LocalRunner,
    RetryPolicy,
    RunLog,
    record_offline_gaps,
    resume_point,
)

DEFAULT_STATE_DIR = Path(os.environ.get("SURGE_STATE_DIR", "")) if os.environ.get(
    "SURGE_STATE_DIR"
) else Path.home() / ".surge"


#: The daily jobs, and when they are expected. Times are in the runner's own
#: timezone; the job decides whether there was a session (D-135), because a
#: schedule is not a trading calendar and must not be mistaken for one.
SCHEDULES: dict[str, DailySchedule] = {
    "jp_eod": DailySchedule(job_name="jp_eod", at=clock_time(16, 30)),
    "jp_materials": DailySchedule(job_name="jp_materials", at=clock_time(19, 0)),
    "us_eod": DailySchedule(job_name="us_eod", at=clock_time(7, 30)),
    "readiness": DailySchedule(job_name="readiness", at=clock_time(8, 0)),
}


def _not_wired(name: str) -> Callable[[], dict]:
    """A job that refuses rather than pretending to have run.

    Every schedule above is real and most of them have no live provider behind
    them yet. A placeholder that returned ``{}`` would write SUCCEEDED rows for
    work nobody did, and the run log would then be evidence of a pipeline that
    was not running.
    """

    def job() -> dict:
        raise RuntimeError(
            f"{name} has no live provider bound yet. This is IMPLEMENTED_NOT_LIVE_VERIFIED: the "
            "runner works and the job it would run does not exist, and recording a success here "
            "would put a fictional run in the log"
        )

    return job


def _paths(args) -> tuple[RunLog, Heartbeat]:
    state = Path(args.state_dir)
    return RunLog(state / "logs"), Heartbeat(state / "runner.lock")


def _marker_path(state_dir) -> Path:
    return Path(state_dir) / "installed_at.json"


def installed_at(state_dir) -> datetime | None:
    """When this machine was first set up to run the pipeline.

    The gap detector works by comparing the schedule against the log, and on a
    fresh machine the log is empty - so without a floor it would report every
    scheduled moment in the lookback window as an outage and then write
    RUNTIME_OFFLINE rows for days on which this system did not exist. Those rows
    are indistinguishable afterwards from real ones, which is exactly the
    confusion the offline record was built to prevent.
    """

    path = _marker_path(state_dir)
    if not path.exists():
        return None
    try:
        return datetime.fromisoformat(json.loads(path.read_text(encoding="utf-8"))["installed_at"])
    except (json.JSONDecodeError, KeyError, OSError, ValueError):
        return None


def record_installation(state_dir, *, now: datetime | None = None) -> datetime:
    """Write the marker once. Never moves it forward: that would erase a gap."""

    existing = installed_at(state_dir)
    if existing is not None:
        return existing
    moment = now or datetime.now(UTC)
    path = _marker_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"installed_at": moment.isoformat(), "host": socket.gethostname()}),
        encoding="utf-8",
    )
    return moment


def _floor(args, point, now: datetime) -> tuple[datetime, str | None]:
    """The earliest moment a gap may be claimed from, and why."""

    if point.last_success_at is not None:
        return point.last_success_at, None
    marker = installed_at(args.state_dir)
    if marker is not None:
        return marker, "no run has ever succeeded; counting from when this machine was set up"
    return now, (
        "this state directory has never been initialised, so there is no point from which an "
        "outage could be claimed. Run `init` (or the install script) first"
    )


def _schedule(name: str) -> DailySchedule:
    try:
        return SCHEDULES[name]
    except KeyError:
        raise SystemExit(
            f"unknown job {name!r}; known jobs are {', '.join(sorted(SCHEDULES))}"
        ) from None


def cmd_run(args) -> int:
    record_installation(args.state_dir)
    log, beat = _paths(args)
    schedule = _schedule(args.job)
    runner = LocalRunner(log=log, heartbeat=beat, retry=RetryPolicy(attempts=args.attempts))
    attempt = runner.run(schedule.job_name, _not_wired(schedule.job_name))
    print(json.dumps(attempt.as_json(), indent=2))
    return 0 if attempt.status is JobStatus.SUCCEEDED else 1


def cmd_dry_run(args) -> int:
    """Everything ``run`` would do, stated, with nothing done.

    Deliberately does not acquire the lock. A dry run that took the lock could
    block the real run it was checking on.
    """

    log, beat = _paths(args)
    schedule = _schedule(args.job)
    now = datetime.now(UTC)
    held = beat.read()
    point = resume_point(log, schedule, now=now)
    floor, floor_note = _floor(args, point, now)
    missed = [moment for moment in point.missed if moment >= floor]

    print(
        json.dumps(
            {
                "job": schedule.job_name,
                "scheduled_at": schedule.at.isoformat(),
                "weekdays_only": schedule.weekdays_only,
                "state_dir": str(args.state_dir),
                "log_file_for_today": str(log.path_for(now.date())),
                "heartbeat_file": str(beat.path),
                "lock_held_by": held,
                "lock_is_stale": beat.is_stale(now=now),
                "last_success_at": (
                    point.last_success_at.isoformat() if point.last_success_at else None
                ),
                "installed_at": (
                    installed_at(args.state_dir).isoformat()
                    if installed_at(args.state_dir)
                    else None
                ),
                "missed_occurrences": [m.isoformat() for m in missed],
                "missed_note": floor_note,
                "retry_attempts": args.attempts,
                "would_run": f"{schedule.job_name} (no live provider bound; it would fail loudly)",
                "did_anything": False,
            },
            indent=2,
        )
    )
    return 0


def cmd_status(args) -> int:
    log, beat = _paths(args)
    now = datetime.now(UTC)
    out = {"now": now.isoformat(), "state_dir": str(args.state_dir), "jobs": {}}
    for name, schedule in sorted(SCHEDULES.items()):
        point = resume_point(log, schedule, now=now)
        floor, floor_note = _floor(args, point, now)
        missed = [moment for moment in point.missed if moment >= floor]
        out["jobs"][name] = {
            "last_success_at": (
                point.last_success_at.isoformat() if point.last_success_at else None
            ),
            "missed_since_then": len(missed),
            "has_a_gap": bool(missed),
            "note": floor_note,
        }
    out["installed_at"] = (
        installed_at(args.state_dir).isoformat() if installed_at(args.state_dir) else None
    )
    out["heartbeat"] = beat.read()
    out["heartbeat_is_stale"] = beat.is_stale(now=now)
    print(json.dumps(out, indent=2))
    # A gap is a finding, not a failure of this command.
    return 0


def cmd_heartbeat(args) -> int:
    """Exit non-zero when the heartbeat is stale or absent.

    Absent counts as stale. A monitor that treated "no heartbeat file" as
    healthy would report a runner that has never started as running.
    """

    _, beat = _paths(args)
    now = datetime.now(UTC)
    current = beat.read()
    stale = beat.is_stale(now=now)
    print(
        json.dumps(
            {
                "heartbeat": current,
                "stale_after_minutes": STALE_LOCK_AFTER.total_seconds() / 60,
                "is_stale": stale,
                "checked_at": now.isoformat(),
            },
            indent=2,
        )
    )
    return 1 if stale else 0


def cmd_gaps(args) -> int:
    """Record the days nothing ran. Run this at the start of a session."""

    log, _ = _paths(args)
    schedule = _schedule(args.job)
    now = datetime.now(UTC)
    point = resume_point(log, schedule, now=now)
    floor, floor_note = _floor(args, point, now)
    if point.last_success_at is None and installed_at(args.state_dir) is None:
        print(
            json.dumps(
                {
                    "job": schedule.job_name,
                    "recorded_offline": [],
                    "note": floor_note,
                },
                indent=2,
            )
        )
        return 1

    # Never earlier than the floor. Days before this machine was set up are not
    # outages, and a RUNTIME_OFFLINE row for one would be indistinguishable
    # afterwards from a day the laptop was genuinely off.
    since = max(floor, now - timedelta(days=args.lookback_days))
    written = record_offline_gaps(schedule, log, since=since, until=now, now=now)
    print(
        json.dumps(
            {
                "job": schedule.job_name,
                "counted_from": since.isoformat(),
                "recorded_offline": [a.scheduled_for.isoformat() for a in written],
                "note": (
                    "a day with no data and a day on which nothing happened are different things, "
                    "and only one of them is a finding about the market"
                ),
            },
            indent=2,
        )
    )
    return 0


def cmd_init(args) -> int:
    """Mark this machine as set up, so a gap can be measured from somewhere."""

    moment = record_installation(args.state_dir)
    log, beat = _paths(args)
    print(
        json.dumps(
            {
                "state_dir": str(args.state_dir),
                "installed_at": moment.isoformat(),
                "log_dir": str(log.directory),
                "heartbeat_file": str(beat.path),
                "note": (
                    "the marker is written once and never moved forward; moving it would erase "
                    "an outage rather than record one"
                ),
            },
            indent=2,
        )
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--state-dir",
        default=str(DEFAULT_STATE_DIR),
        help="where the run log and the heartbeat live (default: ~/.surge)",
    )
    parser.add_argument("--attempts", type=int, default=RetryPolicy().attempts)
    parser.add_argument("--lookback-days", type=int, default=30)
    sub = parser.add_subparsers(dest="command", required=True)

    for name, handler, needs_job in (
        ("init", cmd_init, False),
        ("run", cmd_run, True),
        ("dry-run", cmd_dry_run, True),
        ("status", cmd_status, False),
        ("heartbeat", cmd_heartbeat, False),
        ("gaps", cmd_gaps, True),
    ):
        command = sub.add_parser(name)
        if needs_job:
            command.add_argument("--job", required=True, choices=sorted(SCHEDULES))
        command.set_defaults(handler=handler)

    args = parser.parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
