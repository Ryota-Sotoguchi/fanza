@echo off
rem Opens the browser profile so you can log in to X manually (first time / after logout).
call "%~dp0fp.bat" login
set RC=%ERRORLEVEL%
pause
exit /b %RC%
