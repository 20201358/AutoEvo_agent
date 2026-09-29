@echo off
REM v3.cmd - AutoEvo_agent v3 CLI launcher (Windows)
REM
REM This command makes v3 agent available from any terminal directory
REM It automatically uses the project's venv Python to avoid conflicts
REM
REM Works from ANY directory: keeps the caller's current directory
REM (agent starts where you are) and injects the repo root into
REM PYTHONPATH so the v3 package resolves.
REM
REM Usage from any directory:
REM     v3                       open REPL (resume or new)
REM     v3 --new "project A"     new conversation
REM     v3 --open <conv_id>      open specific conversation
REM     v3 --list                list recent conversations
REM     v3 --once "task"        single-turn mode
REM     v3 --help                show CLI help

setlocal

set "HERE=%~dp0"
set "VENV_PY=%HERE%.venv\Scripts\python.exe"

if not exist "%VENV_PY%" (
    echo [v3] ERROR: venv interpreter not found: %VENV_PY%
    echo [v3] Please create the venv first:
    echo [v3]     cd /d "%HERE%"
    echo [v3]     python -m venv .venv
    echo [v3]     .venv\Scripts\python.exe -m pip install -e .
    exit /b 1
)

REM DO NOT cd to the project root here - the whole point is to keep
REM the user's current directory as the agent working directory.
if defined PYTHONPATH (
    set "PYTHONPATH=%HERE%;%PYTHONPATH%"
) else (
    set "PYTHONPATH=%HERE%"
)

"%VENV_PY%" -m v3.cli %*

endlocal
