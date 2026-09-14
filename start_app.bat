@echo off
chcp 65001 >nul
title 肥鱼娘 App（独立版·便携）
setlocal
set "RT=%~dp0libs\qq_bot_runtime"

rem ---- 便携自举：把 venv\pyvenv.cfg 的 home 重写为包内解释器（幂等） ----
if exist "%RT%\runtime\python\python.exe" (
  > "%RT%\venv\pyvenv.cfg" echo home = %RT%\runtime\python
  >> "%RT%\venv\pyvenv.cfg" echo include-system-site-packages = false
  >> "%RT%\venv\pyvenv.cfg" echo version = 3.13.14
)

rem ---- 探测 venv python（自举后必然可用；否则报错退出） ----
if exist "%RT%\venv\Scripts\python.exe" (
  set "FEIYU_PY=%RT%\venv\Scripts\python.exe"
  set "FEIYU_QQ_BOT=%RT%"
) else (
  echo [ERROR] 找不到运行环境：%RT%\venv\Scripts\python.exe
  echo 请确认便携包完整（libs\qq_bot_runtime 含 venv 与 runtime\python）。
  pause
  exit /b 1
)

set "PYTHONNET_RUNTIME=netfx"
rem 包内 ffmpeg/silk 优先
set "PATH=%RT%\tools\ffmpeg\ffmpeg-2026-05-28-git-7b46c6a2a3-full_build\bin;%RT%\tools\silk2mp3;%PATH%"

cd /d "%FEIYU_QQ_BOT%"
"%FEIYU_PY%" "%~dp0app.py" --with-core %*
if errorlevel 1 pause
