<#
.SYNOPSIS
    Install the copier on a fresh Windows VPS from the package zip.

.DESCRIPTION
    Creates the state directories, builds the virtual environment, installs
    the pinned dependencies, and checks what it cannot create for you.

    It does NOT write secrets and it does NOT start anything. Secrets are
    placed by hand, once, and starting before the terminals are up means a
    burst of failed connections.

    Run from the unzipped package directory:
        .\Install-Copier.ps1

.NOTES
    Windows PowerShell 5.1. No ternary, no ??, no && chaining.
#>
[CmdletBinding()]
param(
    [string] $StateRoot = 'C:\GTC',
    [string] $PackageRoot,
    [string] $Python
)

$ErrorActionPreference = 'Stop'

if (-not $PackageRoot) {
    $here = $PSScriptRoot
    if (-not $here) { $here = Split-Path -Parent $MyInvocation.MyCommand.Path }
    $PackageRoot = $here
}
$hub = Join-Path $PackageRoot 'hub'
if (-not (Test-Path (Join-Path $hub 'run.py'))) {
    throw "No hub\run.py under $PackageRoot -- run this from the unzipped package."
}

# --------------------------------------------------------- an interpreter
if (-not $Python) {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if (-not $cmd) {
        throw 'No python on PATH. Install Python 3.11 and re-run, or pass -Python.'
    }
    $Python = $cmd.Source
}
$version = & $Python -c "import sys; print('%d.%d' % sys.version_info[:2])"
Write-Host ("using {0} (Python {1})" -f $Python, $version)
if ($version -ne '3.11') {
    # Not fatal: the pins are known good on 3.11 and untested elsewhere,
    # and silently running a different one is how a dependency behaves
    # differently here than in CI.
    Write-Warning ("the dependency pins are tested on Python 3.11, not {0}" -f $version)
}

# ------------------------------------------------------------ state dirs
foreach ($d in @('data', 'logs', 'config')) {
    $path = Join-Path $StateRoot $d
    if (-not (Test-Path $path)) {
        New-Item -ItemType Directory -Path $path -Force | Out-Null
        Write-Host ("created {0}" -f $path)
    } else {
        Write-Host ("exists  {0}" -f $path)
    }
}

# A state directory inside a synced folder is the failure this layout
# exists to avoid, so it is worth refusing to be quiet about.
if ($StateRoot -match 'OneDrive|Dropbox|Google Drive') {
    Write-Warning ("{0} looks like a synced folder. The registry is written " +
                   "transactionally and a sync service copying it mid-write " +
                   "can corrupt it." -f $StateRoot)
}

# ----------------------------------------------------------- environment
$venv = Join-Path $hub '.venv'
if (Test-Path (Join-Path $venv 'Scripts\python.exe')) {
    Write-Host 'virtual environment already present'
} else {
    Write-Host 'creating the virtual environment'
    & $Python -m venv $venv
}
$venvPy = Join-Path $venv 'Scripts\python.exe'

Write-Host 'installing pinned dependencies'
& $venvPy -m pip install --upgrade pip --quiet
$constraints = Join-Path $hub 'constraints.txt'
if (Test-Path $constraints) {
    & $venvPy -m pip install -r (Join-Path $hub 'requirements.txt') -c $constraints --quiet
} else {
    & $venvPy -m pip install -r (Join-Path $hub 'requirements.txt') --quiet
}

# ------------------------------------------------- what it cannot create
$missing = @()
if (-not (Test-Path (Join-Path $hub '.env'))) {
    $missing += 'hub\.env  -- copy hub\.env.example and add the Flex token'
}
if (-not (Test-Path (Join-Path $hub 'config\hub_reporter.json'))) {
    $missing += 'hub\config\hub_reporter.json  -- copy the .example, set url, set enabled true'
}
if (-not (Test-Path (Join-Path $StateRoot 'config\hub_key.txt'))) {
    $missing += ("{0}\config\hub_key.txt  -- the hub's API key, one line" -f $StateRoot)
}

Write-Host ''
if ($missing.Count -eq 0) {
    Write-Host 'everything is in place.' -ForegroundColor Green
} else {
    Write-Host 'Still to do by hand, because none of it belongs in a package:' -ForegroundColor Yellow
    foreach ($m in $missing) { Write-Host ("  - {0}" -f $m) }
}

Write-Host ''
Write-Host 'Then, in this order:'
Write-Host '  1. Start MT4/MT5 and IB Gateway, and let them finish connecting.'
Write-Host '  2. .\restart-copier.ps1                 (check it comes up)'
Write-Host '  3. .\Register-CopierTask.ps1            (elevated, once)'
Write-Host ''
Write-Host 'A reporter with no key logs "off" once and is then silent, so a'
Write-Host 'copier that trades correctly and reports nothing is a plausible'
Write-Host 'result of skipping hub_key.txt. The hub would raise "copier silent".'
