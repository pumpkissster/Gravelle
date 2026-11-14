@echo off
rem Run the PowerShell helper to restart the local Gravelle source preview.
setlocal
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0restart-windows.ps1" %*
if errorlevel 1 pause
endlocal
