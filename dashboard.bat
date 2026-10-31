@echo off
title AI Co-Host LCARS Dashboard
cd /d "%~dp0"

REM Uses whatever "python" resolves to on PATH. If PySide6 or dotenv are not
REM installed for that interpreter, point PY at one that has them.
set "PY=python"
if exist "%~dp0.venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe"

echo Launching AI Co-Host LCARS Status Dashboard...
echo (Run alongside start_bot.bat for a live feed.)
"%PY%" -X utf8 dashboard.py
pause
