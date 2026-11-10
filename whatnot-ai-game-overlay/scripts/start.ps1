#Requires -Version 5.1
<#
.SYNOPSIS
  Starts the Whatnot AI Game Overlay (backend + overlay + private dashboard).

.DESCRIPTION
  Production mode by default. If the app exits unexpectedly it is restarted
  automatically (settings, caches and history survive restarts; the OBS overlay
  reconnects by itself — no OBS refresh needed). Press Ctrl+C to stop.

.PARAMETER Demo
  Mock camera, mock Claude and mock PriceCharting data (watermarked). No API costs.
.PARAMETER Dev
  Development mode: debug logging and auto-reload on code changes.
.PARAMETER Port
  Local port (default 8765). The overlay URL becomes http://127.0.0.1:<Port>/overlay
.PARAMETER NoBrowser
  Do not open the dashboard automatically.
#>
param(
    [switch]$Demo,
    [switch]$Dev,
    [int]$Port = 8765,
    [switch]$NoBrowser
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$py = Join-Path $Root '.venv\Scripts\python.exe'
if (-not (Test-Path $py)) {
    Write-Host "The app is not installed yet. Run Install.cmd first." -ForegroundColor Red
    exit 1
}

$inUse = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($inUse) {
    Write-Host "Port $Port is already in use (is the app already running?)." -ForegroundColor Yellow
    Write-Host "Close the other window, or start with:  Start.cmd -Port 8766  (and use that port in OBS)."
    exit 1
}

$argsList = @('-m', 'gameoverlay', 'serve', '--port', "$Port")
if ($Demo) { $argsList += '--demo' }
if ($Dev) { $argsList += @('--env', 'development', '--reload') } else { $argsList += @('--env', 'production') }

$first = $true
$failures = 0
while ($true) {
    $runArgs = $argsList
    if ($first -and -not $NoBrowser) { $runArgs = $argsList + '--open' }
    $first = $false
    $started = Get-Date
    & $py @runArgs
    $code = $LASTEXITCODE
    if ($code -eq 0 -or $Dev) { break }
    if (((Get-Date) - $started).TotalSeconds -gt 60) { $failures = 0 } else { $failures++ }
    if ($failures -ge 5) {
        Write-Host "The app keeps stopping right after start (exit code $code). See docs\TROUBLESHOOTING.md and the log in %LOCALAPPDATA%\WhatnotGameOverlay\logs." -ForegroundColor Red
        exit $code
    }
    Write-Warning "App exited unexpectedly (code $code). Restarting in 3 seconds... (Ctrl+C to stop)"
    Start-Sleep -Seconds 3
}
