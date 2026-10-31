@echo off
REM ---------------------------------------------------------------------------
REM Pocket TTS server for the AI co-host.
REM
REM Pocket runs best in its OWN venv with a recent CPU torch: under a ROCm
REM torch build the same model measures RTF 2.6 instead of RTF 0.25 because
REM ROCm's wheel has slow CPU kernels. Keeping it on CPU also leaves the GPU
REM free for the game and the local LLM.
REM
REM Set POCKET_VENV to override the venv location. Default is .\pocket-tts-venv.
REM start_bot.bat calls this automatically. Run it by hand only if you want the
REM voice without the bot. Safe to run twice: it exits early if the server is
REM already healthy.
REM ---------------------------------------------------------------------------
setlocal EnableDelayedExpansion
set "ROOT=%~dp0"
if defined POCKET_VENV (
  set "VENV=%POCKET_VENV%"
) else (
  set "VENV=%ROOT%pocket-tts-venv"
)
set "VOICE=%ROOT%data\pocket_voice.safetensors"
set "PORT=8123"
set "LANG=english_2026-09"

REM ---------------------------------------------------------------------------
REM Kill switch: `start_pocket_tts.bat stop` shuts the server down.
REM
REM Killing is by PORT, not by process name, because a hand-started server may
REM not share this script's process tree.
REM ---------------------------------------------------------------------------
if /i "%~1"=="stop" goto :stop

REM Already up? Then there is nothing to do (avoids spawning a second server
REM and a second copy of the model in RAM).
curl -s -m 3 http://localhost:%PORT%/health 2>nul | findstr /C:"healthy" >nul
if not errorlevel 1 (
  echo [pocket-tts] already running on port %PORT%
  endlocal
  exit /b 0
)

if not exist "%VOICE%" (
  echo [pocket-tts] SKIP: voice state missing "%VOICE%"
  echo             Run tools\export_pocket_voice.py to create it.
  endlocal
  exit /b 1
)

if not exist "%VENV%\Scripts\python.exe" (
  echo [pocket-tts] SKIP: venv missing "%VENV%"
  echo             Create it with: python -m venv pocket-tts-venv
  echo             then install pocket-tts into it.
  endlocal
  exit /b 1
)

echo [pocket-tts] starting on port %PORT%  language=%LANG%
echo [pocket-tts] voice: %VOICE%

REM /min keeps it out of the way. Closing this process stops the voice and
REM there is no fallback engine, so start_bot.bat brings it back up.
start "pocket-tts" /min "%VENV%\Scripts\python.exe" -m pocket_tts serve ^
  --port %PORT% --language %LANG% --default-voice "%VOICE%"

REM Wait for the model to load. ping beats `timeout` here because a Git/MSYS
REM bash on PATH shadows the Windows `timeout` with GNU coreutils' version.
ping -n 31 127.0.0.1 >nul
curl -s -m 5 http://localhost:%PORT%/health >nul 2>&1
if not errorlevel 1 (
  echo [pocket-tts] ready on http://localhost:%PORT%
  endlocal
  exit /b 0
)

echo [pocket-tts] WARNING: not healthy after 30s. The bot will start without a
echo             voice until the server finishes loading. Check the minimised
echo             "pocket-tts" window.
endlocal
exit /b 1

:stop
REM Find whatever is LISTENING on the port. netstat columns are Proto, Local,
REM Foreign, State, PID, so tokens=5 is the PID.
set "FOUND="
for /f "tokens=5" %%p in ('netstat -ano -p TCP ^| findstr /R /C:":%PORT% .*LISTENING"') do (
  if not "%%p"=="0" set "FOUND=%%p"
)

if "!FOUND!"=="" (
  echo [pocket-tts] stop: nothing listening on port %PORT%, nothing to do.
  endlocal
  exit /b 0
)

echo [pocket-tts] stop: shutting down server on port %PORT% ^(pid !FOUND!^)
taskkill /PID !FOUND! /T /F >nul 2>&1
if errorlevel 1 (
  echo [pocket-tts] stop: WARNING - could not kill pid !FOUND!.
  echo             It may need closing by hand: taskkill /PID !FOUND! /T /F
  endlocal
  exit /b 1
)

REM Confirm the port actually came free, so callers can trust the exit code.
ping -n 3 127.0.0.1 >nul
curl -s -m 2 http://localhost:%PORT%/health 2>nul | findstr /C:"healthy" >nul
if errorlevel 1 (
  echo [pocket-tts] stop: done, port %PORT% is free.
  endlocal
  exit /b 0
)

echo [pocket-tts] stop: WARNING - something is still answering on port %PORT%.
endlocal
exit /b 1
