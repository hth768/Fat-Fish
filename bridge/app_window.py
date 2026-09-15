# -*- coding: utf-8 -*-
"""桌面窗口壳：让 App 控制台以独立窗口运行，不依赖浏览器。

三级策略（自动降级）：
1. pywebview（Edge WebView2 渲染，原生窗口，首选）
2. Edge --app 模式（无地址栏独立窗口，零依赖）
3. 系统默认浏览器（最后兜底）

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


def _open_default_browser(url: str):
    import webbrowser
    try:
        webbrowser.open(url)
        print(f"[WINDOW] 已用系统默认浏览器打开: {url}")
    except Exception as e:
        print(f"[WINDOW][WARN] 浏览器打开失败: {e!r}（请手动访问 {url}）")


def run_window(url: str, title: str = WINDOW_TITLE) -> str:
    """以桌面窗口方式运行控制台（阻塞主线程直到窗口关闭）。

    返回使用的模式: "webview" | "edge-app" | "browser"
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

    # 3) 默认浏览器兜底
    _diag("Edge 不可用 -> 系统默认浏览器兜底")
    _open_default_browser(url)
    _wait_for_close(url)
    return "browser"


def _wait_for_close(url: str):
    """Edge/浏览器模式下等待退出信号（Ctrl+C 或窗口关闭后无法感知，Ctrl+C 退出）。"""
    import time
    print("[WINDOW] 提示：关闭服务请在本控制台按 Ctrl+C（或直接关闭本窗口进程）")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
