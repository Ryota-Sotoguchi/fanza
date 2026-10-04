@echo off
rem Checks config, .env, DMM API, Anthropic API and X login state. Does not post anything.
rem Options: --skip-browser  --skip-api  --notify-test
call "%~dp0fp.bat" selfcheck %*
set RC=%ERRORLEVEL%
pause
exit /b %RC%
