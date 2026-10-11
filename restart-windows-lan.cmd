@echo off
call "%~dp0restart-windows.cmd" -Lan
exit /b %errorlevel%
