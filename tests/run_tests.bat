@echo off
rem 运行构建助手 / 插件协议的核心逻辑测试（纯标准库，无需安装 pytest）
rem 用法：双击，或在仓库根目录执行 tests\run_tests.bat
setlocal
cd /d "%~dp0.."

set PY=python
if exist "libs\qq_bot_runtime\runtime\python\python.exe" set PY=libs\qq_bot_runtime\runtime\python\python.exe
if exist "E:\qq_bot\venv\Scripts\python.exe" set PY=E:\qq_bot\venv\Scripts\python.exe

echo [tests] 使用解释器: %PY%
"%PY%" -m unittest discover -s tests -p "test_*.py" -v
set RC=%ERRORLEVEL%
echo.
if "%RC%"=="0" (echo [tests] 全部通过) else (echo [tests] 有失败，退出码 %RC%)
pause
exit /b %RC%
