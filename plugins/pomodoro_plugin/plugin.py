# -*- coding: utf-8 -*-
"""pomodoro_plugin —— 自包含「番茄钟 / 专注计时」功能插件。

设计要点（与 greeting_demo / reminder_plugin 同风格）：
  * 逻辑全在本包 plugin.py 内，不 import 任何 qq_bot 模块（脱离环境也能加载）；
  * 只通过官方通道向界面推事件，不私接内部对象：
        bridge = getattr(core, "app_bridge", None)
        if bridge: bridge.push(session, {"type": "message", "role": "assistant", "text": ...})
    push 线程安全，可在任意线程/协程调用；session=None 表示全局广播。

行为：自动循环「专注 -> 短休 -> ... -> 长休 -> 专注」，每段开始时把一行文案推到 App 事件流。
  专注 work_minutes 分钟 -> 短休 short_break_minutes 分钟；每完成 cycles_before_long 段专注后
  改为长休 long_break_minutes 分钟。当日专注段数达到 daily_goal（0=不限）后停机，次日自动重开。
  quiet_hours 时段内不推送文案（计时照常，避免夜间打扰）。

配置项（manifest.config_schema，经 on_config 注入）：
  work_minutes / short_break_minutes / long_break_minutes / cycles_before_long / daily_goal / quiet_hours
"""
import asyncio
import time

try:
    from quiet import degrade
except ImportError:  # 脱离 qq_bot 环境时的降级
    def degrade(*_a, **_k):
        pass

try:
    from plugin_base import FeaturePlugin
except Exception:  # 脱离 qq_bot 环境时的降级基类
    class FeaturePlugin:
        name = "base"
        version = "1.0.0"

        def __init__(self, core=None):
            self.core = core
            self._started = False

        async def start(self):
            self._started = True

        async def stop(self):
            self._started = False

        @property
        def started(self):
            return self._started

        def status(self):
            return {"name": self.name, "version": self.version, "running": self._started}


DEFAULTS = {
    "work_minutes": 25,
    "short_break_minutes": 5,
    "long_break_minutes": 15,
    "cycles_before_long": 4,
    "daily_goal": 0,          # 0 = 不限
    "quiet_hours": "",        # 形如 "23:00-07:00"；空 = 不启用
}

PHASES = ("idle", "work", "short_break", "long_break")


