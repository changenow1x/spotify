@echo off
REM ===========================================================================
REM  PodcastAutomation - sign in to Spotify once (Windows)
REM
REM  Opens a real browser window so YOU can type your password.  The app never
REM  sees, asks for or stores it.  Afterwards the legitimate session is kept in
REM  this installation and re-used automatically.
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
echo  A browser window will open.  Sign in to Spotify there.
echo.
echo    * Type your own password in the browser, never here.
echo    * If Spotify asks for two-factor confirmation, do it in the browser.
echo    * Do not close the window until it says the sign-in worked.
echo.
echo  This happens once.  The session is then reused for every episode.
echo.

pause
"%PY%" -m app --auth
set "CODE=%errorlevel%"

echo.
if "%CODE%"=="0" (
    echo  You are signed in.  Start the automation with START.bat.
) else (
    echo  Signing in did not complete.  Read the message above and try again.
)
echo.
pause
exit /b %CODE%
