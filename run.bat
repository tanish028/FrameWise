@echo off
rem FrameWise launcher: sets up a private Python environment on first run, then starts the app.
setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\pythonw.exe" goto launch

where py >nul 2>nul && (set "PY=py -3") || (set "PY=python")
%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
if errorlevel 1 (
    echo FrameWise needs Python 3.10 or newer.
    echo Install it from https://www.python.org/downloads/ ^(tick "Add python.exe to PATH"^) and run this again.
    pause
    exit /b 1
)
echo First run: setting up FrameWise. This takes a minute or two...
%PY% -m venv .venv || goto fail
".venv\Scripts\python.exe" -m pip install --upgrade pip >nul
".venv\Scripts\python.exe" -m pip install -e . || goto fail

:launch
start "" ".venv\Scripts\pythonw.exe" -m framewise %*
exit /b 0

:fail
echo Setup failed. See the messages above.
rmdir /s /q .venv 2>nul
pause
exit /b 1
