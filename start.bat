@echo off
chcp 65001 >nul
title SnapStudy Launcher

cd /d "%~dp0"
REM Capture the script directory here: %~dp0 is not reliable inside a
REM subroutine invoked with "call :label".
set "PROJECT_DIR=%~dp0"

echo ============================================
echo      SnapStudy v1.0 - Setup ^& Launch
echo ============================================
echo.
echo This script will prepare the whole environment:
echo   1) locate Python  2) create .venv  3) install deps
echo   4) create config.yaml from template  5) start service
echo.

REM ============================================================
REM  1/5  Locate a usable Python interpreter (3.10+)
REM ============================================================
call :find_python
if defined PYTHON_EXE goto :python_ok

echo [ERROR] No usable Python found. Please install Python 3.10+
echo         Download: https://www.python.org/downloads/
echo.
echo [HINT] If Python is already installed, the Microsoft Store stub
echo        python.exe is probably shadowing it. Turn it off via:
echo        Settings - Apps - Advanced app settings - App execution aliases
echo        (disable both python.exe and python3.exe), then retry.
pause
exit /b 1

:python_ok
echo [1/5] Interpreter: %PYTHON_EXE%
%PYTHON_EXE% --version
echo.

REM ============================================================
REM  2/5  Prepare a project-local virtual environment
REM ============================================================
set "VENV_DIR=%PROJECT_DIR%.venv"
set "VENV_PY=%VENV_DIR%\Scripts\python.exe"

if exist "%VENV_PY%" goto :venv_ready

echo [2/5] Creating virtual environment (first run only)...
%PYTHON_EXE% -m venv "%VENV_DIR%"
if not exist "%VENV_PY%" goto :venv_failed

:venv_ready
set PYTHON_EXE="%VENV_PY%"
echo [2/5] Virtual environment ready: %VENV_DIR%
echo.
goto :deps_check

:venv_failed
echo [2/5] [WARN] Could not create a virtual environment.
echo           Falling back to the system interpreter.
echo.

REM ============================================================
REM  3/5  Install / verify dependencies
REM ============================================================
:deps_check
REM Check every runtime dependency, not just one, so a half-installed
REM environment gets repaired instead of failing later at startup.
REM NOTE: no parenthesised blocks around %errorlevel% on purpose - it is
REM expanded when a block is parsed, not when it runs, which would make
REM the tests below always report a false result. Plain "goto" keeps
REM every %errorlevel% evaluated at execution time.
%PYTHON_EXE% -c "import fastapi, uvicorn, mss, PIL, httpx, yaml, websockets" >nul 2>&1
if %errorlevel% equ 0 goto :deps_ready

echo [3/5] Installing dependencies (first run only, may take a while)...
%PYTHON_EXE% -m pip install --upgrade pip -i https://pypi.tuna.tsinghua.edu.cn/simple >nul 2>&1
%PYTHON_EXE% -m pip install -r "%PROJECT_DIR%requirements.txt" -i https://pypi.tuna.tsinghua.edu.cn/simple
if %errorlevel% neq 0 goto :deps_retry
goto :deps_ready

:deps_retry
echo [3/5] Mirror failed, retrying with the default index...
%PYTHON_EXE% -m pip install -r "%PROJECT_DIR%requirements.txt"
if %errorlevel% neq 0 goto :deps_failed

:deps_ready
echo [3/5] Dependencies OK.
echo.
goto :config_check

:deps_failed
echo [3/5] [ERROR] Failed to install dependencies, please check your network.
pause
exit /b 1

REM ============================================================
REM  4/5  Create config.yaml from the template when missing
REM ============================================================
:config_check
if exist "%PROJECT_DIR%config.yaml" goto :config_ready

echo [4/5] config.yaml not found - creating it from the template...
copy /y "%PROJECT_DIR%config.example.yaml" "%PROJECT_DIR%config.yaml" >nul

:config_ready
findstr /c:"your-api-key-here" "%PROJECT_DIR%config.yaml" >nul 2>&1
if %errorlevel% neq 0 goto :config_ok

echo.
echo [4/5] [ACTION REQUIRED] config.yaml still has the placeholder api_key.
echo        Open config.yaml and fill in your own key, or set it later
echo        from the web UI on your phone.
echo.

:config_ok
echo [4/5] Config ready.
echo.

REM ============================================================
REM  5/5  Launch the service
REM ============================================================
echo [5/5] Starting service...
echo.
echo Open the URL printed below in your phone browser:
echo.

%PYTHON_EXE% main.py
if %errorlevel% neq 0 goto :run_failed

echo.
echo Service stopped.
pause
exit /b 0

:run_failed
echo.
echo [ERROR] The service exited with an error. See the messages above.
echo.
echo [HINT] The most common causes:
echo        - Port 8000 is already in use. Another SnapStudy instance
echo          may still be running (check the phone page, or Task
echo          Manager), or set server.port in config.yaml to a free port.
echo        - Invalid llm settings in config.yaml.
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
