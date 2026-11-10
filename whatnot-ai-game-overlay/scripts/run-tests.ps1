#Requires -Version 5.1
<#
.SYNOPSIS
  Runs the automated test suite (Python + browser rendering + JavaScript unit tests).
  Install test tooling first:  Install.cmd -WithTests
.PARAMETER Live
  Also run the opt-in live tests against the real Anthropic / PriceCharting / OBS
  services using credentials from environment variables (costs a few cents).
#>
param([switch]$Live)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$py = Join-Path $Root '.venv\Scripts\python.exe'
if (-not (Test-Path $py)) { throw "Run Install.cmd -WithTests first." }

& $py -m pytest -q
$pyCode = $LASTEXITCODE

$jsCode = 0
if (Get-Command node -ErrorAction SilentlyContinue) {
    node --test "tests/js/*.test.mjs"
    $jsCode = $LASTEXITCODE
} else {
    Write-Warning "Node.js not found; skipping JavaScript unit tests (optional)."
}

$liveCode = 0
if ($Live) {
    $env:WGO_RUN_LIVE = '1'
    & $py -m pytest -m live tests/live -v -rs
    $liveCode = $LASTEXITCODE
}

if ($pyCode -ne 0 -or $jsCode -ne 0 -or $liveCode -ne 0) { exit 1 }
Write-Host "All requested tests passed." -ForegroundColor Green
