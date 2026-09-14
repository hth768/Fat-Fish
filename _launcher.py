# -*- coding: utf-8 -*-
"""肥鱼娘 App 启动器（FeiyuApp.exe）——便携版（带首启进度窗口）。

包体不含 venv（省 10GB）。首次启动自动配置：
1. 主 venv（core）：原生进度窗口——虚拟环境创建、解压百分比、剩余时间预估、
   可取消。优先解压 libs/offline_deps_core.zip（2~3 分钟，零网络）；
   无 zip 时回退 pip 在线安装（requirements-portable.txt，30~90 分钟）。
2. venv_vox（本地 TTS，可选）：从始至终静默后台——分离子进程 _vox_setup.py，
   不阻塞、无窗口、失败只记日志；装好后 vox_tts 插件即可用，
   没装好时插件自动降级 GLM 云端 TTS。

引擎与界面逻辑在 libs/qq_bot_runtime；exe 是不依赖 venv 的点火钥匙。
"""
import os
import subprocess
import sys
import time

DONE_FILE = ".setup_{tag}_complete"
PROBES = {"core": ["torch", "httpx", "webview"],
          "vox": ["voxcpm", "librosa"]}


def base_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def alert(msg: str):
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, msg, "肥鱼娘 App", 0x40)
    except Exception:
        pass


