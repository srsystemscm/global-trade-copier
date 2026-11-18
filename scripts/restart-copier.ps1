<#
.SYNOPSIS
    Stop the copier if it is running, start it again, and wait until it
    answers.

.DESCRIPTION
    Written for Windows PowerShell 5.1, which is what a Windows VPS has:
    no ternary, no ?? and no && chaining.

    Stopping is done by PORT rather than by process name. Several
    python.exe processes can be running -- the UI's dev server, a test
    run, another project entirely -- and killing by name would take them
    with it. Whatever holds the hub's HTTP port IS the copier.

    The copier is started DETACHED (Win32_Process.Create), so it does not
    die with the window that restarted it. It used to: see the comment on
    the start block, and the two-hour outage that wrote it.

    "Started" means the health endpoint answered, not that the process
    exists. The hub binds its ZMQ ports before its HTTP one and can sit
    in startup for up to a minute waiting on a shut terminal, so a
    process check would report success while nothing was listening yet.

.EXAMPLE
    .\restart-copier.ps1
    .\restart-copier.ps1 -Port 8010
#>
[CmdletBinding()]
param(
    [int]    $Port     = 8010,
    [string] $HubRoot,
    [string] $LogDir   = 'C:\GTC\logs',
    [int]    $WaitSeconds = 120,
    # Set automatically when this script relaunches itself elevated. It
    # exists only to stop an elevation loop: if the elevated run ALSO
    # cannot stop the process, something other than privilege is wrong
    # and asking for UAC again would just hide it.
    [switch] $NoElevate
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

# ---------------------------------------------------------------- stop
$listening = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($listening) {
    # NOT $pid: that is a PowerShell automatic variable and assigning it
    # in a foreach fails outright on 5.1.
    foreach ($procId in ($listening.OwningProcess | Sort-Object -Unique)) {
        $existing = Get-Process -Id $procId -ErrorAction SilentlyContinue
        if ($existing) {
            Write-Host ("stopping pid {0} ({1}) on port {2}" -f $procId, $existing.ProcessName, $Port)
            try {
                Stop-Process -Id $procId -Force -ErrorAction Stop
            } catch {
                # Almost always because the copier was started from an
                # ELEVATED prompt and this one is not.
                #
                # It used to stop here and tell you to re-run elevated,
                # which sounds simple and is not: every hand-written
                # elevation command has to quote this script's path, and
                # Start-Process -ArgumentList in Windows PowerShell 5.1
                # does NOT quote an element containing spaces. The real
                # path has three of them, so the elevated shell received
                # "-File C:\Users\...\Projects\Global" and died on a path
                # that does not exist -- looking exactly like a restart
                # that silently did nothing. It cost a three-hour outage
                # with two IBKR slaves down, because the copier LOOKED up
                # the whole time: the old process was still answering.
                #
                # So it elevates itself, and does its own quoting.
                Write-Warning ("cannot stop pid {0}: {1}" -f $procId, $_.Exception.Message)

                if ($NoElevate) {
                    Write-Warning 'Already elevated and still refused -- this is not a privilege problem.'
                    exit 1
                }

                $me = $PSCommandPath
                if (-not $me) { $me = $MyInvocation.MyCommand.Path }
                Write-Host 'The copier is running elevated. Asking for elevation (expect a UAC prompt)...'

                # Each argument quoted individually. -File's path needs it
                # because of the spaces; the rest are quoted for symmetry
                # so that changing one later cannot silently reintroduce
                # the same bug.
                $quoted = @(
                    '-NoProfile'
                    '-ExecutionPolicy'
                    'Bypass'
                    '-NoExit'
                    '-File'
                    ('"{0}"' -f $me)
                    '-Port'
                    $Port
                    '-NoElevate'
                )
                try {
                    Start-Process -FilePath 'powershell.exe' -Verb RunAs -ArgumentList $quoted
                    Write-Host 'Elevated window launched -- watch THAT window for the result.'
                    # 3, NOT 0. Handing off is not success: nothing has been
                    # restarted yet, and the caller cannot see the other
                    # window's result. Exiting 0 here had the launcher print
                    # "the copier answered on /health" over a copier that had
                    # not been touched.
                    exit 3
                } catch {
                    Write-Warning ("elevation was refused or cancelled: {0}" -f $_.Exception.Message)
                    exit 1
                }
            }
        }
    }
    # Give the socket time to close, or the new process races the old one
    # for the port and dies with "address already in use".
    $deadline = (Get-Date).AddSeconds(20)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 500
        $still = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
        if (-not $still) { break }
    }
} else {
    Write-Host ("nothing was listening on port {0}" -f $Port)
}

# --------------------------------------------------------------- start
$stdout = Join-Path $LogDir 'console.out'
$stderr = Join-Path $LogDir 'console.err'

