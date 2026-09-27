@echo off
REM ===========================================================================
REM  PodcastAutomation - start the automation (Windows)
REM
REM  Watches the incoming folders, publishes finished episodes to Spotify for
REM  Creators, archives them, and opens the local dashboard.
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
echo  Starting PodcastAutomation.  Leave this window open.
echo  Press Ctrl+C to stop.
echo.
echo  Your shows are in the "shows" folder next to this script:
for /d %%D in ("%CD%\shows\*") do (
    echo     %%~nxD  -^>  drop .mp4/.mp3 + .txt into its "incoming" folder
)
echo.

"%PY%" -m app
set "CODE=%errorlevel%"

echo.
if not "%CODE%"=="0" (
    echo  The automation stopped with an error.  The last lines above say why.
    echo  Run "%PY%" -m app doctor for a deeper look.
    echo.
    pause
)
exit /b %CODE%
