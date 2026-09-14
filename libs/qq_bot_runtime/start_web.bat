>@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 肥鱼娘 Web 控制台

echo ============================================
echo   肥鱼娘 . Web 控制台
echo   地址: http://127.0.0.1:8800
echo   关闭此窗口即停止服务
echo ============================================

REM 优先使用项目 venv 里的 Python
set "PY=%~dp0venv\Scripts\python.exe"
if not exist "%PY%" (
    set "PY=python"
)

"%PY%" --version >nul 2>&1
if errorlevel 1 (
    echo [错误] 未找到 Python，请先安装并加入 PATH。
    pause
    exit /b 1
)

REM 前台运行：bat 阻塞在 Python 上，日志直接可见；Python 退出后 pause 保活，绝不闪退。
REM 参数原样透传，例如：start_web.bat --no-core  或  start_web.bat --port 9000
echo [启动中] 稍候浏览器会自动打开...
"%PY%" web_plugin.py --port 8800 %*

echo.
echo [已退出] 若未正常打开网页，请查看上方报错（或 webui_run.log）。
pause
