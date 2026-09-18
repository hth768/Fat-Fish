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
from quiet import degrade

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WINDOW_TITLE = "肥鱼娘 · App 控制台"
# pywebview 模块对象（导入成功后由 try_app_window 填）：js_api 需要它访问 windows / 常量。
# 注意：不能在 try_app_window 里 `import webview` 后就让 js_api 直接引用 —— 那是函数局部名，
# js_api 里会 NameError 并被 except 静默吞掉（set_title 曾因此长期无效）。
_webview = None
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
    """暴露给前端（仅 pywebview 模式）的接口：改窗口标题、选目录。"""
    def set_title(self, title):
        wv = _webview
        if wv is None:
            return
        try:
            for w in getattr(wv, "windows", []):
                try:
                    w.set_title(str(title)[:40])
                except Exception:
                    pass
        except Exception as e:
            degrade("bridge/app_window.py:53 _AppearanceApi.set_title", e, "降级：for w in getattr(wv, 'windows', [])")

    def pick_folder(self, initial=None):
        """弹系统「选择文件夹」对话框（pywebview 模式才有）。

        返回 {"supported": bool, "path": str|None, "error": str}：
        - supported=False → 前端改用内置目录浏览器（Edge/浏览器模式没有此能力）；
        - supported=True 且 path=None → 用户取消了对话框，前端不要再弹内置选择器。
        """
        wv = _webview
        if wv is None:
            return {"supported": False, "error": "非 pywebview 模式"}
        try:
            wins = getattr(wv, "windows", None) or []
            if not wins:
                return {"supported": False, "error": "窗口未就绪"}
            # pywebview 5+ 推荐 FileDialog.FOLDER；旧版只有 FOLDER_DIALOG（已废弃会告警）
            kind = None
            fd = getattr(wv, "FileDialog", None)
            if fd is not None:
                kind = getattr(fd, "FOLDER", None)
            if kind is None:
                kind = getattr(wv, "FOLDER_DIALOG", None)
            if kind is None:
                return {"supported": False, "error": "当前 pywebview 版本不支持目录对话框"}
            kwargs = {}
            init = str(initial or "").strip().strip('"')
            if init and os.path.isdir(init):
                kwargs["directory"] = init
            r = wins[0].create_file_dialog(kind, **kwargs)
            if isinstance(r, (list, tuple)):
                r = r[0] if r else None
            if not r:
                return {"supported": True, "path": None}      # 用户取消
            return {"supported": True, "path": os.path.abspath(str(r))}
        except Exception as e:
            _diag(f"pick_folder 失败: {e!r}")
            return {"supported": False, "error": repr(e)}


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
    global _webview
    try:
        import webview  # pywebview
        _webview = webview
        _diag("pywebview 可导入（netfx 模式）")
    except Exception as e:
        _diag(f"pywebview 不可导入: {e!r} -> 尝试 Edge App 窗口")
        webview = None
        _webview = None

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
