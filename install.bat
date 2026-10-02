@echo off
setlocal
cd /d "%~dp0"
call "%~dp0scripts\run-python.bat" "%~dp0scripts\bootstrap.py" init %*
set "GATEWAY_EXIT=%errorlevel%"
if "%GATEWAY_EXIT%"=="0" (
  echo Installation complete. Run check.bat, then start.bat.
) else (
  echo Installation failed. See the error above.
  if not defined GATEWAY_NO_PAUSE pause
)
exit /b %GATEWAY_EXIT%
