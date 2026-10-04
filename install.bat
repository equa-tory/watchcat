@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY (
  echo Python 3 not found. Install it from https://www.python.org/downloads/ ^(tick "Add to PATH"^).
  pause
  exit /b 1
)

if not exist .env (
  set "PW="
  set /p "PW=Password for making changes (empty = no login): "
  > .env echo PASSWORD=!PW!
  >> .env echo PORT=8888
  echo Created .env
)

echo Starting on http://localhost:8888  (Ctrl+C to stop; edit PORT in .env)
%PY% server.py
pause
