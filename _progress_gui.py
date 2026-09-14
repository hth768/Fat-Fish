# -*- coding: utf-8 -*-
"""首启进度窗口（纯 Win32 / ctypes，零第三方依赖，基础解释器即可运行）。

run_progress_window(work_fn) -> (ok, cancelled, err_msg)
  work_fn(report, cancel_event) -> (ok, err_msg)，在工作线程执行；
  report(dict) 推送阶段事件：venv / unpack / pip / start。

进度条策略：
- unpack 阶段：真实字节进度 + 线性外推剩余时间；
- venv/pip 阶段：子进程黑盒无真实进度，用缓慢爬行的伪进度（封顶 92%），
  一旦真实进度到达即被覆盖，视觉平滑衔接；
- 窗口关闭 / 取消按钮 -> cancel_event，解压循环感知后安全退出（半残
  site-packages 由下次启动的代表包探测自愈，解压幂等覆盖）。
"""
import ctypes
import ctypes.wintypes as wt
import queue
import threading
import time
from ctypes import byref, c_int, c_ssize_t, c_size_t, c_uint, c_void_p, c_wchar_p

_user32 = ctypes.windll.user32
_gdi32 = ctypes.windll.gdi32

# ---- Win32 常量 ----
WS_CHILD = 0x40000000
WS_VISIBLE = 0x10000000
WS_CAPTION = 0x00C00000
WS_SYSMENU = 0x00080000
WM_SETFONT = 0x0030
WM_COMMAND = 0x0111
WM_TIMER = 0x0113
WM_CLOSE = 0x0010
WM_DESTROY = 0x0002
PBM_SETPOS = 0x0402
PBM_SETRANGE32 = 0x0406
WNDPROC = ctypes.WINFUNCTYPE(c_ssize_t, c_void_p, c_uint, c_size_t, c_ssize_t)


