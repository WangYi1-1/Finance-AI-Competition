@echo off
setlocal
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
pushd "%~dp0"
if errorlevel 1 goto folder_error
if not exist ".venv\Scripts\python.exe" goto not_installed
echo 正在检查并发布 GitHub 更新，请保留此窗口。
".venv\Scripts\python.exe" scripts\publish_cloud_update.py
set "RESULT=%ERRORLEVEL%"
echo.
if not "%RESULT%"=="0" echo 发布未完成，请把上面的提示发回核对。
popd
pause
exit /b %RESULT%

:not_installed
echo 找不到项目环境，请先运行首次安装.cmd。
popd
pause
exit /b 1

:folder_error
echo 无法打开项目文件夹，请确认完整下载了项目。
pause
exit /b 1
