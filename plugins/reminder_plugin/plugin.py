# -*- coding: utf-8 -*-
"""reminder_plugin —— 自包含「定时提醒 / 闹钟」功能插件。

设计要点：
  * 逻辑全在本包 plugin.py 内，不 import 任何 qq_bot 模块（脱离环境也能加载）；
  * 只通过官方通道向界面推事件：
        bridge = getattr(core, "app_bridge", None)
        if bridge: bridge.push(session, {"type": "message", "role": "assistant", "text": ...})
    push 线程安全，可在任意线程/协程调用；session=None 表示全局广播。

四类提醒（reminders 配置项，JSON 数组，每项四者其一）：
  1) 每日定时:   {"text": "该吃药了", "at": "08:30"}              —— 每天 08:30 触发一次
  2) 固定间隔:   {"text": "起来活动一下", "every": 3600}           —— 每 3600 秒触发
  3) 一次性延时: {"text": "10 分钟后开会", "delay": 600}           —— 装载后 600 秒触发一次
  4) 绝对闹钟:   {"text": "叫起床", "once_at": "2026-09-21 07:00"} —— 到点触发一次
                once_at 支持 "YYYY-MM-DD HH:MM"（绝对时刻）；也支持只写 "HH:MM"
                （取今天该时刻，若已过则顺延到明天该时刻）。

任意一项都可选带 "task" 字段作为提醒标签，推送文案形如「⏰ 提醒：[叫起床] 快起床啦」；
不带 task 时形如「⏰ 提醒：该吃药了」。

配置项（manifest.config_schema，经 on_config 注入）：
  reminders        提醒清单（JSON 数组，见上）
  check_interval   扫描/轮询间隔秒，默认 20
  quiet_hours      免打扰时段 "23:00-07:00"（空=不启用）
"""
import asyncio
import json
import time
from datetime import datetime, timedelta

try:
    from quiet import degrade
except ImportError:
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
    "reminders": [{"text": "喝水时间到啦，主人～", "every": 3600}],
    "check_interval": 20,
    "quiet_hours": "",
}


def _parse_hhmm(s):
    """把 'HH:MM' 解析成 (hour, minute)；非法返回 None。"""
    try:
        hh, mm = str(s).strip().split(":")
        h, m = int(hh), int(mm)
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h, m
    except Exception:
        pass
    return None


def _parse_when(s):
    """把 once_at 解析成绝对时间戳(float)；非法返回 None。

    支持两种写法：
      * "YYYY-MM-DD HH:MM"（日期分隔符也容忍 '/'）—— 绝对时刻；
      * "HH:MM" —— 取今天该时刻，若已过则顺延到明天该时刻。
    """
    if s is None:
        return None
    raw = str(s).strip()
    if not raw:
        return None
    now = datetime.now()
    for fmt in ("%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M"):
        try:
            return datetime.strptime(raw, fmt).timestamp()
        except Exception:
            continue
    hm = _parse_hhmm(raw)
    if not hm:
        return None
    try:
        dt = now.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)
    except Exception as e:
        degrade("reminder_plugin._parse_when", e, "构造 once_at 时刻失败")
        return None
    if dt.timestamp() <= now.timestamp():
        dt = dt + timedelta(days=1)
    return dt.timestamp()


def _is_explicitly_empty(raw) -> bool:
    """判断用户是否显式清空了提醒清单（用于区分「清空」与「坏 JSON」）。"""
    if raw is None:
        return False
    if isinstance(raw, list):
        return len(raw) == 0
    if isinstance(raw, str):
        return raw.strip() in ("", "[]")
    return False


def _parse_reminders(raw):
    """把配置里的 reminders 规整成 list[dict]；坏项丢弃，绝不抛异常。"""
    if isinstance(raw, str):
        raw = raw.strip()
        if not raw:
            return []
        try:
            raw = json.loads(raw)
        except Exception as e:
            degrade("reminder_plugin._parse_reminders", e, "reminders 不是合法 JSON，忽略本次")
            return []
    if not isinstance(raw, list):
        return []
    out = []
    for it in raw:
        if not isinstance(it, dict):
            continue
        text = str(it.get("text") or "").strip()
        if not text:
            continue
        item = {"text": text}
        task = str(it.get("task") or "").strip()
        if task:
            item["task"] = task
        # 形态四：绝对时间一次性闹钟（解析失败 → 丢弃该项，不回退到其它形态）
        if it.get("once_at") is not None:
            at_ts = _parse_when(it.get("once_at"))
            if at_ts is None:
                degrade("reminder_plugin._parse_reminders",
                        ValueError("bad once_at: {!r}".format(it.get("once_at"))),
                        "once_at 非法，丢弃该项")
                continue
            item["once_at"] = at_ts
            out.append(item)
            continue
        # 形态一：每日定时 HH:MM
        hm = _parse_hhmm(it.get("at")) if it.get("at") is not None else None
        if hm:
            item["at"] = hm
            out.append(item)
            continue
        # 形态二：固定间隔（秒）
        try:
            sec = int(float(it.get("every")))
            if sec >= 1:
                item["every"] = sec
                out.append(item)
                continue
        except Exception:
            pass
        # 形态三：一次性延时（秒）
        try:
            sec = int(float(it.get("delay")))
            if sec >= 1:
                item["delay"] = sec
                out.append(item)
                continue
        except Exception:
            pass
    return out


def _quiet_range(s):
    """解析 "23:00-07:00" → (start_min, end_min)；非法/空返回 None。"""
    raw = str(s or "").strip()
    if not raw or "-" not in raw:
        return None
    a, b = raw.split("-", 1)
    hm_a, hm_b = _parse_hhmm(a), _parse_hhmm(b)
    if not hm_a or not hm_b:
        return None
    return hm_a[0] * 60 + hm_a[1], hm_b[0] * 60 + hm_b[1]


