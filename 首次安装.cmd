@echo off
setlocal
chcp 65001 >nul
pushd "%~dp0"
if errorlevel 1 goto folder_error
echo [Install] Preparing the project environment...
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup_team.ps1"
set "RESULT=%ERRORLEVEL%"
echo.
if not "%RESULT%"=="0" echo [Install] Installation failed. Check the message above and install.log.
popd
echo [Install] This window will stay open until you press a key.
pause
exit /b %RESULT%

:folder_error
echo [Install] Cannot access the extracted project folder. Extract the entire ZIP to a local folder.
pause
exit /b 1
