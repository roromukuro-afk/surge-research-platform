"""The local runner and the readiness report.

Two things here are worth more than the rest. The runner has to record that the
machine was *off* - a gap that nothing could have logged at the time, because a
process that is not running cannot log that it is not running. And the readiness
report has to be wrong in the alarming direction rather than the reassuring one:
a check it could not run is a check that failed.
"""

from __future__ import annotations

import json
import pathlib
from datetime import UTC, datetime, timedelta
from datetime import time as clock_time

import pytest

from surge.runtime.readiness import (
    CheckStatus,
    MarketInputs,
    Verdict,
    assess,
    assess_all,
    check_teacher_still_zero,
)
from surge.runtime.runner import (
    STALE_LOCK_AFTER,
    DailySchedule,
    Heartbeat,
    JobStatus,
    LocalRunner,
    LockHeld,
    RetryPolicy,
    RunLog,
    missed_occurrences,
    record_offline_gaps,
    resume_point,
    single_instance,
)

MONDAY = datetime(2026, 9, 14, 7, 0, tzinfo=UTC)


@pytest.fixture()
def runner(tmp_path):
    log = RunLog(tmp_path / "logs")
    beat = Heartbeat(tmp_path / "runner.lock")
    return LocalRunner(log=log, heartbeat=beat, sleep=lambda _seconds: None), log, beat


# ------------------------------------------------------------------ the runner


def test_a_successful_job_is_logged_once(runner):
    run, log, _ = runner
    attempt = run.run("eod", lambda: {"rows": 3}, scheduled_for=MONDAY, now=lambda: MONDAY)

    assert attempt.status is JobStatus.SUCCEEDED
    assert log.read(MONDAY.date())[0]["detail"] == {"rows": 3}


def test_a_failing_job_retries_and_then_gives_up_loudly(runner):
    run, log, _ = runner
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        raise RuntimeError("the provider timed out")

    attempt = run.run("eod", flaky, scheduled_for=MONDAY, now=lambda: MONDAY)

    assert calls["n"] == RetryPolicy().attempts
    assert attempt.status is JobStatus.FAILED
    # Every attempt is logged, not just the last. A job that failed twice and
    # then worked is a different thing from one that worked first time.
    assert len(log.read(MONDAY.date())) == RetryPolicy().attempts


def test_a_job_that_recovers_logs_the_failures_too(runner):
    run, log, _ = runner
    calls = {"n": 0}

    def recovers():
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("transient")
        return {"rows": 1}

    attempt = run.run("eod", recovers, scheduled_for=MONDAY, now=lambda: MONDAY)
    rows = log.read(MONDAY.date())

    assert attempt.status is JobStatus.SUCCEEDED
    assert [row["status"] for row in rows] == ["FAILED", "FAILED", "SUCCEEDED"]


def test_the_backoff_is_bounded():
    policy = RetryPolicy(initial_delay_seconds=5, multiplier=10, max_delay_seconds=60)

    assert policy.delay_before(1) == 0
    assert policy.delay_before(2) == 5
    assert policy.delay_before(5) == 60


# ------------------------------------------------------------ single instance


def test_a_second_instance_is_refused(tmp_path):
    beat = Heartbeat(tmp_path / "lock")
    with single_instance(beat, job_name="eod", now=MONDAY):
        with pytest.raises(LockHeld, match="already running"):
            with single_instance(beat, job_name="eod", now=MONDAY):
                pass


def test_the_lock_is_released_even_when_the_job_raises(tmp_path):
    beat = Heartbeat(tmp_path / "lock")
    with pytest.raises(RuntimeError), single_instance(beat, job_name="eod", now=MONDAY):
        raise RuntimeError("boom")

    assert beat.read() is None


def test_a_stale_lock_is_taken_over(tmp_path):
    """A crashed job must not block tomorrow's run forever."""

    beat = Heartbeat(tmp_path / "lock")
    beat.write(job_name="eod", started_at=MONDAY, now=MONDAY)
    much_later = MONDAY + STALE_LOCK_AFTER + timedelta(minutes=1)

    with single_instance(beat, job_name="eod", now=much_later) as took_over:
        assert took_over


