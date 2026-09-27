@echo off
REM ===========================================================================
REM  PodcastAutomation - first time setup (Windows)
REM
REM  Creates a private Python environment, installs what is needed, downloads
REM  the browser Playwright drives, and finishes with a full environment check.
REM
REM  Run this once.  If anything goes wrong, read the last lines it printed.
REM ===========================================================================

setlocal
cd /d "%~dp0"

echo.
echo  PodcastAutomation - Setup
echo  ========================
echo.

REM --- 1. find Python -------------------------------------------------------
set "PYTHON_CMD="
where py >nul 2>&1 && set "PYTHON_CMD=py -3.11"
if not defined PYTHON_CMD (
    where python >nul 2>&1 && set "PYTHON_CMD=python"
)
if not defined PYTHON_CMD (
    echo  [FAIL] Python 3.10 or newer is not on your PATH.
    echo         Install it from https://www.python.org/downloads/
    echo         and tick "Add python.exe to PATH" during the install.
    echo.
    pause
    exit /b 1
)

echo  [OK]   Python found: %PYTHON_CMD%
%PYTHON_CMD% -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)"
if errorlevel 1 (
    echo  [FAIL] That Python is too old. Install 3.10 or newer.
    echo.
    pause
    exit /b 1
)

REM --- 2. virtual environment ----------------------------------------------
if not exist ".venv\Scripts\python.exe" (
    echo.
    echo  Creating a private environment in .venv ...
    %PYTHON_CMD% -m venv .venv
    if errorlevel 1 (
        echo  [FAIL] Could not create the environment.
        echo.
        pause
        exit /b 1
    )
)
set "PY=%CD%\.venv\Scripts\python.exe"
echo  [OK]   Environment ready.

REM --- 3. dependencies ------------------------------------------------------
echo.
echo  Installing the required packages.  This can take a few minutes ...
"%PY%" -m pip install --upgrade pip --quiet
"%PY%" -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo  [FAIL] Installing the packages failed.  The most common cause is no
    echo         internet connection, or a proxy.  Try again, then run:
    echo             "%PY%" -m pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)
echo  [OK]   Packages installed.

REM --- 4. the browser -------------------------------------------------------
echo.
echo  Downloading the browser (about 150 MB, once) ...
"%PY%" -m playwright install chromium
if errorlevel 1 (
    echo.
    echo  [WARN] The browser download failed.  This is usually a network or
    echo         proxy problem.  Nothing else is broken - retry when you are
    echo         online:
    echo             "%PY%" -m playwright install chromium
    echo.
) else (
    echo  [OK]   Browser installed.
)

REM --- 5. folders and check ------------------------------------------------
echo.
if not exist "config" mkdir "config"
if not exist "config\config.yaml" (
    if exist "config\config.example.yaml" (
        copy /y "config\config.example.yaml" "config\config.yaml" >nul
        echo  [OK]   Created config\config.yaml from the example.
    )
)

echo.
echo  Checking everything ...
echo.
"%PY%" -m app --check
set "CHECKCODE=%errorlevel%"

echo.
echo  ========================
if "%CHECKCODE%"=="0" (
    echo   Setup finished.
    echo.
    echo   Next:  1. Sign in to Spotify once:   SIGN_IN.bat
    echo           2. Start the automation:      START.bat
) else (
    echo   Setup finished, but some items above need attention.
    echo   Read the [FAIL] lines and fix them, then run:
    echo       "%PY%" -m app --check
)
echo  ========================
echo.
pause
exit /b %CHECKCODE%
