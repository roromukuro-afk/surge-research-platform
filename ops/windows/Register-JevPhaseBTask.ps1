<#
.SYNOPSIS
    Register one of the Jev evaluation's Phase B jobs with Task Scheduler: the
    prediction job (D-277) or the outcome job (D-278).

.DESCRIPTION
    Weekdays at 16:10 (-Job Prediction) or 18:00 (-Job Outcome) local time -
    this machine must be on Tokyo time - for the current user at the ordinary
    run level, running Invoke-JevPhaseB.ps1 from the frozen worktree, which
    must be exactly at -ExpectCommit, with no changes, or nothing is registered.

    Settings, and why:
      * WakeToRun: a machine asleep while its user is logged on is woken for
        the run (Windows' "allow wake timers" power setting still decides).
      * Run only when the user is logged on: no Windows credential is stored.
      * StartWhenAvailable: a start missed while the machine was off happens
        when it can. The prediction job sends only inside the day's window
        (16:10 JST until 09:00 JST on the next weekday) and ends without
        sending outside it; the outcome job has no deadline.
      * MultipleInstances IgnoreNew: Task Scheduler does not start a second
        instance while one runs. Both jobs hold the cohort's own lock as well,
        so they never write at once.
      * Allowed on battery, not stopped when the machine goes on battery: a
        prediction run stopped halfway is not resumed (the day is lost).
      * 3-hour limit, no restart: a prediction day takes about 45 minutes; a
        hung run is ended, and an interrupted day is not resumed.
      * Holidays are not the trigger's business: each job reads JPX's calendar
        and ends normally on a closed day.

.PARAMETER DryRun
    Print what would be registered; register nothing.

.EXAMPLE
    .\Register-JevPhaseBTask.ps1 -CohortId jev-phase-b-jp-20260924-v1 -ExpectCommit <sha> -DryRun
    .\Register-JevPhaseBTask.ps1 -Job Outcome -CohortId jev-phase-b-jp-20260924-v1 -ExpectCommit <sha>
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)] [string] $CohortId,
    [Parameter(Mandatory = $true)] [string] $ExpectCommit,
    [ValidateSet("Prediction", "Outcome")] [string] $Job = "Prediction",
    [string] $Worktree = "",
    [string] $PythonExe = "",
    [string] $EvaluationRoot = (Join-Path $HOME ".surge"),
    [string] $TaskFolder = "\Surge",
    [string] $At = "",
    [switch] $Force,
    [switch] $DryRun
)

$ErrorActionPreference = "Stop"

# Windows PowerShell 5.1 has no $PSScriptRoot in param() defaults.
if (-not $Worktree) { $Worktree = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path }
if (-not $PythonExe) { $PythonExe = (Get-Command python -ErrorAction Stop).Source }
if (-not $At) { $At = if ($Job -eq "Prediction") { "16:10" } else { "18:00" } }
$taskName = if ($Job -eq "Prediction") { "surge-jev-phase-b-$CohortId" } else { "surge-jev-phase-b-outcome-$CohortId" }
$script = Join-Path $Worktree "ops\windows\Invoke-JevPhaseB.ps1"
$workersSrc = Join-Path $Worktree "workers\src"

if ((Get-TimeZone).Id -ne "Tokyo Standard Time") {
    throw "this machine is on $((Get-TimeZone).Id): a trigger at $At would not be $At JST"
}
if (-not (Test-Path $script)) { throw "no Invoke-JevPhaseB.ps1 in $Worktree" }
if (-not (Test-Path $PythonExe)) { throw "no python at $PythonExe" }
$head = (& git -C $Worktree rev-parse HEAD).Trim()
if ($head -ne $ExpectCommit) { throw "$Worktree is at $head, not at $ExpectCommit" }
$dirty = & git -C $Worktree status --porcelain --untracked-files=no
if ($dirty) { throw "$Worktree has changes; the task runs a commit exactly" }

$arguments = "-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$script`" " +
             "-Job $Job -CohortId $CohortId -ExpectCommit $ExpectCommit -RepoRoot `"$Worktree`" " +
             "-PythonExe `"$PythonExe`" -EvaluationRoot `"$EvaluationRoot`""
$user = "$env:USERDOMAIN\$env:USERNAME"

Write-Host "task          : $TaskFolder\$taskName ($Job job)"
Write-Host "when          : Monday-Friday $At (Tokyo); wakes the machine; started when available if missed"
Write-Host "runs as       : $user, only when logged on, ordinary run level"
Write-Host "command       : powershell.exe $arguments"
Write-Host "working dir   : $workersSrc"
Write-Host "worktree      : $Worktree at $head (clean)"
Write-Host "python        : $PythonExe"
if ($Job -eq "Prediction") {
    Write-Host "credentials   : TYPESAFE_API_KEY only, decrypted for the run (DPAPI, $env:USERNAME); the wrapper"
    Write-Host "                finds its file when it runs (Set-SurgeSecret's folder, else the Claude app's package"
    Write-Host "                cache) and logs the path, never the value"
} else {
    Write-Host "credentials   : none; the outcome job never calls Jev"
}
Write-Host "logs          : $EvaluationRoot\evaluation\jev\$CohortId\scheduler\ (console\ and runs\)"
Write-Host "instances     : IgnoreNew, plus the cohort's lock shared by both jobs; no restart; 3-hour limit; battery ok"

if ($DryRun) {
    Write-Host ""
    Write-Host "nothing was registered."
    exit 0
}

if ((Get-ScheduledTask -TaskPath "$TaskFolder\" -TaskName $taskName -ErrorAction SilentlyContinue) -and -not $Force) {
    throw "$TaskFolder\$taskName is already registered; pass -Force to replace it"
}

$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $arguments -WorkingDirectory $workersSrc
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At $At
$settings = New-ScheduledTaskSettingsSet `
    -WakeToRun `
    -StartWhenAvailable `
    -DontStopOnIdleEnd `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 3) `
    -MultipleInstances IgnoreNew `
    -RestartCount 0
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited

Register-ScheduledTask `
    -TaskName $taskName `
    -TaskPath $TaskFolder `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "Jev evaluation Phase B $Job job ($CohortId). Managed by ops/windows/Register-JevPhaseBTask.ps1." `
    -Force:$Force | Out-Null

$task = Get-ScheduledTask -TaskPath "$TaskFolder\" -TaskName $taskName
$info = $task | Get-ScheduledTaskInfo
Write-Host ""
Write-Host "registered. next run: $($info.NextRunTime); wake to run: $($task.Settings.WakeToRun)"
Write-Host "remove with: Unregister-ScheduledTask -TaskPath '$TaskFolder\' -TaskName '$taskName'"
