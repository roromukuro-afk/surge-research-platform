<#
.SYNOPSIS
    One scheduled run of the Jev evaluation's Phase B (D-277): what Task Scheduler
    starts every weekday at 16:10 JST.

.DESCRIPTION
    Runs `python -m surge.jobs.jev_eval phase-b-scheduled` from the frozen
    worktree this script sits in. Of the credentials Set-SurgeSecret.ps1 keeps,
    only TYPESAFE_API_KEY is decrypted (DPAPI, this Windows user) into this
    process's environment - no other one - and it is removed again at the end.
    It is never written or printed.

    The job decides everything else: whether the frozen worktree is at the
    registered commit, the cohort's lock (a second run sends nothing), the JPX
    business day, the send window, the cohort's stops and caps, and whether
    anything is sent at all.

    Output: <EvaluationRoot>\evaluation\jev\<CohortId>\scheduler\console\
    <UTC stamp>.out.log and .err.log. The job's own record of every run is in
    ...\scheduler\runs\.

    Exit code: the job's - 0 nothing to do or the day was sent, 2 a guard
    stopped it, 1 an error - or 3 when this script could not start it (no key
    stored, no python, not a worktree of this repository).

.EXAMPLE
    .\Invoke-JevPhaseB.ps1 -CohortId jev-phase-b-jp-20260924-v1 -ExpectCommit <sha>
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)] [string] $CohortId,
    [Parameter(Mandatory = $true)] [string] $ExpectCommit,
    [string] $RepoRoot = "",
    [string] $PythonExe = "python",
    [string] $EvaluationRoot = (Join-Path $HOME ".surge")
)

$ErrorActionPreference = "Stop"

# Windows PowerShell 5.1, which Task Scheduler starts, has no $PSScriptRoot in param() defaults.
if (-not $RepoRoot) { $RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path }
$workersSrc = Join-Path $RepoRoot "workers\src"
$secret = Join-Path $env:LOCALAPPDATA "surge\secrets\TYPESAFE_API_KEY.dpapi"
$logDir = Join-Path $EvaluationRoot "evaluation\jev\$CohortId\scheduler\console"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$stamp = (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ")
$out = Join-Path $logDir "$stamp.out.log"
$err = Join-Path $logDir "$stamp.err.log"

function Stop-Here([string] $message) {
    Add-Content -Path $err -Value $message -Encoding UTF8
    exit 3
}

if (-not (Test-Path (Join-Path $workersSrc "surge\jobs\jev_eval.py"))) {
    Stop-Here "no surge\jobs\jev_eval.py under $workersSrc"
}
if (-not (Test-Path $secret)) {
    Stop-Here "TYPESAFE_API_KEY is not stored for $env:USERNAME (ops\windows\Set-SurgeSecret.ps1)"
}

$code = 3
try {
    try {
        $secure = Get-Content -Path $secret -Raw | ConvertTo-SecureString
    } catch {
        Stop-Here "TYPESAFE_API_KEY could not be decrypted: it was stored by another Windows user or machine"
    }
    $env:TYPESAFE_API_KEY = [System.Net.NetworkCredential]::new("", $secure).Password
    $secure = $null
    $env:PYTHONPATH = $workersSrc
    $env:PYTHONIOENCODING = "utf-8"
    $arguments = @(
        "-m", "surge.jobs.jev_eval", "--root", "`"$EvaluationRoot`"",
        "phase-b-scheduled", "--cohort-id", $CohortId,
        "--expect-repo", "`"$RepoRoot`"", "--expect-commit", $ExpectCommit
    )
    $process = Start-Process -FilePath $PythonExe -ArgumentList $arguments -WorkingDirectory $workersSrc `
        -NoNewWindow -Wait -PassThru -RedirectStandardOutput $out -RedirectStandardError $err
    $code = $process.ExitCode
}
finally {
    Remove-Item Env:\TYPESAFE_API_KEY -ErrorAction SilentlyContinue
}
exit $code
