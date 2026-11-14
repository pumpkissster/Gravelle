@echo off
rem Start Gravelle from source with local network access enabled.
call "%~dp0start-windows.cmd" --lan
exit /b %errorlevel%
