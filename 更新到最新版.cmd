@echo off
chcp 65001 >nul
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0update_team.ps1"
set "UPDATE_EXIT_CODE=%ERRORLEVEL%"
echo.
pause
exit /b %UPDATE_EXIT_CODE%
