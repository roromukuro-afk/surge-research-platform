# D-104: binding the daily jobs to Windows Task Scheduler

Production starts on the operator's own machine, because Zero-Cost Core rules out
a scheduler somebody pays for. Task Scheduler is only the invoker: the lock, the
heartbeat, the retry policy and the record of days the machine was off all live
in `surge.jobs.runner_cli`, so switching to cron, a systemd timer or a cloud
runner later replaces the three scripts here and changes no behaviour.

## What is already done

| | |
|---|---|
| the runner | `workers/src/surge/runtime/runner.py` — lock, heartbeat, bounded retry, daily JSONL, `RUNTIME_OFFLINE` detection |
| the CLI the scheduler calls | `workers/src/surge/jobs/runner_cli.py` — `init`, `run`, `dry-run`, `status`, `heartbeat`, `gaps` |
| the binding | the three scripts in this folder |

**No job has a live provider bound yet.** `run` deliberately fails with a message
saying so rather than writing a SUCCEEDED row for work nobody did. That is the
correct state today: the runner is real, the pipeline it would invoke is
`IMPLEMENTED_NOT_LIVE_VERIFIED`, and a run log full of fictional successes would
be worse than an empty one.

## Do not register the tasks yet

The implementation is finished. Registering it is not the next step, and this is
a deliberate decision rather than an oversight (D-199).

No market has a price provider or an analysis provider connected. Registering
the tasks today would start four jobs every day that can only fail, and the run
log - the thing that exists so a real gap is visible - would fill with failures
that mean nothing. Worse, it would teach whoever reads it that the red lines are
normal, which is exactly the habit the offline record depends on not forming.

Register them when one market has a real price source and a real analysis
provider. Until then the steps below are for trying it out, not for production.

## Steps

Run these in PowerShell from `ops/windows`. No administrator rights are needed:
the tasks are registered for your own user, so they run with the permissions that
can actually read `.env.local`.

1. **See what would be registered, without registering it.**

   ```
   .\Install-SurgeTasks.ps1 -DryRun
   ```

   Check the python path and the four times. If `python` is not the interpreter
   you want, pass `-PythonExe "C:\path\to\python.exe"`.

2. **Check the runner works before binding anything to it.**

   ```
   cd ..\..\workers\src
   python -m surge.jobs.runner_cli --state-dir "$HOME\.surge" init
   python -m surge.jobs.runner_cli --state-dir "$HOME\.surge" dry-run --job jp_eod
   ```

   `dry-run` prints where the log and the heartbeat will be, whether a lock is
   held, and what it would run. It takes no lock and writes nothing.

3. **Register the tasks.**

   ```
   .\Install-SurgeTasks.ps1
   ```

4. **Confirm.**

   ```
   .\Get-SurgeTaskStatus.ps1
   ```

   This prints both views. Task Scheduler's `LastResult 0` means python exited
   cleanly, which it does after recording a failure; the runner's own `status`
   is what says whether anything succeeded.

5. **To remove them**, `.\Uninstall-SurgeTasks.ps1`. It never touches the state
   directory: the run log is the record of what did and did not happen, and
   unbinding a scheduler is not a reason to delete it.

## Where this stops

Registering the task is yours to run - it changes your machine's configuration,
so it is not something to do on your behalf. Everything up to it is written and
testable without your involvement, which is why steps 1 and 2 exist.

But it is not yours to run *yet*. See the note above: an empty pipeline on a
schedule is worse than no schedule.

## The one thing to know about `StartWhenAvailable`

It is on. This is a laptop, and the whole reason the runner records
`RUNTIME_OFFLINE` is that the machine is sometimes off at 16:30. With
`StartWhenAvailable`, a missed occurrence is attempted when the machine next
wakes, and `runner_cli gaps` records the ones that were genuinely never
attempted. Two mechanisms, and they answer different questions: one recovers a
late run, the other makes an unrecoverable one visible.

`gaps` refuses to run before `init` has been called, and never counts from
earlier than the install marker. Otherwise a fresh machine's first gap check
would write a month of `RUNTIME_OFFLINE` rows for days on which this system did
not exist - and afterwards those are indistinguishable from real outages.