def test_a_fresh_lock_is_not_taken_over(tmp_path):
    beat = Heartbeat(tmp_path / "lock")
    beat.write(job_name="eod", started_at=MONDAY, now=MONDAY)

    with pytest.raises(LockHeld):
        with single_instance(beat, job_name="eod", now=MONDAY + timedelta(minutes=1)):
            pass


def test_a_locked_run_is_skipped_rather_than_failed(runner):
    """Another instance holding the lock is not a failure of the job."""

    run, log, beat = runner
    beat.write(job_name="eod", started_at=MONDAY, now=MONDAY)

    attempt = run.run("eod", lambda: {}, scheduled_for=MONDAY, now=lambda: MONDAY)

    assert attempt.status is JobStatus.SKIPPED_LOCKED


def test_a_corrupt_heartbeat_is_not_a_held_lock(tmp_path):
    beat = Heartbeat(tmp_path / "lock")
    beat.path.write_text("{not json", encoding="utf-8")

    with single_instance(beat, job_name="eod", now=MONDAY):
        pass


# ------------------------------------------------------ the machine being off


def _schedule() -> DailySchedule:
    return DailySchedule(job_name="eod", at=clock_time(7, 0))


def test_weekends_are_not_scheduled_occurrences():
    schedule = _schedule()
    week = schedule.occurrences_between(MONDAY, MONDAY + timedelta(days=6))

    assert len(week) == 5


def test_a_day_with_no_attempt_at_all_is_a_missed_occurrence(runner):
    run, log, _ = runner
    run.run("eod", lambda: {}, scheduled_for=MONDAY, now=lambda: MONDAY)
    tuesday = MONDAY + timedelta(days=1)

    missed = missed_occurrences(_schedule(), log, since=MONDAY, until=tuesday)

    assert missed == [tuesday]


def test_an_offline_day_is_recorded_after_the_fact(runner):
    """A week of an unplugged laptop must not look like a week of quiet markets."""

    run, log, _ = runner
    run.run("eod", lambda: {}, scheduled_for=MONDAY, now=lambda: MONDAY)
    friday = MONDAY + timedelta(days=4)

    written = record_offline_gaps(_schedule(), log, since=MONDAY, until=friday, now=friday)

    # Tuesday, Wednesday, Thursday. Friday's is only just due.
    assert [a.status for a in written] == [JobStatus.RUNTIME_OFFLINE] * 3
    assert all("was not running" in a.error for a in written)


def test_an_occurrence_that_has_only_just_come_due_is_not_missed(runner):
    """Running the detector at the scheduled minute must not mark today's run
    offline before it has had a chance to start."""

    run, log, _ = runner
    tuesday = MONDAY + timedelta(days=1)

    assert record_offline_gaps(_schedule(), log, since=tuesday, until=tuesday, now=tuesday) == []


def test_recording_a_gap_twice_does_not_duplicate_it(runner):
    run, log, _ = runner
    wednesday = MONDAY + timedelta(days=2)
    first = record_offline_gaps(_schedule(), log, since=MONDAY, until=wednesday, now=wednesday)
    again = record_offline_gaps(_schedule(), log, since=MONDAY, until=wednesday, now=wednesday)

    assert first
    assert again == []


def test_the_resume_point_carries_the_gap_with_it(runner):
    """Given only the last success, a caller would resume from it and skip the
    gap in silence - which is the failure the offline record exists to prevent."""

    run, log, _ = runner
    run.run("eod", lambda: {}, scheduled_for=MONDAY, now=lambda: MONDAY)
    thursday = MONDAY + timedelta(days=3)

    point = resume_point(log, _schedule(), now=thursday)

    assert point.has_a_gap
    # Tuesday and Wednesday. Thursday's occurrence is only just due and has not
    # been missed - it has not been reached yet.
    assert len(point.missed) == 2


def test_the_log_is_json_lines_on_disk(runner, tmp_path):
    run, log, _ = runner
    run.run("eod", lambda: {"rows": 1}, scheduled_for=MONDAY, now=lambda: MONDAY)

    text = log.path_for(MONDAY.date()).read_text(encoding="utf-8")
    parsed = [json.loads(line) for line in text.splitlines() if line.strip()]

    assert parsed[0]["job"] == "eod"
    assert parsed[0]["runner_version"]


# --------------------------------------------------------------- readiness


