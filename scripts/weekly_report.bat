@echo off
rem Weekly report (reports\week_NN.md / .csv). Enter click data in data\clicks_by_id.csv first.
rem Option: --week N
call "%~dp0fp.bat" report %*
set RC=%ERRORLEVEL%
pause
exit /b %RC%
