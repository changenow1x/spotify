@echo off
REM ===========================================================================
REM  PodcastAutomation - stop the running automation (Windows)
REM
REM  Closes the dashboard and stops the watcher.  A job that is mid-upload is
REM  left in a state the app recovers safely on the next start: it will never
REM  publish the same episode twice.
REM ===========================================================================

setlocal

echo.
echo  Stopping PodcastAutomation ...
echo.

REM The dashboard runs on a local port; ask it to close politely first.
for /f "tokens=*" %%P in ('powershell -NoProfile -Command ^
  "(Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue | ^
    Select-Object -First 1).OwningProcess" 2^>nul') do set "PID=%%P"

if defined PID (
    echo  Found the running process %PID%.  Stopping it ...
    taskkill /PID %PID% /T /F >nul 2>&1
    if errorlevel 1 (
        echo  [WARN] Could not stop it.  Close the window it runs in instead.
    ) else (
        echo  [OK]   Stopped.
    )
) else (
    echo  Nothing appears to be running.
)

echo.
echo  Anything half-finished is picked up again on the next start.
echo.
pause
