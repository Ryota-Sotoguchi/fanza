@echo off
rem Generic launcher: scripts\fp.bat <command> [options]
rem Examples: fp.bat status / fp.bat resume / fp.bat review / fp.bat report
setlocal
cd /d "%~dp0.."
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
if not exist ".venv\Scripts\python.exe" (
  echo .venv not found. Run scripts\setup.bat first.
  exit /b 1
)
".venv\Scripts\python.exe" -m fanza_poster %*
exit /b %ERRORLEVEL%
