>@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================
echo   肥鱼娘 记忆 sidecar 启动（独立进程）
echo ============================================

REM 优先使用项目 venv 里的 Python
set "PY=%~dp0venv\Scripts\python.exe"
if not exist "%PY%" (
    set "PY=python"
)

echo 启动记忆 sidecar（vector_memory 等重负载剥离到子进程）...
"%PY%" memory_server.py --host 127.0.0.1 --port 8766 --modules vector_memory

pause
