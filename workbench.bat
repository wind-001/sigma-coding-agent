@echo off
REM ============================================================
REM  sigma workbench launcher (Windows cmd)
REM
REM  Usage:
REM    double-click              -> workbench server on 127.0.0.1:8301
REM                                 + auto-open the default browser
REM    workbench.bat --port 8302 -> forward extra args to the server
REM                                 (auto-open assumes the default port)
REM
REM  Design notes (same rules as sigma.bat):
REM    - ASCII only on purpose: cmd.exe reads .bat in the console's
REM      ANSI codepage (GBK on Chinese Windows), so UTF-8 Chinese in
REM      this file turns into mojibake.
REM    - This file ONLY picks the interpreter and forwards arguments.
REM      All server logic lives in workbench_server.py:main.
REM    - The server runs in this window; Ctrl+C or closing the window
REM      stops it. "pause" at the end keeps errors readable when
REM      double-clicked (disable with SIGMA_NO_PAUSE=1, like sigma.bat).
REM ============================================================

setlocal

set "SCRIPT_DIR=%~dp0"
set "VENV_PY=%SCRIPT_DIR%.venv\Scripts\python.exe"
set "SERVER=%SCRIPT_DIR%src\sigma-frontend\server\workbench_server.py"
set "URL=http://127.0.0.1:8301"

if not exist "%VENV_PY%" (
    echo [harness error] venv python not found: %VENV_PY%
    echo Please run:  python -m venv .venv  ^&^&  python -m pip install -e ".[dev]"
    if not "%SIGMA_NO_PAUSE%"=="1" pause
    exit /b 2
)

REM  Auto-open the browser (default port only) after a short delay so the
REM  server can bind first. Skipped when args are forwarded.
if "%~1"=="" (
    start "" cmd /c "timeout /t 2 /nobreak >nul & start %URL%"
)

echo Starting sigma workbench on %URL%  (Ctrl+C to stop)
"%VENV_PY%" "%SERVER%" %*

set "RC=%ERRORLEVEL%"
if not "%SIGMA_NO_PAUSE%"=="1" pause
exit /b %RC%
