@echo off
REM Installs the Whatnot AI Game Overlay (Python 3.12+ required).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\install.ps1" -Shortcut %*
echo.
pause
