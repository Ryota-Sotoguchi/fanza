@echo off
rem Called by Windows Task Scheduler every 10 minutes (12:00 - 01:00).
rem Collects sale items, makes the daily plan, and posts at most one due item.
call "%~dp0fp.bat" tick
exit /b %ERRORLEVEL%
