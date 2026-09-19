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

rem ---- 1) 选择解释器：优先复用本机部署引擎的已建 venv（含完整依赖），否则自建 ----
if exist "%DEPLOY_VENV%\Scripts\python.exe" (
  "%DEPLOY_VENV%\Scripts\python.exe" -c "import webview" >nul 2>&1
  if not errorlevel 1 (
    set "FEIYU_PY=%DEPLOY_VENV%\Scripts\python.exe"
    set "REUSE=1"
    echo [OK] 复用本机部署引擎依赖：%FEIYU_PY%（跳过自建 venv）
    goto :deps_ready
  )
)

rem ---- 2) 确保自建 venv：首启用捆绑解释器创建；已存在则便携重写 home（幂等）----
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
set "FEIYU_PY=%VENV%\Scripts\python.exe"

rem ---- 2.5) 确保依赖：缺关键包则自动 pip 安装 CPU 友好依赖（含 pywebview，不绑定 CUDA），带重试 ----
"%FEIYU_PY%" -c "import webview" >nul 2>&1
if errorlevel 1 (
  echo [BOOT] 首次启动：安装 CPU 友好依赖（requirements-app.txt，含 pywebview 原生窗口，torch 为 CPU 版，请耐心等待）...
  set "INSTALL_OK=0"
  for /L %%i in (1,1,3) do (
    echo [BOOT] 依赖安装尝试 %%i/3 ...
    "%FEIYU_PY%" -m pip install --retries 5 --timeout 60 -r "%ENGINE%\requirements-app.txt"
    if not errorlevel 1 (
      set "INSTALL_OK=1"
      goto :install_done
    )
    echo [WARN] 第 %%i 次安装失败（源偶发不稳定），稍后重试...
    timeout /t 5 >nul
  )
  :install_done
  if "!INSTALL_OK!"=="0" (
    rem 自建失败：若本机部署引擎 venv 可用，则回退复用
    if exist "%DEPLOY_VENV%\Scripts\python.exe" (
      "%DEPLOY_VENV%\Scripts\python.exe" -c "import webview" >nul 2>&1
      if not errorlevel 1 (
        set "FEIYU_PY=%DEPLOY_VENV%\Scripts\python.exe"
        set "REUSE=1"
        echo [WARN] 自建依赖失败，已回退复用本机部署引擎依赖：%FEIYU_PY%
        goto :deps_ready
      )
    )
    echo [ERROR] 依赖安装失败（可能无网络或被墙），且无可复用的本机部署引擎 venv。可手动执行：
    echo   %VENV%\Scripts\python.exe -m pip install -r %ENGINE%\requirements-app.txt
    pause
    exit /b 1
  )
) else (
  echo [OK] 依赖已就绪（venv 已含 pywebview 等）
)

:deps_ready

rem ---- 3) N 卡检测：仅自建成 CPU 版 venv 时，询问是否安装 CUDA 版 torch 以加速本地模型 ----
if "!REUSE!"=="0" (
  nvidia-smi >nul 2>&1
  if not errorlevel 1 (
    echo [GPU] 检测到 NVIDIA 显卡。
    set "CHOICE=N"
    set /p CHOICE="是否安装 CUDA 版 torch 加速本地模型？(Y/N，回车默认 N): "
    if /i "!CHOICE!"=="Y" (
      echo [BOOT] 安装 CUDA 版 torch（cu128，可能数分钟）...
      "%FEIYU_PY%" -m pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu128
      if errorlevel 1 (
        echo [WARN] CUDA 版 torch 安装失败，保持 CPU 版（不影响启动）。
      ) else (
        echo [OK] 已切换为 CUDA 版 torch。
      )
    ) else (
      echo [INFO] 保持 CPU 版 torch（如需加速可手动执行：pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu128）。
    )
  )
)

rem ---- 4) 启动 ----
cd /d "%ROOT%"
echo [INFO] 解释器：%FEIYU_PY%
echo [INFO] 智能体运行时：%FEIYU_QQ_BOT%
"%FEIYU_PY%" "%ROOT%app.py" --with-core %*
if errorlevel 1 pause
