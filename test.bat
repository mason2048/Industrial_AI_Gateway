@echo off
setlocal
cd /d "%~dp0"
call "%~dp0scripts\run-python.bat" "%~dp0scripts\bootstrap.py" test %*
exit /b %errorlevel%
