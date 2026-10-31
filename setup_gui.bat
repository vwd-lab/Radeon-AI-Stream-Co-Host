@echo off
title Radeon AI Co-Host Setup
cd /d "%~dp0"

REM Launches the identity setup GUI. Writes BOT_NAME, STREAMER_NAME and related
REM fields to .env. Restart start_bot.bat for the change to take effect.
set "PY=python"
if exist "%~dp0.venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe"

"%PY%" setup_gui.py
pause
exit /b 0
