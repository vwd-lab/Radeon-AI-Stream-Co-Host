@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"

REM ---------------------------------------------------------------------------
REM Kill switch: `start_bot.bat stop` shuts the bot AND the voice down.
REM
REM Order matters: the bot goes first so it cannot fire another synthesis
REM request into a server that is being torn down. The voice is stopped last.
REM ---------------------------------------------------------------------------
if /i "%~1"=="stop" goto :stop

REM Bring up the Pocket TTS server before the bot, so the first line has a
REM voice. It runs in its own CPU venv and exits early if a healthy server is
REM already up. Non-fatal: if it fails, the bot still starts and reports the
REM missing voice.
call "%~dp0start_pocket_tts.bat"

set "PY=python"
if exist "%~dp0.venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe"

"%PY%" main.py
pause
exit /b 0

:stop
echo [bot] stop: shutting down the bot...

REM main.py writes its own PID to data\bot.pid at startup. Reading that is
REM deliberate: scanning for python.exe would also hit the Pocket server and
REM the dashboard. Verify the PID really is a python process before killing it,
REM so a stale recycled PID cannot take down something unrelated.
set "PIDFILE=%~dp0data\bot.pid"
set "BOTPID="
if exist "%PIDFILE%" (
  set /p BOTPID=<"%PIDFILE%"
)

if "!BOTPID!"=="" (
  echo [bot] stop: no PID file, assuming the bot is not running.
) else (
  for /f "tokens=1,2 delims=," %%n in ('tasklist /FI "PID eq !BOTPID!" /FO CSV /NH 2^>nul') do (
    set "IMG=%%~n"
  )
  echo !IMG! | findstr /I "python" >nul
  if errorlevel 1 (
    echo [bot] stop: PID !BOTPID! is not a python process - stale PID file
    echo           left behind by a previous run, skipping it.
  ) else (
    echo [bot] stop: killing bot pid !BOTPID!
    taskkill /PID !BOTPID! /T /F >nul 2>&1
    if errorlevel 1 echo [bot] stop: WARNING - could not kill pid !BOTPID!
  )
  del "%PIDFILE%" >nul 2>&1
)

REM Now the voice, via the shared kill switch in the pocket launcher.
call "%~dp0start_pocket_tts.bat" stop
echo [bot] stop: done.
endlocal
exit /b 0
