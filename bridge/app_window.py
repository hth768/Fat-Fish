# -*- coding: utf-8 -*-
"""桌面窗口壳：让 App 控制台以独立窗口运行，不依赖浏览器。

本模块只负责「APP 独立窗口」这一级（自动降级）：
1. pywebview（Edge WebView2 渲染，原生窗口，首选）
2. Edge --app 模式（无地址栏独立窗口，零依赖）

两级都不可用则返回 None，交由 app.py 继续降级到 webui（浏览器）或命令行。
窗口关闭 = 退出 main 流程（app.py 负责清理核心 / sidecar）。
"""
import os
import subprocess
import sys
import time

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WINDOW_TITLE = "肥鱼娘 · App 控制台"
WIDTH, HEIGHT = 1280, 860
MIN_W, MIN_H = 960, 600
ICON_PATH = os.path.join(APP_DIR, "webui", "icon.png")
ICON_ICO = os.path.join(APP_DIR, "webui", "favicon.ico")


def _diag(msg: str):
    """窗口引擎诊断写文件：窗口模式下 print 用户看不到，排查『窗口跳不出来』靠它。"""
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line)
    try:
        d = os.path.join(APP_DIR, "data")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "window.log"), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


class _AppearanceApi:
    """暴露给前端（仅 pywebview 模式）的接口：实时修改窗口标题栏。"""
    def set_title(self, title):
        try:
            for w in getattr(webview, "windows", []):
                try:
                    w.set_title(str(title)[:40])
                except Exception:
                    pass
        except Exception:
            pass


def _edge_candidates() -> list:
    """常见 Edge 可执行文件路径（新→旧）。"""
    pf = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    pf64 = os.environ.get("ProgramFiles", r"C:\Program Files")
    local = os.environ.get("LOCALAPPDATA", "")
    return [
        os.path.join(pf, r"Microsoft\Edge\Application\msedge.exe"),
        os.path.join(pf64, r"Microsoft\Edge\Application\msedge.exe"),
        os.path.join(local, r"Microsoft\Edge\Application\msedge.exe"),
    ]


def _open_edge_app(url: str) -> bool:
    """用 Edge --app 模式打开无地址栏独立窗口。"""
    for exe in _edge_candidates():
        if os.path.isfile(exe):
            try:
                subprocess.Popen(
                    [exe, f"--app={url}",
                     f"--window-size={WIDTH},{HEIGHT}"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                print(f"[WINDOW] 已用 Edge App 窗口打开: {url}")
                return True
            except Exception as e:
                print(f"[WINDOW][WARN] Edge 启动失败: {e!r}")
    return False


def try_app_window(url: str, title: str = WINDOW_TITLE) -> str | None:
    """以桌面独立窗口方式运行控制台（阻塞主线程直到窗口关闭）。

    优先 pywebview（原生窗口），其次 Edge --app 模式；两者都不可用返回 None，
    交由 app.py 降级到 webui（浏览器）或命令行。

    返回使用的模式: "webview" | "edge-app" | None
    """
    # pythonnet 运行时选择：coreclr（.NET Core）在某些机器初始化失败
    # （Failed to create a .NET runtime (coreclr)），强制走 .NET Framework 更稳。
    os.environ.setdefault("PYTHONNET_RUNTIME", "netfx")

    # 1) pywebview 优先
    try:
        import webview  # pywebview
        _diag("pywebview 可导入（netfx 模式）")
    except Exception as e:
        _diag(f"pywebview 不可导入: {e!r} -> 尝试 Edge App 窗口")
        webview = None

    if webview is not None:
        try:
            webview.create_window(
                title, url,
                width=WIDTH, height=HEIGHT,
                min_size=(MIN_W, MIN_H),
                background_color="#0d1220",
                js_api=_AppearanceApi(),
            )
            # 窗口图标：部分后端支持 start(icon=...)；不支持时静默回退默认启动
            try:
                webview.start(icon=ICON_ICO if os.path.isfile(ICON_ICO) else ICON_PATH)
            except TypeError:
                webview.start()
            _diag("pywebview 窗口正常退出（用户关闭）")
            return "webview"
        except Exception as e:
            _diag(f"pywebview 窗口启动失败: {e!r} -> 降级 Edge App 窗口")

    # 2) Edge --app 模式
    if _open_edge_app(url):
        _diag("Edge App 窗口已打开")
        # Edge 窗口是独立进程：主进程保持常驻（HTTP 服务要活多久取决于这里）
        _wait_for_close(url)
        return "edge-app"

    # 独立窗口两级均不可用：返回 None，交由 app.py 降级
    _diag("pywebview 与 Edge App 均不可用 -> 返回 None，交由上层降级")
    return None



def _wait_for_close(url: str):
    """Edge/浏览器模式下等待退出信号（Ctrl+C 或窗口关闭后无法感知，Ctrl+C 退出）。"""
    import time
    print("[WINDOW] 提示：关闭服务请在本控制台按 Ctrl+C（或直接关闭本窗口进程）")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
