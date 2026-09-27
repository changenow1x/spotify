@echo off
REM ===========================================================================
REM  PodcastAutomation - check that everything is ready (Windows)
REM
REM  Safe to run at any time.  It changes nothing except creating missing
REM  folders, and it never touches Spotify.
REM ===========================================================================

setlocal
cd /d "%~dp0"

set "PY=%CD%\.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo  [FAIL] The environment is missing.  Run SETUP.bat first.
    pause
    exit /b 1
)

"%PY%" -m app --check
set "CODE=%errorlevel%"
echo.
pause
exit /b %CODE%
