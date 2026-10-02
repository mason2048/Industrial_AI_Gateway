@echo off
setlocal
cd /d "%~dp0"
call "%~dp0scripts\run-python.bat" "%~dp0scripts\bootstrap.py" start %*
if errorlevel 1 goto fail
exit /b 0
:fail
echo Startup failed. See the error above. Python 3.12 and Internet are needed on first install.
if not defined GATEWAY_NO_PAUSE pause
exit /b 1
