<#
.SYNOPSIS
    Remove the scheduled tasks. Leaves the run log and the heartbeat alone.

.DESCRIPTION
    Unregistering a task stops the pipeline being invoked. It does not, and must
    not, delete the state directory: the run log is the record of what did and
    did not happen, including the days the machine was off, and removing a
    scheduler binding is not a reason to destroy that.

    So this script never touches $StateDir. If the state really is to be thrown
    away, that is a separate, deliberate act by a person who has looked at it.

.EXAMPLE
    .\Uninstall-SurgeTasks.ps1 -DryRun
    .\Uninstall-SurgeTasks.ps1
#>

[CmdletBinding()]
param(
    [string] $TaskFolder = "\Surge",
    [switch] $DryRun
)

$ErrorActionPreference = "Stop"

$tasks = Get-ScheduledTask -TaskPath "$TaskFolder\" -ErrorAction SilentlyContinue |
         Where-Object { $_.TaskName -like "surge-*" }

if (-not $tasks) {
    Write-Host "no surge tasks registered under $TaskFolder."
    return
}

foreach ($task in $tasks) {
    if ($DryRun) {
        Write-Host "would unregister $($task.TaskPath)$($task.TaskName)"
        continue
    }
    Unregister-ScheduledTask -TaskName $task.TaskName -TaskPath $task.TaskPath -Confirm:$false
    Write-Host "unregistered $($task.TaskPath)$($task.TaskName)"
}

Write-Host ""
Write-Host "The state directory was not touched. The run log is the record of what ran and what"
Write-Host "did not, and unbinding a scheduler is not a reason to delete it."
