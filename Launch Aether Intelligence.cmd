@echo off
REM Double-click this to launch Aether Intelligence (dev mode).
REM Starts the Python backend, then opens the app window.
REM First launch compiles the Rust shell — that takes a few minutes.
title Aether Intelligence launcher
powershell -ExecutionPolicy Bypass -NoProfile -File "%~dp0scripts\start-dev.ps1"
echo.
echo (This window can be closed once the app is open.)
pause
