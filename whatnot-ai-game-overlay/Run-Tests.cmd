@echo off
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\run-tests.ps1" %*
pause
