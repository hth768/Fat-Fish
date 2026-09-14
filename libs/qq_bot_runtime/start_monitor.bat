>@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================
echo   肥鱼娘 监控 sidecar 启动（独立进程）
echo   看板: http://127.0.0.1:8770/
echo ============================================

set "PY=%~dp0venv\Scripts\python.exe"
if not exist "%PY%" (
    set "PY=python"
)

echo 启动监控 sidecar（聚合 bot 与各 sidecar 健康度）...
"%PY%" monitor_server.py --host 127.0.0.1 --port 8770 --memory-url http://127.0.0.1:8766

pause
