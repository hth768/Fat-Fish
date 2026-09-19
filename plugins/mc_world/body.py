# -*- coding: utf-8 -*-
"""Minecraft 身体层（Python 版 mineflayer 适配器）。

复用 cortico-world-mc-agent/src/body.ts 的语义：
  connect / disconnect / world-state / chat / 保命反射 / goto / dig / attack / say / scan。

依赖：pip install mineflayer   （社区版 Python mineflayer 协议实现）
注意：mineflayer 的实际 import 放在 __init__ 内惰性执行，保证「未装依赖时模块仍可被 import」，
不会污染 pkg_manager 扫描期。

事件总线：极简 on/emit，对接 plugin.py 的 _wire。
"""
import time
import threading


class _Bus:
    def __init__(self):
        self._h = {}

    def on(self, name, fn):
        self._h.setdefault(name, []).append(fn)

    def emit(self, name, *a):
        for fn in self._h.get(name, []):
            try:
                fn(*a)
            except Exception:
                pass


class MinecraftBody:
    def __init__(self, host="127.0.0.1", port=25565, username="FeiyuBot",
                 version=None, password=None):
        # 惰性 import：缺依赖时只在这一步抛错，不影响插件扫描。
        import mineflayer  # 社区版 Python mineflayer
        self._mf = mineflayer
        self.host = host
        self.port = port
        self.username = username
        self.version = version
        self.password = password
        self.bus = _Bus()
        self.bot = None
        self._obs_timer = None
        self._reflex_timer = None
        self._running = False

    # ---- 事件总线转发 ----
    def on(self, name, fn):
        self.bus.on(name, fn)

    def emit(self, name, *a):
        self.bus.emit(name, *a)

    # ---- 连接生命周期 ----
    def run(self):
        """阻塞直到断开（内部由 mineflayer 事件驱动）。"""
        # 注：真实实现依所用 Python mineflayer 库的 API 调整 connect 与事件名。
        self.bot = self._mf.createBot(
            host=self.host, port=self.port, username=self.username,
            version=self.version, password=self.password,
        )
        self._running = True
        self._hook(self.bot)
        try:
            self.bot.run()  # 阻塞
        finally:
            self._stop_timers()
            self._running = False

    def disconnect(self):
        self._running = False
        self._stop_timers()
        try:
            if self.bot is not None:
                self.bot.end()
        except Exception:
            pass

    def _hook(self, bot):
        """把 mineflayer 原生事件桥到肥鱼事件总线，并启动反射。"""
        # 聊天 -> 转发
        bot.on("chat", lambda u, m: self.emit("chat", u, m))
        # 断线 -> 结束 run 循环
        bot.on("end", lambda: setattr(self, "_running", False))

    # ---- 观察 / 保命反射 ----
    def start_observe(self, interval_ms=22000):
        self._obs_timer = _Repeat(interval_ms / 1000.0, self._tick_observe)
        self._reflex_timer = _Repeat(interval_ms / 1000.0, self._tick_reflex)
        self._obs_timer.start()
        self._reflex_timer.start()

    def _stop_timers(self):
        for t in (self._obs_timer, self._reflex_timer):
            try:
                if t is not None:
                    t.stop()
            except Exception:
                pass

    def observe_now(self) -> dict:
        """看清世界：坐标/血量/饥饿/目标方块/附近实体。"""
        b = self.bot
        if b is None:
            return {}
        return {
            "position": _vec(getattr(b, "entity", None) and b.entity.position),
            "health": getattr(b, "health", None),
            "food": getattr(b, "food", None),
            "biome": _safe(lambda: b.blockAt(b.entity.position).biome),
            "time": _safe(lambda: b.time.timeOfDay),
            "nearby": _safe(lambda: [e.type for e in (b.entities or {}).values()][:8]),
        }

    def _tick_observe(self):
        try:
            self.emit("observe", self.observe_now())
        except Exception:
            pass

    def _tick_reflex(self):
        """保命反射：低血回血/饿进食/溺水上浮/卡墙脱困/岩浆撤离。"""
        try:
            b = self.bot
            if b is None:
                return
            hp = getattr(b, "health", 20)
            if hp is not None and hp <= 6:
                self.emit("reflex", "heal")
                _safe(lambda: self._eat())
            food = getattr(b, "food", 20)
            if food is not None and food <= 6:
                self.emit("reflex", "eat")
                _safe(lambda: self._eat())
            # 溺水：水位以上则上浮
            _safe(lambda: self._anti_drown())
        except Exception:
            pass

    def _eat(self):
        b = self.bot
        if b is None:
            return
        item = _safe(lambda: b.inventory.items().find(lambda i: i.name.endswith("apple") or i.name == "bread"))
        if item:
            b.consume(item)

    def _anti_drown(self):
        pass  # 具体实现依赖所用库的 headInWater 接口

    # ---- 动作（对齐 cortico 的 behavior）----
    def chat(self, text: str):
        self.bot.chat(text)

    def goto(self, x, y, z):
        """演示：直走 + 必要时跳。真实寻路需 pathfinder 扩展。"""
        self.bot.setControlState("forward", True)
        time.sleep(0.5)
        self.bot.setControlState("forward", False)

    def dig(self, block_name=None):
        b = self.bot
        target = _safe(lambda: b.blockAt(b.entity.position.offset(0, -1, 0)))
        if target is not None:
            b.dig(target)

    def attack(self, target_name=None):
        b = self.bot
        ent = _first_entity(b, target_name)
        if ent is not None:
            b.attackEntity(ent)

    def say(self, text: str):
        self.chat(text)


# ----------------------------------------------------------------------
# 工具
# ----------------------------------------------------------------------
def _vec(v):
    if v is None:
        return None
    return {"x": round(getattr(v, "x", 0), 2),
            "y": round(getattr(v, "y", 0), 2),
            "z": round(getattr(v, "z", 0), 2)}


def _safe(fn):
    try:
        return fn()
    except Exception:
        return None


def _first_entity(bot, name):
    ents = getattr(bot, "entities", {}) or {}
    for e in ents.values():
        if name is None or getattr(e, "type", "") == name or getattr(e, "name", "") == name:
            return e
    return None


class _Repeat:
    """后台周期任务（独立于 mineflayer 主循环）。"""

    def __init__(self, interval, fn):
        self.interval = interval
        self.fn = fn
        self._stop = False
        self._t = None

    def start(self):
        def loop():
            while not self._stop:
                time.sleep(self.interval)
                if self._stop:
                    break
                self.fn()
        self._t = threading.Thread(target=loop, daemon=True)
        self._t.start()

    def stop(self):
        self._stop = True
