@echo off
REM ===========================================================================
REM  PodcastAutomation - restart the automation (Windows)
REM
REM  Stops whatever is running, then starts it again.  Use this after changing
REM  config.yaml, after signing in, or any time the app seems stuck.
REM
REM  A job that was mid-upload is left in a state the app recovers safely:
REM  on restart it is checked against Spotify first and never published twice.
REM ===========================================================================

setlocal
cd /d "%~dp0"

set "PY=%CD%\.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo.
    echo  [FAIL] The environment is missing.  Run SETUP.bat first.
    echo.
    pause
    exit /b 1
)

echo.
echo  Stopping any running instance ...
echo.
call "%~dp0STOP.bat" >nul 2>&1

REM  Give the port a moment to be released before we take it again.
timeout /t 2 /nobreak >nul 2>&1

echo  Starting PodcastAutomation again.  A new window will open.
echo.

REM  Start in its own window so this one can close.  If that fails for any
REM  reason, fall back to running it here in the foreground.
start "PodcastAutomation" "%PY%" -m app
if errorlevel 1 (
    echo  [WARN] Could not open a new window; starting here instead.
    echo.
    "%PY%" -m app
    exit /b %errorlevel%
)

echo  [OK]   Restarted.
echo.
echo  The dashboard is at http://127.0.0.1:8765
echo.
pause
