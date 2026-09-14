# -*- coding: utf-8 -*-
"""venv_vox（本地 VoxCPM2 TTS）后台就绪。

由 _launcher.py 以分离子进程拉起（DETACHED，无窗口零交互），
从始至终与主 venv / App 启动并行，任何失败只记日志绝不影响主 App。

四级自举（前三步秒级完成）：
1. qq_bot_runtime/venv_vox 已就绪（python.exe 在）  -> 什么都不做
2. libs/voice_pack/venv_vox 存在                    -> 建 junction（即用环境秒链接）
3. libs/offline_deps_vox.zip 存在（旧包布局）        -> 解压 site-packages
4. 都没有                                           -> pip 在线 requirements-vox.txt

失败写 .setup_vox_failed，24h 内不重试。日志：data/vox_setup.log。
"""
import os
import subprocess
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(BASE, "data", "vox_setup.log")
FAIL_FILE = os.path.join(BASE, "libs", "qq_bot_runtime", ".setup_vox_failed")
FAIL_RETRY_SECONDS = 24 * 3600


def _log(msg: str):
    print(f"[vox] {msg}", flush=True)


def _mklink_j(dst: str, src: str) -> bool:
    """建目录 junction（cmd mklink /J，已存在视为成功）。"""
    if os.path.isdir(dst):
        return True
    try:
        r = subprocess.run(["cmd", "/c", "mklink", "/J", dst, src],
                           capture_output=True, text=True, timeout=30,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return os.path.isdir(dst)
    except Exception:
        return False


def _recently_failed() -> bool:
    try:
        return (time.time() - os.path.getmtime(FAIL_FILE)) < FAIL_RETRY_SECONDS
    except OSError:
        return False


def main() -> int:
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    log = open(LOG_PATH, "a", buffering=1, encoding="utf-8", errors="replace")
    sys.stdout = sys.stderr = log
    _log(f"setup start {time.strftime('%Y-%m-%d %H:%M:%S')}")

    rt = sys.argv[1] if len(sys.argv) > 1 else os.path.join(BASE, "libs", "qq_bot_runtime")
    libs = os.path.dirname(rt)
    try:
        import _launcher
        if _recently_failed():
            _log("recent failure recorded, skip this round (retry after 24h)")
            return 1

        dst_venv = os.path.join(rt, "venv_vox")
        dst_py = os.path.join(dst_venv, "Scripts", "python.exe")

        # 1) 已就绪 -> 秒退（常态路径）
        if os.path.isfile(dst_py):
            _log("venv_vox ready, nothing to do")
            return 0

        # 2) voice_pack 即用环境 -> junction 秒链接（主路径）
        vp_venv = os.path.join(libs, "voice_pack", "venv_vox")
        if os.path.isfile(os.path.join(vp_venv, "Scripts", "python.exe")):
            if _mklink_j(dst_venv, vp_venv):
                _log(f"junction ready: {dst_venv} -> {vp_venv}")
                return 0
            _log("mklink failed (need admin?) fallback to zip/pip")

        # 3) 旧包布局：离线 zip 解压
        zip_file = os.path.join(libs, "offline_deps_vox.zip")
        if os.path.isfile(zip_file):
            py = _launcher.ensure_pyvenv(rt, "venv_vox")
            if not py:
                _log("base python missing, abort")
                return 1
            ok = _launcher.setup_deps(py, rt,
                                      os.path.join(rt, "requirements-vox.txt"),
                                      zip_file, "vox", venv_name="venv_vox",
                                      show_progress=True)
            _log(f"zip/pip result: {ok}")
            if ok:
                try:
                    os.remove(FAIL_FILE)
                except OSError:
                    pass
                return 0
            open(FAIL_FILE, "w").write(time.strftime("%Y-%m-%d %H:%M:%S") + "\n")
            return 1

        # 4) 无任何本地源：pip 在线（仅剩的兜底，耗时较长）
        py = _launcher.ensure_pyvenv(rt, "venv_vox")
        if not py:
            _log("base python missing, abort")
            return 1
        ok = _launcher.setup_deps(py, rt,
                                  os.path.join(rt, "requirements-vox.txt"),
                                  "", "vox", venv_name="venv_vox",
                                  show_progress=True)
        _log(f"pip result: {ok}")
        if ok:
            try:
                os.remove(FAIL_FILE)
            except OSError:
                pass
            return 0
        open(FAIL_FILE, "w").write(time.strftime("%Y-%m-%d %H:%M:%S") + "\n")
        return 1
    except Exception as e:
        _log(f"ERROR {e!r}")
        try:
            open(FAIL_FILE, "w").write(time.strftime("%Y-%m-%d %H:%M:%S") + "\n")
        except OSError:
            pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
