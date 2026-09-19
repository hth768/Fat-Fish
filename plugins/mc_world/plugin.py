# -*- coding: utf-8 -*-
"""mc_world —— world 类型示例插件（肥鱼单机版）。

用 Python 版 mineflayer 接入原版 Minecraft，把游戏世界接进肥鱼。
复用 cortico-world-mc-agent 身体层语义：
  connect / disconnect / world-state / chat / 保命反射 / 指令(goto/dig/attack/say/scan)。

设计要点：
  * 作为「世界包」(kind="world")，走与大脑相同的 create_brain + core.brains.register 通道，
    因此天然获得统一生命周期、/大脑 状态查询、brain.event 事件通道。
  * 通过 core.app_bridge.push 把世界事件推到界面（与 feature 插件相同官方通道）。
  * auto_start_on_core=False：世界需要用户主动 connect，不随核心自启。
  * mineflayer 依赖采用懒导入（见 body.py），未装依赖时插件仍可 load，运行 connect 时才提示。
"""
import time
import threading


def _push(core, session, event: dict):
    """经官方通道把事件推到界面；通道不存在时静默丢弃。"""
    try:
        bridge = getattr(core, "app_bridge", None)
        if bridge is not None:
            bridge.push(session, event)
    except Exception:
        pass


def create_brain(core):
    """构造 world 包大脑实例（AgentBrain 子类）。"""
    return MinecraftWorldBrain(core)


class MinecraftWorldBrain:
    """把 Minecraft 世界接入肥鱼的大脑（外部世界形态）。

    继承 AgentBrain 以获得统一生命周期/状态/事件通道。
    注：这里用组合方式显式实现 AgentBrain 契约的字段与方法，
    避免顶层 import 引擎基类导致扫描期强耦合（保持与 body.py 一致的惰性策略）。
    """

    name = "mc_world"
    title = "Minecraft 世界"
    kind = "external"        # 外部世界：核心只注册它的状态/事件通道，由用户主动 connect
    description = "world 类型示例：用 mineflayer 接入原版 Minecraft，把游戏世界接进肥鱼。"
    auto_start_on_core = False

    def __init__(self, core):
        self.core = core
        self.session = None
        self.bot = None
        self._thread = None
        self._stop = False
        self._started = False
        self.cfg = {}

    # ---- Plugin / AgentBrain 契约 ----
    @property
    def started(self) -> bool:
        return self._started

    async def start(self):
        self._started = True

    async def stop(self):
        self._started = False
        self._do_disconnect()

    def status(self) -> dict:
        return {
            "name": self.name, "kind": "world", "world_domain": "game",
            "running": self._started,
            "connected": self.bot is not None,
        }

    # ---- 配置（feature/brain 插件约定）----
    def on_config(self, cfg: dict):
        self.cfg = dict(cfg or {})

    def on_agent_reply(self, text: str, session=None):
        """App 里用户/助手说的话含 @mc 指令时，驱身体动作。"""
        self.session = session or self.session
        cmd = _parse_command(text)
        if cmd is None:
            return
        self._dispatch(cmd, session)

    def on_unload(self):
        self._do_disconnect()

    # ---- 世界控制（供 App / 命令触发）----
    def connect(self, cfg=None, session=None):
        if cfg:
            self.cfg = dict(cfg)
        if session:
            self.session = session
        if self._thread and self._thread.is_alive():
            return {"ok": True, "hint": "已在连接/已连接"}
        self._stop = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return {"ok": True, "hint": "连接线程已启动"}

    def disconnect(self):
        return self._do_disconnect()

    # ---- 内部 ----
    def _do_disconnect(self):
        self._stop = True
        try:
            if self.bot is not None:
                self.bot.disconnect()
        except Exception:
            pass
        self.bot = None
        _push(self.core, self.session, {
            "type": "world", "domain": "game",
            "event": "disconnected", "text": "已断开 Minecraft"})

    def _run(self):
        from .body import MinecraftBody
        cfg = self.cfg
        attempt = 0
        while not self._stop and attempt < 5:
            attempt += 1
            try:
                bot = MinecraftBody(
                    host=cfg.get("host", "127.0.0.1"),
                    port=int(cfg.get("port", 25565)),
                    username=cfg.get("username", "FeiyuBot"),
                    version=None if cfg.get("version", "auto") == "auto" else cfg.get("version"),
                )
                self.bot = bot
                _push(self.core, self.session, {
                    "type": "world", "domain": "game", "event": "connected",
                    "text": f"已接入 {cfg.get('host')}:{cfg.get('port')}"})
                self._wire(bot)
                bot.run()
            except Exception as e:
                _push(self.core, self.session, {
                    "type": "world", "domain": "game", "event": "error",
                    "text": f"连接异常: {e!r}"})

            if self._stop:
                break
            time.sleep(3)
        self.bot = None

    def _wire(self, bot):
        cfg = self.cfg
        auto_chat = bool(cfg.get("auto_chat_relay", True))
        auto_reflex = bool(cfg.get("auto_reflex", True))
        interval = max(5000, int(cfg.get("observe_interval_ms", 22000)))

        bot.on("chat", lambda u, m: auto_chat and _push(
            self.core, self.session,
            {"type": "world", "domain": "game", "event": "chat", "from": u, "text": m}))
        bot.on("observe", lambda s: _push(
            self.core, self.session,
            {"type": "world", "domain": "game", "event": "world_state", "state": s}))
        bot.on("reflex", lambda a: auto_reflex and _push(
            self.core, self.session,
            {"type": "world", "domain": "game", "event": "reflex", "action": a}))
        bot.start_observe(interval)

    def _dispatch(self, cmd, session):
        bot = self.bot
        op = cmd["op"]
        if bot is None:
            _push(self.core, session, {"type": "world", "domain": "game",
                                       "event": "error", "text": "世界未连接，先连服。"})
            return
        try:
            if op == "say":
                bot.chat(cmd["arg"])
            elif op == "goto":
                xyz = [float(x) for x in cmd["arg"].split()]
                bot.goto(xyz[0], xyz[1], xyz[2])
            elif op == "dig":
                bot.dig(cmd["arg"] or None)
            elif op == "attack":
                bot.attack(cmd["arg"] or None)
            elif op == "scan":
                bot.emit("observe", bot.observe_now())
            else:
                _push(self.core, session, {"type": "world", "domain": "game",
                                           "event": "error", "text": f"未知指令: {op}"})
                return
            _push(self.core, session, {"type": "world", "domain": "game",
                                       "event": "action", "action": f"{op}: {cmd['arg']}"})
        except Exception as e:
            _push(self.core, session, {"type": "world", "domain": "game",
                                       "event": "error", "text": f"执行 {op} 失败: {e!r}"})


def _parse_command(text: str):
    """极简指令解析：@mc goto x y z / dig / attack / say ... / scan。"""
    if not text:
        return None
    t = text.strip()
    if not t.lower().startswith("@mc"):
        return None
    body = t[len("@mc"):].strip()
    if not body:
        return None
    parts = body.split(None, 1)
    op = parts[0].lower()
    arg = parts[1] if len(parts) > 1 else ""
    return {"op": op, "arg": arg}