def _spawn(coro):
    """在当前事件循环上创建后台任务（兼容无运行循环的老写法）。"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.get_event_loop()
    return loop.create_task(coro)


def _clamp_int(v, lo, hi, default):
    """把配置值规整到 [lo, hi] 的整数；无法解析时回退 default。"""
    try:
        n = int(float(v))
    except Exception:
        return default
    return max(lo, min(hi, n))


def _normalize(cfg):
    """把用户配置规整成内部 _cfg（字段缺省/非法一律回退默认，绝不抛异常）。"""
    out = dict(DEFAULTS)
    if not isinstance(cfg, dict):
        return out
    if cfg.get("work_minutes") not in (None, ""):
        out["work_minutes"] = _clamp_int(cfg.get("work_minutes"), 1, 180, DEFAULTS["work_minutes"])
    if cfg.get("short_break_minutes") not in (None, ""):
        out["short_break_minutes"] = _clamp_int(cfg.get("short_break_minutes"), 1, 60, DEFAULTS["short_break_minutes"])
    if cfg.get("long_break_minutes") not in (None, ""):
        out["long_break_minutes"] = _clamp_int(cfg.get("long_break_minutes"), 1, 120, DEFAULTS["long_break_minutes"])
    if cfg.get("cycles_before_long") not in (None, ""):
        out["cycles_before_long"] = _clamp_int(cfg.get("cycles_before_long"), 1, 12, DEFAULTS["cycles_before_long"])
    if cfg.get("daily_goal") not in (None, ""):
        out["daily_goal"] = _clamp_int(cfg.get("daily_goal"), 0, 100, DEFAULTS["daily_goal"])
    if "quiet_hours" in cfg:
        out["quiet_hours"] = str(cfg.get("quiet_hours") or "").strip()
    return out


def _hhmm_to_min(s):
    """'HH:MM' -> 当日起始分钟数；非法返回 None。"""
    try:
        hh, mm = str(s).strip().split(":")
        h, m = int(hh), int(mm)
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h * 60 + m
    except Exception:
        pass
    return None


def _parse_quiet(s):
    """解析 "23:00-07:00"，返回 (start_min, end_min)；非法/空返回 None。"""
    raw = str(s or "").strip()
    if not raw or "-" not in raw:
        return None
    a, b = raw.split("-", 1)
    s_min, e_min = _hhmm_to_min(a), _hhmm_to_min(b)
    if s_min is None or e_min is None or s_min == e_min:
        return None
    return s_min, e_min


class PomodoroPlugin(FeaturePlugin):
    name = "pomodoro_plugin"
    version = "1.0.0"

    def __init__(self, core):
        super().__init__(core)
        self._cfg = dict(DEFAULTS)
        self._task = None
        self._wake = None
        self._phase = "idle"
        self._phase_start = 0.0
        self._phase_end = 0.0
        self._cycle = 0          # 当前长休循环内的专注段计数
        self._done = 0           # 当日累计专注段数
        self._day = time.strftime("%Y-%m-%d")
        self._running = False

    # ---- 参数热生效（App 插件页「设置」保存后即时回调）----
    def on_config(self, cfg):
        self.apply_config(cfg)

    def apply_config(self, cfg):
        self._cfg = _normalize(cfg)
        self._kick()  # 唤醒等待中的循环，立即按新配置重算

    # ---- 生命周期 ----
    async def start(self):
        self._wake = asyncio.Event()
        self._running = True
        self._phase = "idle"
        self._roll_day()
        self._task = _spawn(self._loop())
        await super().start()

    async def stop(self):
        self._running = False
        self._kick()
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                degrade("pomodoro_plugin.stop", e, "等待任务退出失败")
        self._wake = None
        self._phase = "idle"
        await super().stop()

    # ---- 主循环 ----
    async def _loop(self):
        while True:
            try:
                if self._phase == "idle":
                    if self._should_start():
                        self._begin("work")
                    elif await self._sleep(60):
                        continue
                    continue
                dur = self._duration_of(self._phase)
                remaining = (self._phase_start + dur) - time.time()
                if remaining > 0:
                    if await self._sleep(remaining):
                        continue  # 被配置变更/停止唤醒 -> 回到顶部重算
                    dur = self._duration_of(self._phase)
                    if time.time() < self._phase_start + dur:
                        continue  # 被唤醒但（新配置下）还没到点
                self._advance()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                degrade("pomodoro_plugin._loop", e, "主循环异常，5 秒后重试")
                if await self._sleep(5):
                    continue

    # ---- 相位推进 ----
    def _advance(self):
        if self._phase == "work":
            self._done += 1
            self._cycle += 1
            goal = int(self._cfg.get("daily_goal") or 0)
            if goal and self._done >= goal:
                self._phase = "idle"
                self._push(f"🎉 今日目标达成（共 {self._done} 段专注），番茄钟已停机，明天继续～")
                return
            nxt = "long_break" if (self._cycle % max(1, int(self._cfg["cycles_before_long"])) == 0) else "short_break"
        else:
            nxt = "work"
        self._begin(nxt)

    def _begin(self, phase):
        self._phase = phase
        self._phase_start = time.time()
        dur = self._duration_of(phase)
        self._phase_end = self._phase_start + dur
        mins = int(round(dur / 60.0))
        if phase == "work":
            self._push(f"🍅 第 {self._done + 1} 段专注开始（约 {mins} 分钟），加油～")
        elif phase == "short_break":
            self._push(f"☕ 短休开始（约 {mins} 分钟），起来动一动吧～")
        elif phase == "long_break":
            self._push(f"🌿 长休开始（约 {mins} 分钟），好好放松一下～")

    def _duration_of(self, phase):
        if phase == "work":
            return max(1, int(self._cfg["work_minutes"])) * 60
        if phase == "short_break":
            return max(1, int(self._cfg["short_break_minutes"])) * 60
        if phase == "long_break":
            return max(1, int(self._cfg["long_break_minutes"])) * 60
        return 0

    # ---- 每日目标 ----
    def _roll_day(self):
        today = time.strftime("%Y-%m-%d")
        if today != self._day:
            self._day = today
            self._done = 0
            self._cycle = 0

    def _should_start(self):
        self._roll_day()
        goal = int(self._cfg.get("daily_goal") or 0)
        return not (goal and self._done >= goal)

    # ---- 等待（可被唤醒，返回 True 表示被唤醒）----
    async def _sleep(self, secs):
        try:
            secs = float(secs)
        except Exception:
            secs = 1.0
        if secs <= 0:
            return False
        ev = self._wake
        if ev is None:
            await asyncio.sleep(secs)
            return False
        try:
            await asyncio.wait_for(ev.wait(), timeout=secs)
        except asyncio.TimeoutError:
            return False
        except asyncio.CancelledError:
            raise
        try:
            ev.clear()
        except Exception:
            pass
        return True

    def _kick(self):
        ev = self._wake
        if ev is not None:
            try:
                ev.set()
            except Exception as e:
                degrade("pomodoro_plugin._kick", e, "唤醒循环失败")

    # ---- 官方通道：向 App 事件流推一条消息 ----
    def _in_quiet_hours(self):
        rng = _parse_quiet(self._cfg.get("quiet_hours"))
        if not rng:
            return False
        start, end = rng
        lt = time.localtime()
        cur = lt.tm_hour * 60 + lt.tm_min
        if start < end:
            return start <= cur < end
        return cur >= start or cur < end  # 跨零点

    def _push(self, text, etype="message"):
        if self._in_quiet_hours():
            return
        bridge = getattr(self.core, "app_bridge", None)
        if bridge is None:
            return
        try:
            bridge.push(None, {"type": etype, "role": "assistant", "text": str(text)})
        except Exception as e:
            degrade("pomodoro_plugin._push", e, "推送事件失败")

    # ---- 状态（供 /插件 或调试查看）----
    def status(self):
        try:
            base = super().status()
        except Exception:
            base = {"name": self.name, "version": self.version}
        rem = 0
        if self._phase != "idle":
            rem = max(0, int(self._phase_end - time.time()))
        return {**base, "phase": self._phase, "cycle": self._cycle,
                "done_today": self._done, "remaining_seconds": rem}


_INSTANCE = None


def on_config(cfg: dict):
    """管理器在装载时与用户保存配置后调用（模块级，作用于当前实例）。"""
    inst = _INSTANCE
    if inst is not None and hasattr(inst, "apply_config"):
        inst.apply_config(cfg)


def create_plugin(core):
    global _INSTANCE
    _INSTANCE = PomodoroPlugin(core)
    return _INSTANCE