def ensure_pyvenv(rt: str, name: str = "venv"):
    """venv 不存在则现场创建（home 自然指向当时的 runtime\\python，天然便携）。"""
    venv_dir = os.path.join(rt, name)
    py = os.path.join(venv_dir, "Scripts", "python.exe")
    if os.path.isfile(py):
        return py
    base_py = os.path.join(rt, "runtime", "python", "python.exe")
    if not os.path.isfile(base_py):
        return None
    # 现场创建（pip 由 ensurepip 自带）
    subprocess.run([base_py, "-m", "venv", venv_dir],
                   capture_output=True, timeout=300,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return py if os.path.isfile(py) else None


def setup_deps(py: str, rt: str, req_file: str, zip_file: str, tag: str,
               venv_name: str = "venv", show_progress: bool = False,
               progress=None, cancel=None) -> bool:
    """依赖三级自举：解压离线 zip → pip 在线 → 失败。幂等（有标记即跳过）。

    progress(dict)：阶段事件回调（venv 由调用方发，这里发 unpack/pip）；
    cancel：threading.Event，解压循环感知后返回 False。
    完成判定用代表包探测（torch/httpx/webview 任一缺失即重装，解压幂等覆盖），
    标记仅作缓存——用户删了 venv 留下标记也能自愈重装。
    """
    site = os.path.join(rt, venv_name, "Lib", "site-packages")
    done = os.path.join(rt, DONE_FILE.format(tag=tag))
    probes = PROBES.get(tag, [])
    probes_ok = bool(probes) and all(os.path.isdir(os.path.join(site, p)) for p in probes)
    if os.path.isfile(done):
        if probes_ok or not probes:
            return True
        try:  # 标记在但代表包缺失（如手动删了 venv）：自愈重装
            os.remove(done)
        except Exception:
            pass

    # 1) 离线 zip 优先（带真实字节进度与 ETA）
    if os.path.isfile(zip_file):
        try:
            import zipfile
            os.makedirs(site, exist_ok=True)
            with zipfile.ZipFile(zip_file) as zf:
                infos = zf.infolist()
                n = len(infos)
                total_bytes = sum(i.file_size for i in infos)
                done_bytes = 0
                t0 = time.time()
                last = 0.0
                for i, info in enumerate(infos):
                    if cancel is not None and cancel.is_set():
                        print(f"[{tag}] cancelled", flush=True)
                        return False
                    zf.extract(info, site)
                    done_bytes += info.file_size
                    now = time.time()
                    if progress and (now - last >= 0.15 or i == n - 1):
                        last = now
                        elapsed = now - t0
                        eta = (elapsed / done_bytes * (total_bytes - done_bytes)
                               if done_bytes else 0)
                        progress({"stage": "unpack", "tag": tag, "items": i + 1,
                                  "total_items": n, "bytes": done_bytes,
                                  "total_bytes": total_bytes, "elapsed": elapsed,
                                  "eta": eta})
                    if show_progress and i % 4000 == 0:
                        print(f"[{tag}] unpack {i}/{n}", flush=True)
            open(done, "w").write("from-offline-zip\n")
            return True
        except Exception as e:
            print(f"[{tag}] 离线包解压失败: {e!r}，转 pip 在线安装", flush=True)

    # 2) pip 在线安装
    if os.path.isfile(req_file):
        if progress:
            progress({"stage": "pip", "tag": tag})
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        r = subprocess.run(
            [py, "-m", "pip", "install", "-r", req_file,
             "--disable-pip-version-check"],
            capture_output=not show_progress, timeout=7200,
            creationflags=creationflags, cwd=rt)
        if r.returncode == 0:
            open(done, "w").write("from-pip\n")
            return True
        return False
    return False


# 可选大件 pack 分离（分开下载）：实体可在包内 libs/<pack>/ 或包外 <盘>:\plugins\<pack>，
# 引擎原位建 junction。缺 pack 时插件自动降级（vox->GLM 云端 / MC 报依赖缺失 /
# 语音转码不可用），不阻塞启动。
PACK_LINKS = [
    ("voice_pack", "models"), ("voice_pack", "venv_vox"),
    ("mc_pack", "mc_bot"), ("mc_pack", "mc_mod"), ("mc_pack", "_mc_ref"),
    ("tools_pack", "tools"), ("vl_pack", "hf_cache"),
]


def _mklink_j(dst: str, src: str) -> bool:
    try:
        subprocess.run(["cmd", "/c", "mklink", "/J", dst, src],
                       capture_output=True, text=True, timeout=30,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return os.path.isdir(dst)
    except Exception:
        return False


def _pack_candidates(base: str, libs: str, pack: str):
    """pack 实体的候选位置（优先级从高到低）：包内 libs/ -> 包外 <盘>:/plugins。

    注意 splitdrive 返回 "E:" 不带反斜杠，join 前必须补齐（否则生成
    "E:plugins/..." 相对路径，isdir 永远 False）。
    """
    drive = os.path.splitdrive(base)[0] + "\\"
    return [os.path.join(libs, pack), os.path.join(drive, "plugins", pack)]


def ensure_packs(base: str, rt: str):
    """兼容旧布局：包内 libs/<pack>/ 存在则接线（解压即装）。

    包外 <盘>:\\plugins 仓库不再自动接线——插件与资源的安装统一由
    App「插件页 → 插件市场」在 UI 内完成（bridge/plugins_api.market_action）。
    """
    libs = os.path.dirname(rt)
    for pack, name in PACK_LINKS:
        dst = os.path.join(rt, name)
        if os.path.isdir(dst):
            continue
        src = os.path.join(libs, pack, name)
        if os.path.isdir(src):
            if _mklink_j(dst, src):
                print(f"[PKG] pack 已接线: {name} -> {src}", flush=True)


def spawn_app(base: str, rt: str):
    """用 venv 解释器拉起 app.py（独立进程，启动器退出不影响）。"""
    env = dict(os.environ)
    env["FEIYU_QQ_BOT"] = rt
    env["PYTHONNET_RUNTIME"] = "netfx"
    tools_bin = os.path.join(rt, "tools", "ffmpeg",
                             "ffmpeg-2026-05-28-git-7b46c6a2a3-full_build", "bin")
    if os.path.isdir(tools_bin):
        env["PATH"] = tools_bin + os.pathsep + env.get("PATH", "")
    py = os.path.join(rt, "venv", "Scripts", "python.exe")
    pyw = py.replace("python.exe", "pythonw.exe")
    python = pyw if os.path.isfile(pyw) else py
    subprocess.Popen([python, os.path.join(base, "app.py"), "--with-core"],
                     cwd=rt, env=env, close_fds=True)


def launch_vox_setup(base: str, rt: str, base_py: str):
    """venv_vox 从始至终静默后台：分离子进程，脚本内部自判秒退/续装。"""
    script = os.path.join(base, "_vox_setup.py")
    if not os.path.isfile(script):
        return
    flags = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
    try:
        subprocess.Popen([base_py, script, rt], cwd=base,
                         close_fds=True, creationflags=flags)
    except Exception:
        pass  # vox 纯可选，任何失败不阻塞主启动


def first_boot_flow(base: str, rt: str, libs: str, report, cancel):
    """工作线程执行：建 venv → 装 core 依赖 → 拉起 App。返回 (ok, err_msg)。"""
    if report:
        report({"stage": "venv"})
    py = ensure_pyvenv(rt)
    if not py:
        return False, ("找不到 Python：libs\\qq_bot_runtime\\runtime\\python\\python.exe。\n\n"
                       "便携包不完整，请重新解压/复制。")
    ok = setup_deps(py, rt, os.path.join(rt, "requirements-portable.txt"),
                    os.path.join(libs, "offline_deps_core.zip"), "core",
                    progress=report, cancel=cancel)
    if not ok:
        if cancel is not None and cancel.is_set():
            return False, None
        return False, ("依赖自动配置失败。\n\n"
                       "推荐：把 offline_deps_core.zip 放到 libs\\ 目录后重试（离线秒装）；\n"
                       "或检查网络后重试（pip 自动从清华源 + PyTorch 官方源安装）。")
    if report:
        report({"stage": "start"})
    spawn_app(base, rt)
    return True, None


def main() -> int:
    base = base_dir()
    rt = os.path.join(base, "libs", "qq_bot_runtime")
    app_py = os.path.join(base, "app.py")

    if not os.path.isfile(os.path.join(rt, "config.py")):
        alt = os.environ.get("FEIYU_QQ_BOT", "").strip()
        if alt and os.path.isfile(os.path.join(alt, "config.py")):
            rt = alt
        else:
            alert("找不到运行引擎：libs\\qq_bot_runtime\\config.py 不存在。\n\n"
                  "请确认 FeiyuApp.exe 与 libs、app.py 在同一目录。")
            return 1
    if not os.path.isfile(app_py):
        alert("找不到 app.py（应与 FeiyuApp.exe 同目录）。")
        return 1
    base_py = os.path.join(rt, "runtime", "python", "python.exe")
    if not os.path.isfile(base_py):
        alert("找不到 Python：libs\\qq_bot_runtime\\runtime\\python\\python.exe。\n\n"
              "便携包不完整，请重新解压/复制。")
        return 1

    # 兼容旧布局：包内 libs/<pack> 存在则接线（包外仓库走 UI 插件市场安装）
    ensure_packs(base, rt)

    # 快速路径：环境已就绪 → 直接点火，不开任何窗口（日常启动）
    venv_py = os.path.join(rt, "venv", "Scripts", "python.exe")
    marker = os.path.join(rt, DONE_FILE.format(tag="core"))
    if os.path.isfile(venv_py) and os.path.isfile(marker):
        spawn_app(base, rt)
        return 0

    # 首启：进度窗口（创建 venv + 解压依赖 + 预估时间 + 可取消）
    libs = os.path.dirname(rt)
    try:
        from _progress_gui import run_progress_window
        ok, cancelled, _msg = run_progress_window(
            lambda report, cancel: first_boot_flow(base, rt, libs, report, cancel))
    except Exception:
        # GUI 异常兜底：静默直跑（与旧版行为一致）
        ok, _m = first_boot_flow(base, rt, libs, None, None)
        if not ok:
            alert("依赖自动配置失败。\n\n"
                  "推荐：把 offline_deps_core.zip 放到 libs\\ 目录后重试（离线秒装）；\n"
                  "或检查网络后重试。")
            return 1
        return 0
    return 0 if ok else (0 if cancelled else 1)


if __name__ == "__main__":
    sys.exit(main())