# Rolled rather than appended: these are the only files the app does not
# rotate itself, because the shell writes them and not the logger.
foreach ($f in @($stdout, $stderr)) {
    if ((Test-Path $f) -and ((Get-Item $f).Length -gt 5MB)) {
        Move-Item $f ($f + '.1') -Force
    }
}

Write-Host ("starting {0} in {1}" -f (Split-Path $python -Leaf), $HubRoot)

# STARTED DETACHED, THROUGH WMI, AND NOT WITH Start-Process.
#
# Start-Process with -RedirectStandardOutput forces UseShellExecute=false,
# and a process started that way INHERITS THE LAUNCHING CONSOLE. Closing
# that window sends every attached process a close event -- so the copier
# lived exactly as long as the window someone had used to restart it.
#
# On 2026-11-18 that window was an ELEVATED one, opened by this script's
# own UAC path. The copier started at 11:20, answered /health, reconciled
# all four slaves, and died at 11:53:52 the moment the window was tidied
# away: no traceback in console.err, no shutdown line in hub.log, just a
# stop between two routine log lines. It was down for nearly two hours
# with eight IBKR positions open, and nothing said so -- the ops hub saw
# the reporter go silent and that was the only signal.
#
# Win32_Process.Create is started by the WMI provider service rather than
# by this shell, so the new process has NO console of ours to lose. The
# cmd wrapper is only there to keep the redirections: Create takes a
# command line and no stream handles.
$commandLine = ('cmd.exe /c ""{0}" run.py >> "{1}" 2>> "{2}""' -f $python, $stdout, $stderr)
$created = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
    CommandLine      = $commandLine
    CurrentDirectory = $HubRoot
}
if ($created.ReturnValue -ne 0) {
    throw ("could not start the copier: Win32_Process.Create returned {0}" -f $created.ReturnValue)
}
$startedPid = [int]$created.ProcessId
Write-Host ("started detached: pid {0} (cmd), python is its child" -f $startedPid)

# --------------------------------------------------------------- verify
$health = "http://127.0.0.1:$Port/health"
$deadline = (Get-Date).AddSeconds($WaitSeconds)
$ok = $false
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 2
    # The wrapper lives exactly as long as the hub does, so its absence is
    # the hub having exited -- most often the ZMQ ports still held by a
    # copier the stop above failed to kill.
    if (-not (Get-Process -Id $startedPid -ErrorAction SilentlyContinue)) {
        Write-Warning ("the copier exited immediately. Last lines of {0}:" -f $stderr)
        if (Test-Path $stderr) { Get-Content $stderr -Tail 15 }
        exit 1
    }
    try {
        $r = Invoke-RestMethod -Uri $health -TimeoutSec 5
        if ($r.status -eq 'ok') { $ok = $true; break }
    } catch {
        # Still starting: the HTTP port is bound last, after each slave
        # has been reconciled. A shut terminal costs a timeout here.
    }
}

if ($ok) {
    # /health answering is not proof that OUR process is the one answering.
    # If the stop silently failed, the old copier is still on the port and
    # would satisfy the check perfectly while the new one died unable to
    # bind -- a green light for a restart that never happened.
    $holder = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    $holderPid = $null
    if ($holder) { $holderPid = ($holder.OwningProcess | Select-Object -First 1) }

    # A CHILD counts, and there are now two generations of them: cmd.exe
    # wraps the venv's python.exe, which is itself a redirector that
    # re-executes the base interpreter -- and THAT is what binds the
    # ports. Walking up from the holder is the only check that survives
    # both.
    $ours = $false
    if ($holderPid -eq $startedPid) {
        $ours = $true
    } elseif ($holderPid) {
        $walk = $holderPid
        for ($i = 0; $i -lt 6 -and -not $ours; $i++) {
            $info = Get-CimInstance Win32_Process -Filter ("ProcessId=" + $walk) -ErrorAction SilentlyContinue
            if (-not $info) { break }
            if ($info.ParentProcessId -eq $startedPid) { $ours = $true; break }
            $walk = $info.ParentProcessId
        }
    }

    if (-not $ours) {
        Write-Warning ("port {0} is held by pid {1}, which is not the process just started (pid {2}) nor a child of it." -f $Port, $holderPid, $startedPid)
        Write-Warning 'Something else is answering. Nothing was restarted.'
        exit 1
    }

    Write-Host ("copier is up: pid {0} holds port {1}, {2}" -f $holderPid, $Port, $health) -ForegroundColor Green
    Write-Host 'It is detached -- closing this window will not stop it.' -ForegroundColor Green
    exit 0
}

Write-Warning ("no answer from {0} within {1}s. The process is pid {2}; check {3}" -f $health, $WaitSeconds, $startedPid, (Join-Path $LogDir 'hub.log'))
exit 1
