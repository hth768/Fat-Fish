@echo off
chcp 65001 >nul
title 肥鱼娘 App（仓库版 · 首启自动建环境）
setlocal EnableDelayedExpansion
set "ROOT=%~dp0"
set "ENGINE=%ROOT%libs\qq_bot_runtime"
set "RUNTIME_PY=%ENGINE%\runtime\python\python.exe"
set "VENV=%ENGINE%\venv"
set "DEPLOY_VENV=E:\qq_bot\venv"
set "FEIYU_QQ_BOT=%ENGINE%"
set "REUSE=0"

if not exist "%RUNTIME_PY%" (
  echo [ERROR] 找不到捆绑解释器：%RUNTIME_PY%
  echo   仓库 libs\qq_bot_runtime\runtime\python 缺失，请先恢复捆绑 Python（见 Release 的 feiyu_core 包）。
  pause
  exit /b 1
)

rem ---- 1) 优先复用本机部署引擎的已建 venv（含完整依赖），日常最快路径 ----
if exist "%DEPLOY_VENV%\Scripts\python.exe" (
  "%DEPLOY_VENV%\Scripts\python.exe" -c "import webview" >nul 2>&1
  if not errorlevel 1 (
    set "FEIYU_PY=%DEPLOY_VENV%\Scripts\python.exe"
    set "REUSE=1"
    echo [OK] 复用本机部署引擎依赖：%FEIYU_PY%（跳过自建 venv）
    goto :deps_ready
  )
)

rem ---- 2) 本地自建 venv 已就绪（含 pywebview）：日常启动，直接进 UI，不弹窗 ----
if exist "%VENV%\Scripts\python.exe" (
  "%VENV%\Scripts\python.exe" -c "import webview" >nul 2>&1
  if not errorlevel 1 (
    set "FEIYU_PY=%VENV%\Scripts\python.exe"
    echo [OK] 使用本地自建 venv：%FEIYU_PY%
    goto :deps_ready
  )
)

rem ---- 3) 真正首启：venv 缺失或缺 pywebview → 交给 firstboot.py（弹 HTML 进度窗 + 后台建环境 + 装完拉起 app）----
echo [BOOT] 首次启动：用捆绑解释器搭建运行环境（将弹出进度窗，后台安装最小 UI 依赖；本地 AI 依赖可稍后按需装）...
"%RUNTIME_PY%" "%ENGINE%\firstboot.py" --engine "%ENGINE%" --venv "%VENV%" --rpy "%RUNTIME_PY%" --req "%ENGINE%\requirements-ui.txt" --app "%ROOT%app.py" --progress-port 8910 --app-port 8900 %*
if errorlevel 1 (
  echo [ERROR] 首启引导失败。可手动执行：
  echo   %VENV%\Scripts\python.exe -m pip install -r %ENGINE%\requirements-app.txt
  pause
)
exit /b 0

:deps_ready

rem ---- 启动主界面（原生窗口 → Edge → 浏览器 三级降级，由 app.py 内部处理）----
cd /d "%ROOT%"
echo [INFO] 解释器：%FEIYU_PY%
echo [INFO] 智能体运行时：%FEIYU_QQ_BOT%
"%FEIYU_PY%" "%ROOT%app.py" --with-core %*
if errorlevel 1 pause