def _market(code="JP", **overrides) -> MarketInputs:
    base = {
        "market_code": code,
        "security_master_fresh": True,
        "universe_run_published": True,
        "mock_guard_constraints": 1,
        "migrations_in_sync": True,
        "ci_head_green": True,
        "scheduler_configured": True,
        "object_store_configured": True,
    }
    base.update(overrides)
    return MarketInputs(**base)


def test_a_market_with_nothing_connected_is_blocked():
    assert assess(_market(material_sources_live=0)).verdict is Verdict.BLOCKED


def test_live_materials_without_a_price_is_partial_live():
    """JP today: timely disclosures are live-verified and no price source exists.
    That is a real state and worth naming separately from BLOCKED."""

    readiness = assess(_market(material_sources_live=1))

    assert readiness.verdict is Verdict.PARTIAL_LIVE
    assert "D-102" in readiness.blocker_ids
    assert "D-06b" in readiness.blocker_ids


def _connected(**overrides) -> dict:
    base = {
        "material_sources_live": 1,
        "eod_price_provider": "a settled provider",
        "intraday_price_provider": "a settled provider",
        "eod_analysis_provider": "a real model",
        "eod_analysis_is_a_stand_in": False,
        "entry_analysis_provider": "a real model",
        "entry_analysis_is_a_stand_in": False,
    }
    base.update(overrides)
    return base


def test_everything_connected_is_live_ready():
    readiness = assess(_market(**_connected()))

    assert readiness.verdict is Verdict.LIVE_READY
    assert readiness.blockers == []


def test_a_stand_in_analysis_provider_blocks_on_its_own():
    readiness = assess(
        _market(
            **_connected(
                eod_analysis_provider="deterministic_mock",
                eod_analysis_is_a_stand_in=True,
                entry_analysis_provider="deterministic_mock",
                entry_analysis_is_a_stand_in=True,
            )
        )
    )

    assert readiness.verdict is Verdict.PARTIAL_LIVE
    assert readiness.blocker_ids == ["D-32-ENTRY", "D-32-EOD"]


def test_a_connected_stage3_alone_is_not_ready_to_predict():
    """Stage 3 has no ENTRY state and runs against a closed market. A model
    wired in there produces setups, and a setup is not a prediction."""

    readiness = assess(
        _market(
            **_connected(
                entry_analysis_provider="deterministic_mock",
                entry_analysis_is_a_stand_in=True,
            )
        )
    )

    assert readiness.verdict is Verdict.PARTIAL_LIVE
    assert readiness.blocker_ids == ["D-32-ENTRY"]


def test_the_us_price_question_is_two_questions():
    """Delayed consolidated history can answer the outcome engine and cannot
    answer what a decision could have been taken at."""

    readiness = assess(
        _market(
            "US",
            **_connected(
                fx_provider="ECB",
                eod_price_provider="alpaca_historical_sip (delayed SIP)",
                intraday_price_provider=None,
            )
        )
    )

    assert "D-103-LIVE" in readiness.blocker_ids
    assert "D-103-EOD" not in readiness.blocker_ids
    assert readiness.verdict is Verdict.PARTIAL_LIVE


def test_a_passing_non_capability_does_not_make_a_market_look_live():
    """"The teacher tables are still empty" is true, useful, and not a pipeline."""

    readiness = assess(_market(material_sources_live=0, teacher_row_count=0))

    assert any(c.name == "teacher_rows_still_zero" and c.status is CheckStatus.PASS
               for c in readiness.checks)
    assert readiness.verdict is Verdict.BLOCKED


def test_the_two_markets_are_judged_separately():
    """JP must not hold US, and US must not hold JP."""

    report = assess_all(
        [
            _market("JP", material_sources_live=1),
            _market("US", **_connected(fx_provider="ECB")),
        ]
    )
    verdicts = {m.market_code: m.verdict for m in report.markets}

    assert verdicts["JP"] is Verdict.PARTIAL_LIVE
    assert verdicts["US"] is Verdict.LIVE_READY
    assert report.any_market_live


