@echo off
REM Starts the overlay server + private dashboard (LIVE mode).
title Price-O-Matic (LIVE)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start.ps1" %*
pause
