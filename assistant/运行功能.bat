@echo off
cd /d "%~dp0"

rem ============================================================
rem BetterGI remote assistant entry.
rem All features live in launcher.py; this file only starts it.
rem You can also run: python launcher.py
rem With arguments:    python launcher.py --status
rem ============================================================

set "PY="
where python >nul 2>nul && set "PY=python"
if not defined PY (
    where py >nul 2>nul && set "PY=py"
)

if not defined PY (
    echo.
    echo  [ERROR] Python not found.
    echo  Please install Python 3.9+ and check "Add Python to PATH":
    echo  https://www.python.org/downloads/
    echo.
    pause
    exit /b 1
)

%PY% "%~dp0launcher.py" %*
if errorlevel 1 pause
