@echo off
chcp 65001 >nul
title 肥鱼娘 App（仓库版）
setlocal
rem ---- 智能体运行时固定指向仓库自带的引擎 ----
set "FEIYU_QQ_BOT=%~dp0libs\qq_bot_runtime"
cd /d "%~dp0"

rem ---- 选择带依赖的 Python（优先仓库自带的 venv，其次本机部署引擎 venv，再次捆绑裸解释器）----
if exist "%~dp0libs\qq_bot_runtime\venv\Scripts\python.exe" (
  set "FEIYU_PY=%~dp0libs\qq_bot_runtime\venv\Scripts\python.exe"
) else if exist "E:\qq_bot\venv\Scripts\python.exe" (
  set "FEIYU_PY=E:\qq_bot\venv\Scripts\python.exe"
) else if exist "%~dp0libs\qq_bot_runtime\runtime\python\python.exe" (
  set "FEIYU_PY=%~dp0libs\qq_bot_runtime\runtime\python\python.exe"
) else (
  echo [ERROR] 找不到可用的 Python：
  echo   1^) 在 libs\qq_bot_runtime\venv 建虚拟环境并安装依赖（见 README「快速开始」）；或
  echo   2^) 确保本机部署引擎 E:\qq_bot\venv 存在（复用其解释器与依赖）。
  pause
  exit /b 1
)

echo [INFO] 解释器：%FEIYU_PY%
echo [INFO] 智能体运行时：%FEIYU_QQ_BOT%
"%FEIYU_PY%" "%~dp0app.py" --with-core %*
if errorlevel 1 pause
