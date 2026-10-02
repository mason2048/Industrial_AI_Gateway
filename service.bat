@echo off
setlocal
cd /d "%~dp0"
if "%~1"=="install" goto install
if "%~1"=="start" goto manage
if "%~1"=="stop" goto manage
if "%~1"=="status" goto manage
if "%~1"=="remove" goto manage
echo Usage: service.bat install [--offline --wheelhouse PATH]
echo        service.bat start ^| stop ^| status ^| remove
echo Use an Administrator terminal for install, start, stop, or remove.
exit /b 1
:install
call "%~dp0scripts\run-python.bat" "%~dp0scripts\bootstrap.py" service-init %2 %3 %4 %5 %6 %7 %8 %9
if errorlevel 1 exit /b 1
call "%~dp0scripts\run-python.bat" -m backend.windows_service install
exit /b %errorlevel%
:manage
if not exist "%~dp0.venv\Scripts\python.exe" (
  echo Service environment is missing. Run service.bat install first.
  exit /b 1
)
"%~dp0.venv\Scripts\python.exe" -m backend.windows_service %*
exit /b %errorlevel%
