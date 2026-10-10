@echo off
setlocal
chcp 65001 >nul
pushd "%~dp0"
if errorlevel 1 goto folder_error
if not exist ".venv\Scripts\python.exe" goto not_installed
if not exist "app.py" goto missing_app
echo [Start] Opening http://localhost:8501 ...
".venv\Scripts\python.exe" -m streamlit run app.py --server.headless false --server.address 127.0.0.1 --server.port 8501 --server.maxUploadSize 30 --browser.gatherUsageStats false
set "RESULT=%ERRORLEVEL%"
echo.
if not "%RESULT%"=="0" echo [Start] Startup failed. Check the message above.
popd
echo [Start] This window will stay open until you press a key.
pause
exit /b %RESULT%

:not_installed
echo [Start] Project environment is missing. Run the first-install CMD in this folder first.
popd
pause
exit /b 1

:missing_app
echo [Start] app.py is missing. Extract the entire ZIP before running the project.
popd
pause
exit /b 1

:folder_error
echo [Start] Cannot access the extracted project folder. Extract the entire ZIP to a local folder.
pause
exit /b 1
