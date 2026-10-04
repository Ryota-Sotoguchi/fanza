@echo off
rem Shows low-performing patterns and disables only the ones you approve (y/N).
call "%~dp0fp.bat" review %*
set RC=%ERRORLEVEL%
pause
exit /b %RC%