class ReminderPlugin(FeaturePlugin):
    name = "reminder_plugin"
    version = "1.1.0"

    def __init__(self, core):
        super().__init__(core)
        self._task = None
        self._wake = None
        self._cfg = dict(DEFAULTS)
        self._reminders = _parse_reminders(DEFAULTS["reminders"])
        self._loaded_at = time.time()
        self._state = []
        self._reset_state()

    # ---- 状态重建：last_day(每日去重) / last_ts(间隔基准) / fired(一次性) ----
    def _reset_state(self):
        now_ts = time.time()
        self._state = []
        for item in self._reminders:
            fired = "once_at" in item and item["once_at"] <= now_ts
            self._state.append({"last_day": None, "last_ts": self._loaded_at, "fired": fired})

    # ---- 参数热生效（App 插件页「设置」保存后即时回调）----
    def on_config(self, cfg):
        self.apply_config(cfg)

    def apply_config(self, cfg: dict):
        cfg = cfg or {}
        if "check_interval" in cfg:
            try:
                self._cfg["check_interval"] = max(5, min(3600, int(float(cfg["check_interval"]))))
            except Exception as e:
                degrade("reminder_plugin.apply_config", e, "check_interval 解析失败，保留旧值")
        if "quiet_hours" in cfg:
            self._cfg["quiet_hours"] = str(cfg.get("quiet_hours") or "").strip()
        if "reminders" in cfg:
            raw = cfg.get("reminders")
            new = _parse_reminders(raw)
            if new:
                self._reminders = new
            elif _is_explicitly_empty(raw):
                self._reminders = []   # 显式清空
            # 否则：坏 JSON / 未知格式 → 保留旧清单，避免误清空
        self._loaded_at = time.time()
        self._reset_state()
        # 唤醒等待中的循环，立刻按新配置重新计时
        ev = self._wake
        if ev is not None:
            try:
                ev.set()
            except Exception as e:
                degrade("reminder_plugin.apply_config.wake", e, "唤醒循环失败")

    # ---- 生命周期 ----
    async def start(self):
        self._wake = asyncio.Event()
        self._loaded_at = time.time()
        self._reset_state()
        self._task = asyncio.get_event_loop().create_task(self._loop())
        await super().start()

    async def stop(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                degrade("reminder_plugin.stop", e, "等待提醒任务退出失败")
            self._task = None
        self._wake = None
        await super().stop()

    # ---- 主循环（可被配置变更唤醒，重算等待时间）----
    async def _loop(self):
        while True:
            try:
                await asyncio.wait_for(self._wake.wait(),
                                       timeout=float(self._cfg["check_interval"]))
                self._wake.clear()
                continue
            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:
                raise
            try:
                self._tick()
            except Exception as e:
                degrade("reminder_plugin._loop", e, "本轮提醒扫描异常")

    def _tick(self):
        now = datetime.now()
        ts = time.time()
        for item, st in zip(self._reminders, self._state):
            try:
                if self._should_fire(item, st, now, ts):
                    self._fire(item)
                    self._mark_fired(item, st, now, ts)
            except Exception as e:
                degrade("reminder_plugin._tick", e, "单项提醒处理失败")

    def _should_fire(self, item, st, now, ts) -> bool:
        if "at" in item:
            h, m = item["at"]
            return now.hour == h and now.minute == m and st["last_day"] != now.strftime("%Y-%m-%d")
        if "once_at" in item:
            return (not st["fired"]) and ts >= item["once_at"]
        if "every" in item:
            return (ts - st["last_ts"]) >= item["every"]
        if "delay" in item:
            return (not st["fired"]) and (ts - self._loaded_at) >= item["delay"]
        return False

    def _mark_fired(self, item, st, now, ts):
        if "at" in item:
            st["last_day"] = now.strftime("%Y-%m-%d")
        elif "every" in item:
            st["last_ts"] = ts
        else:
            st["fired"] = True

    def _fire(self, item):
        """向 App 事件流推一条提醒（拿不到桥/处于免打扰时段则静默跳过）。"""
        now = datetime.now()
        if self._in_quiet(now):
            return
        bridge = getattr(self.core, "app_bridge", None)
        if bridge is None:
            return
        base = item["text"]
        task = item.get("task")
        text = "⏰ 提醒：{}{}".format("[{}] ".format(task) if task else "", base)
        try:
            bridge.push(None, {"type": "message", "role": "assistant", "text": text})
        except Exception as e:
            degrade("reminder_plugin._fire", e, "推送提醒失败")

    def _in_quiet(self, now) -> bool:
        """判断 now 是否落在免打扰时段（支持跨天，如 23:00-07:00）。"""
        rng = _quiet_range(self._cfg.get("quiet_hours"))
        if not rng:
            return False
        s, e = rng
        cur = now.hour * 60 + now.minute
        if s == e:
            return False
        if s < e:
            return s <= cur < e
        return cur >= s or cur < e   # 跨天

    def status(self):
        st = super().status()
        st.update({"running": self._started,
                   "reminders": len(self._reminders),
                   "check_interval": self._cfg.get("check_interval")})
        return st


_INSTANCE = None


def on_config(cfg: dict):
    """管理器在装载时与用户保存配置后调用（模块级，作用于当前实例）。"""
    inst = _INSTANCE
    if inst is not None and hasattr(inst, "apply_config"):
        inst.apply_config(cfg)


def create_plugin(core):
    global _INSTANCE
    _INSTANCE = ReminderPlugin(core)
    return _INSTANCE
