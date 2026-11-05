@echo off
setlocal DisableDelayedExpansion
cd /d "%~dp0" || exit /b 1
if "%~1"=="" goto launch
if /i "%~1"=="--check" goto check
echo Usage: start.cmd [--check] 1>&2
exit /b 2
:check
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "scripts\start_runtime.ps1" -Check
exit /b %errorlevel%
:launch
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "scripts\start_runtime.ps1"
exit /b %errorlevel%
