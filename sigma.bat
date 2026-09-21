@echo off
REM ============================================================
REM  sigma launcher (Windows cmd)
REM
REM  Usage:
REM    double-click              -> interactive mode (workspace = current dir)
REM    sigma.bat "some task"     -> one-shot mode
REM    set SIGMA_NO_PAUSE=1      -> don't wait for a key at the end (for scripts)
REM
REM  Design notes:
REM    - This file ONLY picks the interpreter and forwards arguments.
REM      Every bit of logic lives in sigma.cli:main.
REM    - All text here is ASCII on purpose: cmd.exe reads .bat in the
REM      console's ANSI codepage (GBK on Chinese Windows), so UTF-8
REM      Chinese in this file turns into mojibake. Chinese output happens
REM      inside Python, which sets the console to UTF-8 itself.
REM    - "pause" at the end is the fix for the classic Windows problem:
REM      double-click -> window flashes and disappears before you read it.
REM      Scripts/CI can disable it with SIGMA_NO_PAUSE=1.
REM ============================================================

setlocal

set "SCRIPT_DIR=%~dp0"
set "VENV_PY=%SCRIPT_DIR%.venv\Scripts\python.exe"

if not exist "%VENV_PY%" (
    echo [harness error] venv python not found: %VENV_PY%
    echo Please run:  python -m venv .venv  ^&^&  python -m pip install -e ".[dev]"
    if not "%SIGMA_NO_PAUSE%"=="1" pause
    exit /b 2
)

REM  First arg starting with "-" means "forward everything to the CLI"
REM  (e.g. sigma.bat --workspace D:\code). Anything else is treated as the task.
set "FIRST=%~1"

if "%FIRST%"=="" (
    "%VENV_PY%" -m sigma
) else if "%FIRST:~0,1%"=="-" (
    "%VENV_PY%" -m sigma %*
) else (
    "%VENV_PY%" -m sigma -p "%~1"
)

REM  Preserve the CLI's exit code before anything else can clobber it:
REM  Q4 says non-zero = harness failure, and a wrapper that always returns 0
REM  silently turns "sigma crashed" into "everything is fine" for scripts/CI.
set "RC=%ERRORLEVEL%"
if not "%SIGMA_NO_PAUSE%"=="1" pause
exit /b %RC%
