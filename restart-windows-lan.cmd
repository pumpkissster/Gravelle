@echo off
rem Restart the Gravelle source preview with local network access enabled.
call "%~dp0restart-windows.cmd" -Lan
exit /b %errorlevel%
