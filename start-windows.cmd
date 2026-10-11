@echo off
setlocal
cd /d "%~dp0"

py -3 -c "import sys; raise SystemExit(sys.version_info < (3, 12))" >nul 2>nul
if not errorlevel 1 (
    py -3 "%~dp0start_windows.py" %*
    goto finished
)

python -c "import sys; raise SystemExit(sys.version_info < (3, 12))" >nul 2>nul
if not errorlevel 1 (
    python "%~dp0start_windows.py" %*
    goto finished
)

echo A working Python 3.12 or newer was not found.
echo Install Python from https://www.python.org/downloads/windows/
echo Then double-click this file again.
pause
exit /b 1

:finished
if errorlevel 1 (
    echo.
    echo The reader stopped with an error. See the message above.
    echo If port 8080 is in use, stop the other reader before trying again.
    pause
    exit /b 1
)
endlocal
