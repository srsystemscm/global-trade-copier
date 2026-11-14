<#
.SYNOPSIS
    Register the copier as a Scheduled Task that starts at logon and
    restarts itself if it stops.

.DESCRIPTION
    A Scheduled Task rather than a Windows service, deliberately.

    The copier talks to MT4/MT5 terminals and IB Gateway over localhost,
    and those are GUI applications that need an interactive desktop
    session. A service running as LocalSystem has no such session, so the
    terminals would have to run in a user session anyway -- and then the
    copier would be the only piece somewhere else, started before the
    things it connects to. At logon, in the same session, everything comes
    up together and in the right order.

    The practical setup on a trading VPS is auto-logon plus this task.
    That means the session must stay logged in: disconnect from RDP, never
    sign out. Signing out closes the terminals and the copier with them.

    Run this once, elevated:
        .\Register-CopierTask.ps1

    Then check it:
        Get-ScheduledTask GlobalTradeCopier | Get-ScheduledTaskInfo

.NOTES
    Windows PowerShell 5.1. No ternary, no ??, no && chaining.
#>
[CmdletBinding()]
param(
    [string] $TaskName = 'GlobalTradeCopier',
    [string] $HubRoot,
    [string] $LogDir   = 'C:\GTC\logs',
    [int]    $StartDelaySeconds = 60
)

$ErrorActionPreference = 'Stop'

# $PSScriptRoot is NOT populated while parameter defaults are being bound
# when the script is launched with `powershell -File`, so the default could
# not be computed there. Resolved here instead, where both invocation
# styles have it, with $MyInvocation as the fallback.
if (-not $HubRoot) {
    $here = $PSScriptRoot
    if (-not $here) { $here = Split-Path -Parent $MyInvocation.MyCommand.Path }
    $HubRoot = Join-Path (Split-Path -Parent $here) 'hub'
}

$python = Join-Path $HubRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) {
    throw "No interpreter at $python -- is this the right -HubRoot?"
}
if (-not (Test-Path $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
}

# The copier connects to the terminals and to IB Gateway on localhost. If
# it starts first every slave fails its first connect, reconciliation runs
# against nothing, and the hub gets a burst of alerts that mean only "I was
# early". A delay is cruder than waiting for each port, and it is the thing
# Task Scheduler can actually express.
$trigger = New-ScheduledTaskTrigger -AtLogOn
$trigger.Delay = ('PT{0}S' -f $StartDelaySeconds)

$action = New-ScheduledTaskAction `
    -Execute $python `
    -Argument 'run.py' `
    -WorkingDirectory $HubRoot

# Interactive, in the logged-on user's own session: see the note above.
$principal = New-ScheduledTaskPrincipal `
    -UserId (whoami) `
    -LogonType Interactive `
    -RunLevel Limited

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -RestartCount 999 `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew

# ExecutionTimeLimit zero means "no limit". The default is 3 days, after
# which Task Scheduler would kill a perfectly healthy copier.
# MultipleInstances IgnoreNew stops a restart racing a running copy for
# the HTTP port.

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Write-Host ("replacing the existing '{0}' task" -f $TaskName)
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

Register-ScheduledTask `
    -TaskName $TaskName `
    -Trigger $trigger `
    -Action $action `
    -Principal $principal `
    -Settings $settings `
    -Description 'Global Trade Copier. Starts at logon, after the terminals and IB Gateway.' | Out-Null

Write-Host ("registered '{0}'" -f $TaskName) -ForegroundColor Green
Write-Host ''
Write-Host 'Start it now without waiting for a logon:'
Write-Host ("    Start-ScheduledTask -TaskName {0}" -f $TaskName)
Write-Host ''
Write-Host 'What it does NOT do, so this is not a surprise later:'
Write-Host '  - Output goes to the Task Scheduler history, not to console.out.'
Write-Host '    The app logs to C:\GTC\logs\hub.log either way, which rotates.'
Write-Host '  - It restarts on a CRASH. A copier that is running but has'
Write-Host '    stopped copying still looks healthy to Task Scheduler --'
Write-Host '    that is what the health check and the hub alert are for.'
Write-Host '  - Auto-logon must be enabled and the session must stay signed'
Write-Host '    in. Disconnect from RDP; never sign out.'
