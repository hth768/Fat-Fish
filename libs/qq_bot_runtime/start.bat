@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 启动肥鱼娘（Web 控制台 + 核心 + 记忆/监控服务）...
"%~dp0venv\Scripts\python.exe" "%~dp0start.py"
pause
