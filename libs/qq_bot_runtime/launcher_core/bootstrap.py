# -*- coding: utf-8 -*-
"""启动器环境自愈内核（对齐 N.E.K.O launcher_core.bootstrap）。

在真正拉起业务进程前，先把运行环境收拾干净：
  - Windows 标准流 UTF-8 归一（避免中文日志被 cp936 截断崩溃）；
  - 必要时置 PYTHONUTF8 并通过 os.execv 自我替换修复文件系统编码
    （带一次性防护标记，避免死循环）；
  - 项目 .venv 切换（仅由门面在 __main__ 下触发，import 时不会重拉进程）。

无副作用的 `configure_runtime_env()` 可安全重复调用 / 在测试里 import。
"""
import os
import sys
from quiet import degrade


def configure_runtime_env():
    """环境变量归一：stdio UTF-8 + PYTHONUTF8。可安全重复调用。"""
    _configure_stdio_utf8()
    _ensure_utf8_fs()


def _configure_stdio_utf8():
    """Windows 下把 stdout/stderr 设为 UTF-8，避免打印中文被截断。"""
    if os.name == "nt":
        for s in (sys.stdout, sys.stderr):
            try:
                if hasattr(s, "reconfigure"):
                    s.reconfigure(encoding="utf-8", errors="replace")
            except Exception as e:
                degrade("libs/qq_bot_runtime/launcher_core/bootstrap.py:29 _configure_stdio_utf8", e, "降级：if hasattr(s, 'reconfigure')")


def _ensure_utf8_fs():
    """文件系统编码非 UTF-8 时，置 PYTHONUTF8=1 并自我替换一次。"""
    enc = sys.getfilesystemencoding()
    if enc and enc.lower().replace("-", "") not in ("utf8", "utf"):
        if os.environ.get("_QQBOT_FS_UTF8_REEXEC") == "1":
            return  # 已替换过，放行，防死循环
        os.environ["PYTHONUTF8"] = "1"
        os.environ["_QQBOT_FS_UTF8_REEXEC"] = "1"
        try:
            os.execv(sys.executable, [sys.executable] + sys.argv)
        except OSError as e:
            degrade("libs/qq_bot_runtime/launcher_core/bootstrap.py:43 _ensure_utf8_fs", e, "降级：os.execv(sys.executable, [sys.executable] + sys.ar")


def reexec_into_venv():
    """若当前解释器不是项目 .venv，则替换为 .venv 内的 python 重新执行。

    仅在门面 launcher.py 的 __main__ 下调用；import 本模块时不会触发，
    以免误杀测试 / IDE 进程。
    """
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(here)
    venv_py = (os.path.join(root, ".venv", "Scripts", "python.exe") if os.name == "nt"
               else os.path.join(root, ".venv", "bin", "python"))
    if (os.path.isfile(venv_py)
            and os.path.abspath(sys.executable) != os.path.abspath(venv_py)):
        os.execv(venv_py, [venv_py] + sys.argv)
