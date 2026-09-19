@echo off
chcp 65001 >nul
title 肥鱼娘 App（仓库版 · 首启自动建环境）
setlocal EnableDelayedExpansion
set "ROOT=%~dp0"
set "ENGINE=%ROOT%libs\qq_bot_runtime"
set "RUNTIME_PY=%ENGINE%\runtime\python\python.exe"
set "VENV=%ENGINE%\venv"
set "FEIYU_QQ_BOT=%ENGINE%"

if not exist "%RUNTIME_PY%" (
  echo [ERROR] 找不到捆绑解释器：%RUNTIME_PY%
  echo   仓库 libs\qq_bot_runtime\runtime\python 缺失，请先恢复捆绑 Python（见 Release 的 feiyu_core 包）。
  pause
  exit /b 1
)

rem ---- 1) 确保 venv：首启用捆绑解释器创建；已存在则便携重写 home（幂等）----
if not exist "%VENV%\Scripts\python.exe" (
  echo [BOOT] 首次启动：用捆绑解释器创建虚拟环境 %VENV%
  "%RUNTIME_PY%" -m venv "%VENV%"
  if errorlevel 1 (
    echo [ERROR] 创建 venv 失败。
    pause
    exit /b 1
  )
) else (
  rem 便携：把 venv 的 home 指向捆绑解释器，换机器也能跑（幂等，逐行替换避免正则转义问题）
  "%RUNTIME_PY%" -c "import os; p=os.path.join(r'%VENV%','pyvenv.cfg'); hp=os.path.dirname(r'%RUNTIME_PY%'); out=[]; [out.append(('home = '+hp) if l.startswith('home') else l) for l in open(p,encoding='utf-8').read().splitlines()]; open(p,'w',encoding='utf-8').write(('\n'.join(out)+'\n'))"
)

rem ---- 2) 确保依赖：首启缺关键包则自动 pip 安装基础依赖（含 torch，可能数分钟）----
set "FEIYU_PY=%VENV%\Scripts\python.exe"
"%FEIYU_PY%" -c "import torch" >nul 2>&1
if errorlevel 1 (
  echo [BOOT] 首次启动：安装基础依赖（requirements.txt，含 torch 等重依赖，请耐心等待安装完成）...
  "%FEIYU_PY%" -m pip install -r "%ENGINE%\requirements.txt"
  if errorlevel 1 (
    echo [ERROR] 依赖安装失败（可能无网络或被墙）。可手动执行：
    echo   %FEIYU_PY% -m pip install -r %ENGINE%\requirements.txt
    echo 或让本机部署引擎 E:\qq_bot\venv 存在以复用其依赖。
    pause
    exit /b 1
  )
) else (
  echo [OK] 依赖已就绪（venv 已含 torch 等）
)

rem ---- 3) 启动 ----
cd /d "%ROOT%"
echo [INFO] 解释器：%FEIYU_PY%
echo [INFO] 智能体运行时：%FEIYU_QQ_BOT%
"%FEIYU_PY%" "%ROOT%app.py" --with-core %*
if errorlevel 1 pause
