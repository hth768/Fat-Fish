@echo off
rem ============================================================
rem  可选：安装本地 TTS（VoxCPM2）依赖 —— 需要 NVIDIA 显卡
rem  包内已有 models\；此处创建 venv_vox 并装依赖（离线包优先）
rem ============================================================
chcp 65001 >nul
title 本地 TTS 依赖安装
setlocal
set "RT=%~dp0libs\qq_bot_runtime"
if not exist "%RT%\runtime\python\python.exe" (
  echo [ERROR] 找不到包内 Python：%RT%\runtime\python\python.exe
  pause & exit /b 1
)
if exist "%RT%\venv_vox\Scripts\python.exe" (
  echo 已安装过，跳过。如需重装先删除 libs\qq_bot_runtime\venv_vox 文件夹。
  pause & exit /b 0
)
echo [1/2] 创建 venv_vox ...
"%RT%\runtime\python\python.exe" -m venv "%RT%\venv_vox"
echo [2/2] 安装依赖（离线包优先，其次网络）...
if exist "%~dp0libs\offline_deps_vox.zip" (
  "%RT%\venv\Scripts\python.exe" "%~dp0_bridge_unpack.py" "%~dp0libs\offline_deps_vox.zip" "%RT%\venv_vox\Lib\site-packages"
) else (
  "%RT%\venv_vox\Scripts\python.exe" -m pip install -r "%RT%\requirements-vox.txt" --disable-pip-version-check
)
if errorlevel 1 (
  echo [FAIL] 安装失败，请检查网络后重试。
) else (
  echo [OK] 本地 TTS 依赖就绪。到 App「插件」页启用 vox_tts 即可。
)
pause