def test_a_yen_market_needs_no_fx_and_a_dollar_market_does():
    jp = assess(_market("JP", material_sources_live=1))
    us = assess(_market("US", material_sources_live=1))

    assert next(c for c in jp.checks if c.name == "fx_provider").status is CheckStatus.PASS
    assert next(c for c in us.checks if c.name == "fx_provider").status is CheckStatus.FAIL


def test_the_object_store_is_a_warning_and_never_a_blocker():
    """A local store is enough to start, so D-105 does not hold anything up."""

    readiness = assess(_market(material_sources_live=1, object_store_configured=False))
    check = next(c for c in readiness.checks if c.name == "object_store")

    assert check.status is CheckStatus.WARN
    assert not check.blocks


def test_teacher_rows_appearing_before_production_is_a_warning():
    check = check_teacher_still_zero(7)

    assert check.status is CheckStatus.WARN
    assert "indistinguishable from real data later" in check.detail


def test_the_report_names_the_decision_behind_each_blocker():
    readiness = assess(_market(material_sources_live=1))
    ids = readiness.blocker_ids

    # Each blocker points at the decision that would unblock it, so the report
    # is a list of things to do rather than a list of things that are wrong.
    assert ids
    assert all(i.startswith("D-") for i in ids)


# ------------------------------------------------- the CLI the scheduler calls


def _cli(tmp_path, *argv) -> tuple[int, dict]:
    import io
    from contextlib import redirect_stdout

    from surge.jobs import runner_cli

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = runner_cli.main(["--state-dir", str(tmp_path / "state"), *argv])
    return code, json.loads(buffer.getvalue())


def test_a_dry_run_takes_no_lock_and_writes_nothing(tmp_path):
    """A dry run that took the lock could block the real run it was checking."""

    code, out = _cli(tmp_path, "dry-run", "--job", "jp_eod")

    assert code == 0
    assert out["did_anything"] is False
    assert out["lock_held_by"] is None
    assert not (tmp_path / "state" / "runner.lock").exists()


def test_the_gap_detector_refuses_before_the_machine_was_initialised(tmp_path):
    """Otherwise a fresh install would write a month of RUNTIME_OFFLINE rows for
    days on which this system did not exist - and afterwards those are
    indistinguishable from real outages."""

    code, out = _cli(tmp_path, "gaps", "--job", "jp_eod")

    assert code == 1
    assert out["recorded_offline"] == []
    assert "never been initialised" in out["note"]


def test_after_init_gaps_are_counted_from_the_marker(tmp_path):
    _cli(tmp_path, "init")
    code, out = _cli(tmp_path, "gaps", "--job", "jp_eod")

    assert code == 0
    assert out["recorded_offline"] == []
    assert out["counted_from"]


def test_the_install_marker_is_never_moved_forward(tmp_path):
    """Moving it would erase an outage rather than record one."""

    from surge.jobs.runner_cli import installed_at, record_installation

    state = tmp_path / "state"
    first = record_installation(state, now=MONDAY)
    second = record_installation(state, now=MONDAY + timedelta(days=7))

    assert first == MONDAY
    assert second == MONDAY
    assert installed_at(state) == MONDAY


def test_an_absent_heartbeat_counts_as_stale(tmp_path):
    """A monitor that read "no heartbeat file" as healthy would report a runner
    that has never started as running."""

    code, out = _cli(tmp_path, "heartbeat")

    assert code == 1
    assert out["is_stale"] is True
    assert out["heartbeat"] is None


def test_a_job_with_no_provider_fails_rather_than_reporting_success(tmp_path):
    """A placeholder returning {} would write SUCCEEDED rows for work nobody did,
    and the run log would become evidence of a pipeline that was not running."""

    code, out = _cli(tmp_path, "--attempts", "1", "run", "--job", "jp_eod")

    assert code == 1
    assert out["status"] == "FAILED"
    assert "no live provider bound" in out["error"]


def test_the_schedules_are_the_ones_the_installer_registers(tmp_path):
    """The PowerShell installer names four jobs. A name that drifts out of the
    CLI would register a task that fails every day with an unknown-job error."""

    from surge.jobs.runner_cli import SCHEDULES

    installer = (
        pathlib.Path(__file__).resolve().parents[2] / "ops" / "windows" / "Install-SurgeTasks.ps1"
    ).read_text(encoding="utf-8")

    for name in SCHEDULES:
        assert f'"{name}"' in installer
