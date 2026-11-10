#Requires -Version 5.1
<#
.SYNOPSIS
  Installs the Whatnot AI Game Overlay on Windows.

.DESCRIPTION
  * Finds Python 3.12+ (Python launcher "py" or "python" on PATH).
  * Creates a private virtual environment in .venv and installs pinned dependencies.
  * Initializes the database and the private dashboard access token in
    %LOCALAPPDATA%\WhatnotGameOverlay (outside the project folder; never committed).
  * Optionally installs test tooling (-WithTests) and a desktop shortcut (-Shortcut).

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -Shortcut
#>
param(
    [switch]$WithTests,
    [switch]$Shortcut,
    [switch]$Recreate
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

function Write-Step($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }

function Find-Python {
    # Returns e.g. "py -3.12" or "python" (a single string; split by the caller).
    $candidates = @('py -3.13', 'py -3.12', 'py -3', 'python', 'python3')
    foreach ($c in $candidates) {
        $parts = $c.Split(' ')
        $exe = $parts[0]
        $extra = @($parts | Select-Object -Skip 1)
        if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
        try {
            # The Microsoft Store "python" stub exits with 9009 and is skipped here.
            $ok = & $exe @extra -c "import sys; print(1 if sys.version_info >= (3, 12) else 0)" 2>$null
            if ($LASTEXITCODE -eq 0 -and "$ok".Trim() -eq '1') { return $c }
        } catch { }
    }
    return $null
}

Write-Step "Looking for Python 3.12 or newer"
$pyCmd = Find-Python
if (-not $pyCmd) {
    Write-Host ""
    Write-Host "Python 3.12+ was not found." -ForegroundColor Red
    Write-Host "Install it from https://www.python.org/downloads/windows/ (tick 'Add python.exe to PATH'),"
    Write-Host "or run:  winget install -e --id Python.Python.3.12"
    Write-Host "then run Install.cmd again."
    exit 1
}
$pyParts = $pyCmd.Split(' ')
$pyExe = $pyParts[0]
$pyArgs = @($pyParts | Select-Object -Skip 1)
$ver = & $pyExe @pyArgs -c "import sys; print('%d.%d.%d' % sys.version_info[:3])"
Write-Host "    using $pyCmd (Python $ver)"

$venvPy = Join-Path $Root '.venv\Scripts\python.exe'
if ($Recreate -and (Test-Path (Join-Path $Root '.venv'))) {
    Write-Step "Removing existing virtual environment"
    Remove-Item -Recurse -Force (Join-Path $Root '.venv')
}
if (-not (Test-Path $venvPy)) {
    Write-Step "Creating virtual environment (.venv)"
    & $pyExe @pyArgs -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw "Could not create the virtual environment." }
}

Write-Step "Installing dependencies (this can take a few minutes the first time)"
& $venvPy -m pip install --upgrade pip --disable-pip-version-check
if ($LASTEXITCODE -ne 0) { throw "pip upgrade failed" }
& $venvPy -m pip install -r requirements.txt --disable-pip-version-check
if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed. Check your internet connection and try again." }

if ($WithTests) {
    Write-Step "Installing test tooling (pytest, Playwright + Chromium)"
    & $venvPy -m pip install -r requirements-dev.txt --disable-pip-version-check
    if ($LASTEXITCODE -ne 0) { throw "Test dependency installation failed." }
    & $venvPy -m playwright install chromium
    if ($LASTEXITCODE -ne 0) { Write-Warning "Playwright browser install failed; browser tests will be skipped." }
}

Write-Step "Initializing database and dashboard access token"
& $venvPy -m gameoverlay init --env production
if ($LASTEXITCODE -ne 0) { throw "Initialization failed." }

if (-not (Test-Path (Join-Path $Root '.env')) -and (Test-Path (Join-Path $Root '.env.example'))) {
    Copy-Item (Join-Path $Root '.env.example') (Join-Path $Root '.env')
    Write-Host "    created .env from .env.example (optional settings; credentials are entered in the dashboard)"
}

if ($Shortcut) {
    Write-Step "Creating desktop shortcuts"
    $shell = New-Object -ComObject WScript.Shell
    $desk = [Environment]::GetFolderPath('Desktop')
    foreach ($pair in @(@('Price-O-Matic.lnk', 'Start.cmd'), @('Price-O-Matic (Demo).lnk', 'Start-Demo.cmd'))) {
        $lnk = $shell.CreateShortcut((Join-Path $desk $pair[0]))
        $lnk.TargetPath = Join-Path $Root $pair[1]
        $lnk.WorkingDirectory = $Root
        $lnk.Save()
    }
}

Write-Host ""
Write-Host "Installation complete." -ForegroundColor Green
Write-Host "  Try it safely with sample data:   Start-Demo.cmd"
Write-Host "  Run live for your stream:         Start.cmd"
Write-Host "  The first live start opens a setup wizard for your API keys, OBS and camera."
