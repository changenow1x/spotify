@echo off
REM ===========================================================================
REM  PodcastAutomation - test the Spotify connection (Windows)
REM
REM  READ-ONLY.  It signs in with the saved session, checks that the browser
REM  can reach Spotify for Creators and that the show is readable.
REM  It creates, uploads and publishes NOTHING.
REM ===========================================================================

setlocal
cd /d "%~dp0"

set "PY=%CD%\.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo  [FAIL] The environment is missing.  Run SETUP.bat first.
    pause
    exit /b 1
)

echo.
echo  This check is READ-ONLY.  No episode will be created or published.
echo.
pause
"%PY%" -m app spotify-check --live
set "CODE=%errorlevel%"
echo.
pause
exit /b %CODE%
