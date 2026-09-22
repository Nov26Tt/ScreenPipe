@echo off
chcp 65001 >nul
title SnapStudy Launcher

cd /d "%~dp0"
REM Capture the script directory here: %~dp0 is not reliable inside a
REM subroutine invoked with "call :label".
set "PROJECT_DIR=%~dp0"

echo ============================================
echo         SnapStudy v1.0
echo ============================================
echo.

REM Pick an interpreter: project venv first, then PATH, skipping the
REM Microsoft Store stub.
call :find_python
if defined PYTHON_EXE goto :start_service

echo [ERROR] No usable Python found. Please install Python 3.10+
echo         Download: https://www.python.org/downloads/
echo.
echo [HINT] If Python is already installed, the Microsoft Store stub
echo        python.exe is probably shadowing it. Turn it off via:
echo        Settings - Apps - Advanced app settings - App execution aliases
echo        (disable both python.exe and python3.exe), then retry.
pause
exit /b 1


:start_service
echo [ENV] Interpreter: %PYTHON_EXE%
%PYTHON_EXE% --version
echo.

REM Check whether dependencies are installed.
REM NOTE: no parenthesised blocks here on purpose - %errorlevel% inside a
REM bracketed block is expanded when the block is parsed, not when it runs,
REM which makes the test below always report a false failure. Plain "goto"
REM keeps every %errorlevel% evaluated at execution time.
%PYTHON_EXE% -c "import fastapi" >nul 2>&1
if %errorlevel% equ 0 goto :deps_ready

echo [INFO] Installing dependencies into the interpreter above...
%PYTHON_EXE% -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
if %errorlevel% neq 0 goto :deps_failed

:deps_ready
echo [START] Launching service...
echo.
echo Open the URL below in your phone browser:
echo.

%PYTHON_EXE% main.py

pause
exit /b 0


:deps_failed
echo [ERROR] Failed to install dependencies, please check your network.
pause
exit /b 1


REM ==================== internal: interpreter probe ====================

REM NOTE: keep this file pure ASCII. Non-ASCII characters combined with the
REM chcp switch above make cmd mis-track file offsets and execute garbage.

:find_python
set "PYTHON_EXE="

REM 1) project-local virtual environment: isolated deps, preferred
call :test_exe "%PROJECT_DIR%.venv\Scripts\python.exe"
if defined PYTHON_EXE exit /b 0

call :test_exe "%PROJECT_DIR%venv\Scripts\python.exe"
if defined PYTHON_EXE exit /b 0

call :test_exe "%PROJECT_DIR%env\Scripts\python.exe"
if defined PYTHON_EXE exit /b 0

REM 2) every python.exe / python3.exe on PATH (also covers an activated venv;
REM    the Store stub is rejected because it prints no version)
for /f "delims=" %%p in ('where python 2^>nul') do call :test_exe "%%p"
if defined PYTHON_EXE exit /b 0

for /f "delims=" %%p in ('where python3 2^>nul') do call :test_exe "%%p"
if defined PYTHON_EXE exit /b 0

REM 3) the py launcher
for /f "delims=" %%p in ('where py 2^>nul') do call :test_exe "%%p"
if defined PYTHON_EXE exit /b 0

REM 4) well-known install locations
for %%d in (
    "%LocalAppData%\Programs\Python\Python313"
    "%LocalAppData%\Programs\Python\Python312"
    "%LocalAppData%\Programs\Python\Python311"
    "%LocalAppData%\Programs\Python\Python310"
    "C:\Python313"
    "C:\Python312"
    "C:\Python311"
    "C:\Python310"
    "C:\Program Files\Python313"
    "C:\Program Files\Python312"
    "C:\Program Files\Python311"
) do call :test_exe "%%~d\python.exe"

exit /b 0


REM %1 = candidate interpreter path.
REM Accept it only when it prints a real version string ("Python 3.x.y").
REM The 0-byte Microsoft Store stub prints nothing, so it gets rejected.
:test_exe
if defined PYTHON_EXE exit /b 0
set "PYVER="
for /f "delims=" %%v in ('""%~1" --version" 2^>nul') do set "PYVER=%%v"
if not defined PYVER exit /b 0
echo %PYVER% | findstr /b /c:"Python 3." >nul
if errorlevel 1 exit /b 0
set "PYTHON_EXE="%~1""
exit /b 0
