# -*- coding: utf-8 -*-
"""Bot 管理器：运行时动态增删 / 启停多个 bot（每个 bot = 一个独立 AgentCore）。

设计要点：
- 每个 bot 一个 agent_id，对应一个 AgentCore 实例（get_core 按 agent_id 分桶）。
- 记忆 / 人格 / 插件完全隔离：记忆子系统按 agent_ctx.current_agent() 落地到
  agents/<id>/memory；人格经 core._persona_override 注入；每个 core 自带 PluginManager。
- 注册表持久化在 data/bots.json（运行时数据，不进版本库）。
- 默认主 bot 固定为 "feiyu"（沿用既有单 bot 的行为，记忆落在引擎目录，不迁移）。
"""
import os
import json
import time
import threading
import shutil

from quiet import degrade

DEFAULT_BOT_ID = "feiyu"


def _reg_path() -> str:
    return os.path.join("data", "bots.json")


def _slug(name: str) -> str:
    s = "".join(ch if (ch.isalnum() or ch in "-_") else "_" for ch in (name or "").strip())
    return s or "bot"


def _default_spec(bot_id: str, name: str) -> dict:
    return {
        "id": bot_id,
        "name": name,
        "persona": None,      # None -> 沿用 config.SYSTEM_PROMPT（基座人设）
        "model": None,       # None -> 沿用全局模型配置
        "plugins": None,     # None -> 默认（全部功能插件）；列表 -> 仅启用这些
        "autostart": True,
        "enabled": True,
        "running": False,     # 运行时状态（不持久化为真，启动后由管理器改写内存态）
        "created_at": time.time(),
    }


