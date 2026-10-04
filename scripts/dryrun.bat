@echo off
rem Generates sample posts without posting (output\dryrun\sample_*.txt).
rem Options: --count N  --no-collect  --weekly
call "%~dp0fp.bat" dry-run %*
set RC=%ERRORLEVEL%
pause
exit /b %RC%
