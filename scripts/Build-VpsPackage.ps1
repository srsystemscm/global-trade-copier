<#
.SYNOPSIS
    Build a zip that installs the copier on a fresh Windows VPS.

.DESCRIPTION
    Everything needed to stand the copier up, and nothing that must not
    leave this machine.

    DELIBERATELY EXCLUDED, and each for its own reason:

      .env              the Flex token. Shipped as .env.example instead.
      config/hub_key.txt the Operations hub's API key.
      config/hub_reporter.json  carries the live hub URL and enabled=true.
                        Shipped as a template with enabled=false.
      config/slaves.json  may name real accounts.
      data/             the registry. Copied separately and deliberately,
                        because overwriting a VPS's live registry with a
                        stale one would lose every open position it knows.
      .venv/            rebuilt on the target; a venv is not portable.
      logs/, __pycache__/

    Secrets are placed on the VPS by hand, once. Nothing here writes them,
    and nothing here carries them.

.EXAMPLE
    .\Build-VpsPackage.ps1
    .\Build-VpsPackage.ps1 -OutDir D:\builds
#>
[CmdletBinding()]
param(
    [string] $OutDir,
    [string] $RepoRoot
)

$ErrorActionPreference = 'Stop'

# $PSScriptRoot is not populated while parameter defaults are bound under
# `powershell -File`, so these are resolved here.
if (-not $RepoRoot) {
    $here = $PSScriptRoot
    if (-not $here) { $here = Split-Path -Parent $MyInvocation.MyCommand.Path }
    $RepoRoot = Split-Path -Parent $here
}
if (-not $OutDir) { $OutDir = Join-Path $RepoRoot 'dist' }

$stamp   = Get-Date -Format 'yyyyMMdd'
$name    = "gtc-vps-$stamp"
$staging = Join-Path ([System.IO.Path]::GetTempPath()) $name
$zipPath = Join-Path $OutDir "$name.zip"

if (Test-Path $staging) { Remove-Item $staging -Recurse -Force }
New-Item -ItemType Directory -Path $staging -Force | Out-Null
New-Item -ItemType Directory -Path $OutDir -Force | Out-Null

Write-Host "staging $name"

# ------------------------------------------------------------- the app
$hubSrc = Join-Path $RepoRoot 'hub'
$hubDst = Join-Path $staging 'hub'
New-Item -ItemType Directory -Path $hubDst -Force | Out-Null

foreach ($item in @('app', 'tests', 'run.py', 'requirements.txt',
                    'requirements-dev.txt', 'constraints.txt', 'README.md')) {
    $src = Join-Path $hubSrc $item
    if (Test-Path $src) {
        Copy-Item $src -Destination $hubDst -Recurse -Force
    }
}

# Compiled bytecode from THIS machine's interpreter is at best useless on
# the target and at worst confusing when it shadows a changed source file.
Get-ChildItem $hubDst -Recurse -Directory -Filter '__pycache__' |
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

# ------------------------------------------------- config, as templates
$cfgDst = Join-Path $hubDst 'config'
New-Item -ItemType Directory -Path $cfgDst -Force | Out-Null

$reporterTemplate = [ordered]@{
    '_comment' = @(
        'Copier -> Operations hub reporter. The contract is docs/hub-reporter.md.',
        'OFF until the hub issues a key. Put the key in the file named by',
        'key_file (one line, nothing else) or in ODH_COPIER_KEY, set url to',
        'the hub, and set enabled to true.',
        'copier_id is configured, never derived, so moving this copier to',
        'another host does not look like a new copier to the hub.'
    )
    'hub_reporter' = [ordered]@{
        'enabled'               = $false
        'url'                   = ''
        'copier_id'             = 'gtc-vps1'
        'key_file'              = 'C:/GTC/config/hub_key.txt'
        'snapshot_interval_s'   = 15
        'timeout_s'             = 5
        'queue_max'             = 10000
        'max_events_per_report' = 200
    }
}
$reporterTemplate | ConvertTo-Json -Depth 5 |
    Set-Content (Join-Path $cfgDst 'hub_reporter.json.example') -Encoding utf8

@'
# Copy to .env beside run.py and fill in. Nothing here is committed.
#
# The IBKR Flex token and query id. The ops hub should have its OWN query
# id: the rate limit (error 1018) is per token and bites on frequency, and
# a query narrowed for one consumer silently changes the other's input.
TC_FLEX_TOKEN=
TC_FLEX_QUERY_ID=

# The dashboard and docs expect this port. Set it, or a restart silently
# moves the hub to the 8000 default.
TC_HTTP_PORT=8010

# State lives OFF the application directory, and must never be inside a
# synced folder: the registry is written transactionally, and a sync
# service copying it mid-write is a known way to get lock stalls and
# corruption. It is the record of every open position.
TC_DB_PATH=C:/GTC/data/tradecopier.db
TC_LOG_DIR=C:/GTC/logs
'@ | Set-Content (Join-Path $hubDst '.env.example') -Encoding utf8

# ----------------------------------------------------------- the scripts
foreach ($s in @('restart-copier.ps1', 'Register-CopierTask.ps1')) {
    Copy-Item (Join-Path $PSScriptRoot $s) -Destination $staging -Force
}
Copy-Item (Join-Path $PSScriptRoot 'Install-Copier.ps1') -Destination $staging -Force

# ------------------------------------------------------------- the docs
$docs = Join-Path $RepoRoot 'docs\hub-reporter.md'
if (Test-Path $docs) {
    New-Item -ItemType Directory -Path (Join-Path $staging 'docs') -Force | Out-Null
    Copy-Item $docs -Destination (Join-Path $staging 'docs') -Force
}
Copy-Item (Join-Path $PSScriptRoot 'README-VPS.md') -Destination $staging -Force

# ---------------------------------------------------------------- zip it
if (Test-Path $zipPath) { Remove-Item $zipPath -Force }
Compress-Archive -Path (Join-Path $staging '*') -DestinationPath $zipPath
Remove-Item $staging -Recurse -Force

$size = [math]::Round((Get-Item $zipPath).Length / 1MB, 2)
Write-Host ("built {0} ({1} MB)" -f $zipPath, $size) -ForegroundColor Green
Write-Host ''
Write-Host 'NOT in this package, by design -- put them on the VPS by hand:'
Write-Host '  hub\.env               (Flex token)       -- copy .env.example'
Write-Host '  C:\GTC\config\hub_key.txt   (hub API key)'
Write-Host '  hub\config\hub_reporter.json  -- copy the .example and set url + enabled'
Write-Host '  C:\GTC\data\tradecopier.db  -- only if moving a LIVE registry across'