class BotManager:
    """多 bot 生命周期管理器（由 CoreBridge 持有）。"""

    def __init__(self, bridge):
        self.bridge = bridge
        self.specs = self._load()
        self.cores = {}            # bot_id -> AgentCore（仅运行中的）
        self._lock = threading.Lock()
        self._ensure_default()

    # ---- 注册表持久化 ----
    def _load(self) -> list:
        try:
            with open(_reg_path(), "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                return data
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        return []

    def _save(self):
        try:
            os.makedirs(os.path.dirname(_reg_path()), exist_ok=True)
            with open(_reg_path(), "w", encoding="utf-8") as f:
                json.dump(self.specs, f, ensure_ascii=False, indent=2)
        except OSError as e:
            degrade("bridge/bot_manager.py:save", e, "bots.json 保存失败")

    def _ensure_default(self):
        if not any(s.get("id") == DEFAULT_BOT_ID for s in self.specs):
            self.specs.insert(0, _default_spec(DEFAULT_BOT_ID, "肥鱼娘"))
            self._save()

    # ---- 查询 ----
    def list_specs(self) -> list:
        out = []
        for s in self.specs:
            d = dict(s)
            d["running"] = s.get("id") in self.cores
            out.append(d)
        return out

    def get_spec(self, bot_id: str):
        for s in self.specs:
            if s.get("id") == bot_id:
                d = dict(s)
                d["running"] = bot_id in self.cores
                return d
        return None

    def is_running(self, bot_id: str) -> bool:
        return bot_id in self.cores

    def core_for(self, bot_id: str = None):
        if bot_id and bot_id in self.cores:
            return self.cores[bot_id]
        return self.cores.get(DEFAULT_BOT_ID) or getattr(self.bridge, "core", None)

    # ---- 增删改 ----
    def create_bot(self, name: str, persona=None, model=None, plugins=None,
                   autostart: bool = False) -> dict:
        base = _slug(name)
        bot_id = base
        i = 1
        while any(s.get("id") == bot_id for s in self.specs):
            bot_id = f"{base}_{i}"
            i += 1
        spec = _default_spec(bot_id, name)
        spec["persona"] = persona or None
        spec["model"] = model or None
        spec["plugins"] = plugins
        spec["autostart"] = bool(autostart)
        self.specs.append(spec)
        self._save()
        return self.get_spec(bot_id)

    def update_bot(self, bot_id: str, **fields) -> dict:
        allowed = {"name", "persona", "model", "plugins", "autostart", "enabled"}
        spec = None
        for idx, s in enumerate(self.specs):
            if s.get("id") == bot_id:
                spec = s
                break
        if spec is None:
            return None
        for k, v in fields.items():
            if k in allowed:
                spec[k] = v
        # 运行中修改人格 / 模型需重启生效
        if bot_id in self.cores and ("persona" in fields or "model" in fields
                                     or "plugins" in fields):
            self.restart_bot(bot_id)
        self._save()
        return self.get_spec(bot_id)

    def delete_bot(self, bot_id: str) -> bool:
        if bot_id == DEFAULT_BOT_ID:
            return False
        self.stop_bot(bot_id, wait=False)
        self.specs = [s for s in self.specs if s.get("id") != bot_id]
        self._save()
        # 清理该 bot 命名空间目录（记忆 / 人格等）
        try:
            import agent_ctx
            d = agent_ctx.ns_dir(bot_id)  # agents/<id>/memory
            if d:
                parent = os.path.dirname(d)  # agents/<id>
                if os.path.isdir(parent):
                    shutil.rmtree(parent, ignore_errors=True)
        except Exception:
            pass
        return True

    # ---- 启动 / 停止 ----
    def _build_core(self, bot_id: str, spec: dict):
        import config as qq_config
        import plugin_registry as reg
        from agent_core import get_core
        core = get_core(bot_id)
        try:
            core.app_bridge = self.bridge
        except Exception as e:
            print(f"[BOTMGR][WARN] 注入 app_bridge 失败: {e!r}")
        core._persona_override = spec.get("persona") or None
        core._model_override = spec.get("model") or None
        saved = {}
        for s in reg.by_kind("feature"):
            if s.switch:
                saved[s.switch] = getattr(qq_config, s.switch, s.default_on)
                setattr(qq_config, s.switch, False)
        try:
            platforms = spec.get("plugins") or []
            core.register_builtin_plugins(platforms=platforms)
        finally:
            for k, v in saved.items():
                setattr(qq_config, k, v)
        if self.bridge._build_hook:
            try:
                self.bridge._build_hook(core)
            except Exception as e:
                print(f"[BOTMGR][WARN] 插件包预装载异常: {e!r}")
        return core

    async def _astart(self, bot_id: str):
        spec = self.get_spec(bot_id)
        if spec is None or bot_id in self.cores:
            return
        core = self._build_core(bot_id, spec)
        await core.start()
        self.cores[bot_id] = core
        if bot_id == DEFAULT_BOT_ID:
            self.bridge.core = core

    async def _astop(self, bot_id: str):
        core = self.cores.pop(bot_id, None)
        if bot_id == DEFAULT_BOT_ID:
            self.bridge.core = None
        if core is not None:
            try:
                await core.shutdown()
            except Exception as e:
                degrade(f"bot_manager.stop.{bot_id}", e, "bot 停止异常")

    def start_bot(self, bot_id: str, wait: bool = True) -> dict:
        with self._lock:
            if bot_id in self.cores:
                return {"ok": True, "state": "running"}
        if wait:
            self.bridge.lt.run_coro(self._astart(bot_id), timeout=180)
        else:
            self.bridge.lt.schedule(self._astart(bot_id))
        return {"ok": True, "state": "running" if wait else "starting"}

    def stop_bot(self, bot_id: str, wait: bool = True) -> dict:
        if wait:
            self.bridge.lt.run_coro(self._astop(bot_id), timeout=60)
        else:
            self.bridge.lt.schedule(self._astop(bot_id))
        return {"ok": True}

    def restart_bot(self, bot_id: str) -> dict:
        self.stop_bot(bot_id, wait=True)
        time.sleep(0.5)
        return self.start_bot(bot_id, wait=True)

    def autostart_all(self):
        """启动全部 autostart/enabled 且未运行的 bot（非阻塞，逐个 schedule）。"""
        started = []
        for s in self.specs:
            bid = s.get("id")
            if bid in self.cores:
                continue
            if s.get("autostart", True) and s.get("enabled", True):
                self.start_bot(bid, wait=False)
                started.append(bid)
        return started
