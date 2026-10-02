@echo off
setlocal
set "GATEWAY_ROOT=%~dp0.."
if exist "%GATEWAY_ROOT%\.venv\Scripts\python.exe" goto environment
py -3.12 -c "import sys; sys.exit(0 if sys.version_info[:2] == (3, 12) else 1)" >nul 2>&1
if not errorlevel 1 goto launcher
python -c "import sys; sys.exit(0 if sys.version_info[:2] == (3, 12) else 1)" >nul 2>&1
if not errorlevel 1 goto pathpython
echo Python 3.12 was not found. Install 64-bit Python 3.12 from https://www.python.org/downloads/windows/
echo Enable the Python launcher or Add python.exe to PATH, then reopen this window.
exit /b 1
:environment
"%GATEWAY_ROOT%\.venv\Scripts\python.exe" %*
exit /b %errorlevel%
:launcher
py -3.12 %*
exit /b %errorlevel%
:pathpython
python %*
exit /b %errorlevel%
