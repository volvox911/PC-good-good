@echo off
REM Starts in DEMO mode: mock camera, mock Claude, mock PriceCharting (watermarked sample data).
title Price-O-Matic (DEMO)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start.ps1" -Demo -Port 8766 %*
pause