class _Rect(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class _WndClassW(ctypes.Structure):
    _fields_ = [("style", c_uint), ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", c_int), ("cbWndExtra", c_int),
                ("hInstance", c_void_p), ("hIcon", c_void_p),
                ("hCursor", c_void_p), ("hbrBackground", c_void_p),
                ("lpszMenuName", c_wchar_p), ("lpszClassName", c_wchar_p)]


def _fmt_mmss(sec: float) -> str:
    sec = int(max(0, sec))
    return f"{sec // 60}:{sec % 60:02d}"


class ProgressWindow:
    W, H = 470, 212  # 客户区逻辑尺寸

    def __init__(self):
        self.cancel_event = threading.Event()
        self._q = queue.Queue()
        self._fake = 0.0          # 伪进度（venv/pip 阶段爬行值）
        self._stage = ""
        self._t0 = time.time()
        self._ended = False
        self._result = {"ok": False, "cancelled": False, "msg": None}

    # ------------------------------------------------------------------
    # 对外入口
    # ------------------------------------------------------------------
    def run(self, work_fn):
        """跑 work_fn 并显示进度窗口。返回 (ok, cancelled, err_msg)。"""
        try:
            self._run_gui(work_fn)
        except Exception:
            # 无 GUI 环境（极端精简系统）：降级静默直跑
            try:
                ok, msg = work_fn(None, self.cancel_event)
                self._result.update(ok=bool(ok), msg=msg)
                if not ok and self.cancel_event.is_set():
                    self._result["cancelled"] = True
            except Exception as e:
                self._result["msg"] = repr(e)
        if self._result["msg"] and not self._result["ok"] and not self._result["cancelled"]:
            try:
                _user32.MessageBoxW(None, self._result["msg"], "肥鱼娘 App", 0x10)
            except Exception:
                pass
        return self._result["ok"], self._result["cancelled"], self._result["msg"]

    # ------------------------------------------------------------------
    # GUI 主流程
    # ------------------------------------------------------------------
    def _run_gui(self, work_fn):
        try:
            _user32.SetProcessDPIAware()
        except Exception:
            pass
        hinst = ctypes.windll.kernel32.GetModuleHandleW(None)
        self._hinst = hinst

        def proc(hwnd, msg, wparam, lparam):
            if msg == WM_TIMER:
                if wparam == 1:
                    self._tick()
                elif wparam == 2:
                    _user32.DestroyWindow(hwnd)
                return 0
            if msg == WM_COMMAND:      # 取消按钮
                self.cancel_event.set()
                self._set_stage("正在取消…", "当前文件解压完即停止")
                return 0
            if msg == WM_CLOSE:        # 点窗口 X 同取消
                self.cancel_event.set()
                _user32.DestroyWindow(hwnd)
                return 0
            if msg == WM_DESTROY:
                _user32.PostQuitMessage(0)
                return 0
            return _user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        self._proc_ref = WNDPROC(proc)  # 持引用防 GC
        wc = _WndClassW(0, self._proc_ref, 0, 0, c_void_p(hinst), None, None,
                        c_void_p(16), None, c_wchar_p("FeiyuSetupWnd"))
        _user32.RegisterClassW(byref(wc))

        style = WS_CAPTION | WS_SYSMENU
        rect = _Rect(0, 0, self.W, self.H)
        _user32.AdjustWindowRect(byref(rect), style, False)
        w, h = rect.right - rect.left, rect.bottom - rect.top
        sx, sy = _user32.GetSystemMetrics(0), _user32.GetSystemMetrics(1)
        self._hwnd = _user32.CreateWindowExW(
            0, c_wchar_p("FeiyuSetupWnd"), c_wchar_p("肥鱼娘 App · 首次启动配置"),
            c_uint(style | WS_VISIBLE),
            c_int(max(0, (sx - w) // 2)), c_int(max(0, (sy - h) // 2)),
            c_int(w), c_int(h), None, None, c_void_p(hinst), None)

        f_title, f_sub = self._font(17, True), self._font(12)
        f_stage, f_detail, f_btn = self._font(14), self._font(12), self._font(12)
        self._ctrl("STATIC", "🐟 肥鱼娘 · 首次启动配置", 0, 16, 12, 430, 26, f_title)
        self._ctrl("STATIC", "正在准备运行环境（仅首次需要，之后启动秒开）",
                   0, 16, 42, 430, 18, f_sub)
        self._pb = self._ctrl("msctls_progress32", None, 0, 16, 70, 430, 20, None)
        _user32.SendMessageW(self._pb, PBM_SETRANGE32, 0, 100 << 16)
        self._stage_lbl = self._ctrl("STATIC", "准备中…", 0, 16, 100, 430, 20, f_stage)
        self._detail_lbl = self._ctrl("STATIC", "", 0, 16, 124, 430, 18, f_detail)
        self._ctrl("BUTTON", "取消", 0, 360, 154, 86, 26, f_btn)

        def worker():
            try:
                ok, msg = work_fn(lambda ev: self._q.put(dict(ev)), self.cancel_event)
                self._result["ok"] = bool(ok)
                self._result["msg"] = msg
                if not ok and self.cancel_event.is_set():
                    self._result["cancelled"] = True
            except Exception as e:
                self._result["msg"] = repr(e)
            self._q.put({"stage": "_end"})

        threading.Thread(target=worker, daemon=True).start()
        _user32.SetTimer(self._hwnd, 1, 100, None)
        _user32.SetForegroundWindow(self._hwnd)

        msg_s = wt.MSG()
        lp = byref(msg_s)
        while _user32.GetMessageW(lp, None, 0, 0) > 0:
            _user32.TranslateMessage(lp)
            _user32.DispatchMessageW(lp)

    # ------------------------------------------------------------------
    # UI 刷新
    # ------------------------------------------------------------------
    def _tick(self):
        try:
            while True:
                ev = self._q.get_nowait()
                if ev.get("stage") == "_end":
                    self._finish()
                    return
                self._apply(ev)
        except queue.Empty:
            pass
        if self._ended:
            return
        # 伪进度爬行（venv/pip/start 黑盒阶段）；unpack 用真实值
        if self._stage in ("venv", "pip"):
            self._fake = min(92.0, self._fake + 0.5)
            _user32.SendMessageW(self._pb, PBM_SETPOS, int(self._fake), 0)
        if self._stage == "venv":
            self._set_detail(f"已用 {int(time.time() - self._t0)} 秒…")
        elif self._stage == "pip":
            self._set_detail(f"已用 {_fmt_mmss(time.time() - self._t0)}…")

    def _apply(self, ev):
        st = ev.get("stage")
        if st == "venv":
            self._stage, self._fake = "venv", 0.0
            self._t0 = time.time()
            self._set_stage("正在创建虚拟环境…", "")
        elif st == "unpack":
            self._stage = "unpack"
            tot = ev.get("total_bytes") or 1
            done = ev.get("bytes", 0)
            pct = min(100.0, done / tot * 100.0)
            self._fake = max(self._fake, pct)
            _user32.SendMessageW(self._pb, PBM_SETPOS, int(self._fake), 0)
            self._set_stage("正在解压核心依赖…",
                            f"{pct:.0f}% · {done / 1e9:.2f} / {tot / 1e9:.2f} GB"
                            f" · 剩余约 {_fmt_mmss(ev.get('eta', 0))}")
        elif st == "pip":
            self._stage, self._fake = "pip", 0.0
            self._t0 = time.time()
            self._set_stage("正在在线安装依赖…",
                            "视网速约 30~90 分钟（仅离线包缺失时才会走此路径）")
        elif st == "start":
            self._stage = "start"
            self._set_stage("依赖就绪，正在启动肥鱼娘…", "马上就好")

    def _finish(self):
        self._ended = True
        if self._result["cancelled"]:
            self._set_stage("已取消", "稍后重新双击 FeiyuApp.exe 可继续")
        elif self._result["ok"]:
            _user32.SendMessageW(self._pb, PBM_SETPOS, 100, 0)
            self._set_stage("启动完成", "本窗口将自动关闭")
        else:
            self._set_stage("配置失败", (self._result["msg"] or "未知错误")[:120])
        try:
            _user32.KillTimer(self._hwnd, 1)
            _user32.SetTimer(self._hwnd, 2, 1200 if (self._result["ok"]
                             or self._result["cancelled"]) else 3500, None)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Win32 小工具
    # ------------------------------------------------------------------
    def _font(self, px, bold=False):
        return _gdi32.CreateFontW(-px, 0, 0, 0, 700 if bold else 400,
                                  0, 0, 0, 0, 0, 0, 0, 0, c_wchar_p("Microsoft YaHei UI"))

    def _ctrl(self, cls, text, style, x, y, w, h, font):
        hwnd = _user32.CreateWindowExW(
            0, c_wchar_p(cls), c_wchar_p(text or ""),
            c_uint(style | WS_CHILD | WS_VISIBLE),
            c_int(x), c_int(y), c_int(w), c_int(h),
            c_void_p(self._hwnd), None, c_void_p(self._hinst), None)
        if font:
            _user32.SendMessageW(hwnd, WM_SETFONT, font, 1)
        return hwnd

    def _set_stage(self, stage, detail):
        try:
            _user32.SetWindowTextW(self._stage_lbl, c_wchar_p(stage))
            _user32.SetWindowTextW(self._detail_lbl, c_wchar_p(detail or ""))
        except Exception:
            pass

    def _set_detail(self, detail):
        _user32.SetWindowTextW(self._detail_lbl, c_wchar_p(detail or ""))


def run_progress_window(work_fn):
    """便捷入口：构造 ProgressWindow 并运行。返回 (ok, cancelled, err_msg)。"""
    return ProgressWindow().run(work_fn)
