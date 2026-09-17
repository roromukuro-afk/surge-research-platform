<#
.SYNOPSIS
    What Task Scheduler thinks, and what the runner's own log says.

.DESCRIPTION
    Both halves, because they disagree in the case that matters. Task Scheduler
    reports LastTaskResult 0 when python exited cleanly, which it does after
    writing a FAILED attempt to the log. Reading only the scheduler would show a
    week of healthy runs that all failed.

    The runner's own view comes from `runner_cli status`, which reports the last
    success per job, the gaps since, and whether the heartbeat is stale.

.EXAMPLE
    .\Get-SurgeTaskStatus.ps1
#>

[CmdletBinding()]
param(
    [string] $RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path,
    [string] $StateDir = (Join-Path $HOME ".surge"),
    [string] $PythonExe = "python",
    [string] $TaskFolder = "\Surge"
)

$ErrorActionPreference = "Stop"

Write-Host "=== Task Scheduler ==="
$tasks = Get-ScheduledTask -TaskPath "$TaskFolder\" -ErrorAction SilentlyContinue |
         Where-Object { $_.TaskName -like "surge-*" }

if (-not $tasks) {
    Write-Host "no surge tasks are registered. Nothing will run on a schedule."
} else {
    $tasks | ForEach-Object {
        $info = $_ | Get-ScheduledTaskInfo
        [pscustomobject]@{
            Task       = $_.TaskName
            State      = $_.State
            LastRun    = $info.LastRunTime
            LastResult = $info.LastTaskResult
            NextRun    = $info.NextRunTime
        }
    } | Format-Table -AutoSize
    Write-Host "LastResult 0 means python exited cleanly. It does NOT mean the job succeeded -"
    Write-Host "a failed job still exits cleanly after writing why. The runner's view is below."
}

Write-Host ""
Write-Host "=== The runner ==="
$workersSrc = Join-Path $RepoRoot "workers\src"
Push-Location $workersSrc
try {
    & $PythonExe -m surge.jobs.runner_cli --state-dir $StateDir status
} finally {
    Pop-Location
}

Write-Host ""
Write-Host "=== Heartbeat ==="
Push-Location $workersSrc
try {
    & $PythonExe -m surge.jobs.runner_cli --state-dir $StateDir heartbeat
    if ($LASTEXITCODE -ne 0) {
        Write-Host "stale or absent. That is expected when no job is running right now;"
        Write-Host "it is a finding only while a job is supposed to be in flight."
    }
} finally {
    Pop-Location
}
