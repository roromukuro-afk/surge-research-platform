<#
.SYNOPSIS
    Bind the daily jobs to Windows Task Scheduler.

.DESCRIPTION
    Task Scheduler is the invoker and nothing more. The lock, the heartbeat, the
    retry policy and the record of days the machine was off all live in
    `surge.jobs.runner_cli`, so moving to cron or a cloud runner later replaces
    this file and leaves the behaviour alone.

    Two choices here are deliberate.

    The tasks are registered for the CURRENT USER at the ordinary run level, so
    no elevation is needed and the jobs run with exactly the permissions the
    operator has. A task registered as SYSTEM would run as an account that has
    no .env.local, and would fail at the first credential.

    `-StartWhenAvailable` is set. This machine is a laptop: the whole reason the
    runner records RUNTIME_OFFLINE is that it is sometimes off at 16:30. With
    this, a missed occurrence is attempted when the machine wakes, and the gap
    detector records the ones that were genuinely never attempted.

.PARAMETER DryRun
    Print the task definitions and register nothing.

.EXAMPLE
    .\Install-SurgeTasks.ps1 -DryRun
    .\Install-SurgeTasks.ps1
#>

[CmdletBinding()]
param(
    [string] $RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path,
    [string] $StateDir = (Join-Path $HOME ".surge"),
    [string] $PythonExe = "python",
    [string] $TaskFolder = "\Surge",
    [switch] $DryRun
)

$ErrorActionPreference = "Stop"

# job name -> local time of day. These mirror SCHEDULES in runner_cli.py; the
# CLI is the source of truth for what a job is, this is only when to start it.
$Jobs = [ordered]@{
    "jp_eod"       = "16:30"
    "jp_materials" = "19:00"
    "us_eod"       = "07:30"
    "readiness"    = "08:00"
}

$WorkersSrc = Join-Path $RepoRoot "workers\src"
if (-not (Test-Path $WorkersSrc)) {
    throw "workers\src not found under '$RepoRoot'. Pass -RepoRoot with the path to the repository."
}

Write-Host "repository : $RepoRoot"
Write-Host "state dir  : $StateDir"
Write-Host "python     : $PythonExe"
Write-Host ""

# The marker the gap detector measures from. Without it, the first run of the
# detector would report every scheduled moment in its lookback window as an
# outage and write RUNTIME_OFFLINE rows for days this machine was not set up.
if ($DryRun) {
    Write-Host "would run: $PythonExe -m surge.jobs.runner_cli --state-dir `"$StateDir`" init"
} else {
    $env:PYTHONPATH = $WorkersSrc
    & $PythonExe -m surge.jobs.runner_cli --state-dir $StateDir init
    if ($LASTEXITCODE -ne 0) { throw "could not initialise the state directory" }
}

foreach ($job in $Jobs.Keys) {
    $taskName = "surge-$job"
    $at = $Jobs[$job]

    $action = New-ScheduledTaskAction `
        -Execute $PythonExe `
        -Argument "-m surge.jobs.runner_cli --state-dir `"$StateDir`" run --job $job" `
        -WorkingDirectory $WorkersSrc

    $trigger = New-ScheduledTaskTrigger -Daily -At $at

    $settings = New-ScheduledTaskSettingsSet `
        -StartWhenAvailable `
        -DontStopOnIdleEnd `
        -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
        -MultipleInstances IgnoreNew `
        -RestartCount 0

    if ($DryRun) {
        Write-Host "would register $TaskFolder\$taskName"
        Write-Host "    at            : $at daily, started when available if missed"
        Write-Host "    command       : $PythonExe -m surge.jobs.runner_cli --state-dir `"$StateDir`" run --job $job"
        Write-Host "    working dir   : $WorkersSrc"
        Write-Host "    instances     : IgnoreNew (the CLI holds its own lock as well)"
        Write-Host ""
        continue
    }

    Register-ScheduledTask `
        -TaskName $taskName `
        -TaskPath $TaskFolder `
        -Action $action `
        -Trigger $trigger `
        -Settings $settings `
        -Description "Surge research platform: $job. Managed by ops/windows/Install-SurgeTasks.ps1." `
        -Force | Out-Null

    Write-Host "registered $TaskFolder\$taskName ($at daily)"
}

Write-Host ""
if ($DryRun) {
    Write-Host "nothing was registered and nothing was written."
} else {
    Write-Host "done. Check with: .\Get-SurgeTaskStatus.ps1"
    Write-Host ""
    Write-Host "Each task starts python with its working directory set to workers\src, which is"
    Write-Host "what puts the surge package on the import path for 'python -m'. If you move the"
    Write-Host "repository, re-run this script: the path is baked into the registered task."
}
