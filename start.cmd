@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    python -m venv .venv
    if errorlevel 1 exit /b 1
)
".venv\Scripts\python.exe" -m pip --disable-pip-version-check install -r requirements.txt
if errorlevel 1 exit /b 1
".venv\Scripts\python.exe" run.py
