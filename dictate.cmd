@echo off
rem dictate launcher -- starts the tray app hidden (pythonw), unless it is
rem already running. Safe to double-click, put in shell:startup, or run from
rem a shortcut; does nothing but print a message if dictate is already up.
setlocal

set "REPO=%~dp0"
if "%REPO:~-1%"=="\" set "REPO=%REPO:~0,-1%"

powershell -NoProfile -Command ^
  "$p = Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe'\" -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -like '*dictate.app*' }; if ($p) { exit 1 } else { exit 0 }"

if %ERRORLEVEL% EQU 1 (
    echo dictate is already running.
    exit /b 0
)

start "dictate" /D "%REPO%" pythonw -m dictate.app
endlocal
