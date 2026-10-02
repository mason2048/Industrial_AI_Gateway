@echo off
setlocal
cd /d "%~dp0"
call "%~dp0scripts\run-python.bat" "%~dp0scripts\bootstrap.py" check %*
set "GATEWAY_EXIT=%errorlevel%"
if not "%GATEWAY_EXIT%"=="0" if not defined GATEWAY_NO_PAUSE pause
exit /b %GATEWAY_EXIT%
