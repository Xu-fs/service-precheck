@echo off
chcp 65001 >nul
setlocal
title 政务易办 - 本地网页
pushd "%~dp0"
if errorlevel 1 goto path_error

set "PYTHONPATH=%CD%\src"
set "PYTHONUTF8=1"
if not exist ".venv\Scripts\python.exe" goto missing_environment

".venv\Scripts\python.exe" -X utf8 "scripts\start_web.py" %*
set "LAUNCH_EXIT_CODE=%ERRORLEVEL%"
if /i "%~1"=="--check" goto finish
if "%LAUNCH_EXIT_CODE%"=="0" goto finish
echo.
echo 启动失败，请查看上面的错误信息。
pause

:finish
popd
exit /b %LAUNCH_EXIT_CODE%

:missing_environment
echo 未找到项目的 Python 环境：.venv\Scripts\python.exe
echo 请先按 README.md 的“环境与运行”说明创建环境并安装依赖。
set "LAUNCH_EXIT_CODE=1"
if /i "%~1"=="--check" goto finish
pause
goto finish

:path_error
echo 无法打开项目目录，请检查启动文件所在的位置。
pause
exit /b 1
