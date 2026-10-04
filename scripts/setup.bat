@echo off
rem First-time setup: create .venv and install dependencies.
setlocal
cd /d "%~dp0.."
where py >nul 2>nul
if %ERRORLEVEL%==0 (set "PYLAUNCH=py -3") else (set "PYLAUNCH=python")
if not exist ".venv\Scripts\python.exe" (
  echo Creating virtual environment .venv ...
  %PYLAUNCH% -m venv .venv
  if errorlevel 1 goto :error
)
".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 goto :error
".venv\Scripts\python.exe" -m pip install -e .
if errorlevel 1 goto :error
if not exist ".env" (
  copy ".env.example" ".env" >nul
  echo Created .env from .env.example. Open .env and fill in your keys.
)
echo.
echo Setup finished.
echo Next: edit .env and config.yaml, then run scripts\login.bat and scripts\selfcheck.bat
echo If you set "browser_channel: chromium" in config.yaml, also run:
echo   .venv\Scripts\python.exe -m playwright install chromium
pause
exit /b 0
:error
echo.
echo Setup failed. Check that Python 3.11 or later is installed.
pause
exit /b 1
