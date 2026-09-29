# -*- coding: utf-8 -*-
"""装配层：World 的挂载/停用/重启、工具撞名检查、配置组与环境提示词汇总。

对齐 `cortico/src/world.ts` 的 `WorldAssembly`：
- 挂载状态与启动状态分开；定义实例在停用和重启时重新构造
- 工具名在 bot 内唯一：与保留名或已挂载 World 冲突时拒绝挂载（记入 missing）
- 单个定义构造失败记入 missing，继续构造其他 World，不拖垮启动
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import env_prompt
from .config_schema import describe as describe_group
from .host import FeiyuWorldHost
from .store import EventStore
from .types import (ConfigGroup, EventEnvelope, ToolDef, World, WorldContext,
                    WorldDefinition, WorldHost)

_HERE = os.path.dirname(os.path.abspath(__file__))


def _data_dir() -> str:
    import agent_ctx
    return agent_ctx.agent_storage_dir(os.path.dirname(_HERE))


def _state_file() -> str:
    return os.path.join(_data_dir(), "cortico_worlds.json")


# ---------------------------------------------------------------------------
# 槽位
# ---------------------------------------------------------------------------

@dataclass
class WorldSlot:
    id: str
    label: str = ""
    declared: bool = False
    definition: Optional[WorldDefinition] = None
    instance: Optional[World] = None
    mounted: bool = False
    #: 未能挂载的原因（本地没实现 / 版本不符 / 工具撞名 / 构造失败）
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "declared": self.declared,
            "mounted": self.mounted,
            "reason": self.reason,
            "tools": [t.name for t in (self.instance.tools() if self.instance else [])],
        }


@dataclass
class MissingWorld:
    id: str
    label: str = ""
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"id": self.id, "label": self.label, "reason": self.reason}


# ---------------------------------------------------------------------------
# 装配层
# ---------------------------------------------------------------------------

class WorldAssembly:
    """World 装配与生命周期管理。"""

    def __init__(self,
                 cfg: Optional[Dict[str, Any]] = None,
                 deliver: Optional[Callable[[EventEnvelope, str], Any]] = None,
                 paused: Optional[Callable[[], bool]] = None,
                 state_path: Optional[str] = None,
                 timezone: str = "",
                 bot_name: str = "",
                 secrets: Optional[Callable[[str], str]] = None,
                 store_secret: Optional[Callable[[str, str], None]] = None):
        self.cfg: Dict[str, Any] = cfg if cfg is not None else {}
        self.cfg.setdefault("worlds", {})
        self._deliver = deliver
        self._paused = paused
        self._state_path = state_path or _state_file()
        self.timezone = timezone
        self.bot_name = bot_name
        self._secrets_data = self._load_secrets()
        self._secrets = secrets or self._read_secret
        self._store_secret = store_secret or self._write_secret
        self.store = EventStore()
        self.slots: List[WorldSlot] = []
        self.missing: List[MissingWorld] = []
        self.hosts: Dict[str, FeiyuWorldHost] = {}
        self._reserved: List[str] = []
        self._mounting: Dict[str, asyncio.Lock] = {}
        self._load_state()

    # ---- 配置持久化 ----
    def _load_state(self) -> None:
        """读持久化的 worlds 段（enabled 等），与 cfg 合并（cfg 优先）。"""
        if not os.path.exists(self._state_path):
            return
        try:
            with open(self._state_path, "r", encoding="utf-8") as f:
                saved = json.load(f)
        except (json.JSONDecodeError, OSError):
            return
        if not isinstance(saved, dict):
            return
        worlds = self.cfg.setdefault("worlds", {})
        for wid, section in (saved.get("worlds") or {}).items():
            if not isinstance(section, dict):
                continue
            cur = worlds.setdefault(wid, {})
            for k, v in section.items():
                cur.setdefault(k, v)

    def _persist(self) -> None:
        """把 worlds 段写回盘（深合并语义由 setdefault 保证不丢运行期改的值）。"""
        try:
            d = os.path.dirname(self._state_path)
            if d:
                os.makedirs(d, exist_ok=True)
            tmp = self._state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"worlds": self.cfg.get("worlds", {}),
                           "savedAt": time.time()}, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self._state_path)
        except OSError as e:
            print(f"[CORTICO] 装配状态落盘失败: {e}")

    # ---- 密钥（邀请码 / 凭据）持久化 ----
    def _secrets_path(self) -> str:
        return os.path.join(os.path.dirname(self._state_path), "cortico_secrets.json")

    def _load_secrets(self) -> Dict[str, str]:
        try:
            with open(self._secrets_path(), encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, json.JSONDecodeError):
            return {}
        return d if isinstance(d, dict) else {}

    def _read_secret(self, name: str) -> str:
        v = os.environ.get(name)
        if v:
            return v
        return self._secrets_data.get(name, "")

    def _write_secret(self, name: str, value: str) -> None:
        self._secrets_data[name] = "" if value is None else str(value)
        os.environ[name] = self._secrets_data[name]
        try:
            p = self._secrets_path()
            os.makedirs(os.path.dirname(p), exist_ok=True)
            tmp = p + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._secrets_data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, p)
        except OSError as e:
            print(f"[CORTICO] 密钥持久化失败: {e}")

    def section(self, wid: str) -> Dict[str, Any]:
        return self.cfg["worlds"].setdefault(wid, {"enabled": False})

    def persist(self, wid: str, patch: Dict[str, Any]) -> None:
        """深合并进 `worlds.<id>`（数组整体替换）。"""
        cur = self.section(wid)
        for k, v in (patch or {}).items():
            if isinstance(v, dict) and isinstance(cur.get(k), dict):
                cur[k].update(v)
            else:
                cur[k] = v
        self._persist()

    # ---- 注册 ----
    def register(self, definition: WorldDefinition, declared: bool = False,
                 enabled: Optional[bool] = None) -> WorldSlot:
        """登记一个 World 定义并构造实例（构造失败记入 missing，不抛错）。"""
        wid = definition.id
        defaults = {}
        try:
            defaults = definition.defaults() or {}
        except Exception as e:
            defaults = {}
            print(f"[CORTICO] World {wid} defaults() 失败: {e}")

        # 对齐 Cortico worldDefaults()：段首次出现时 = {…defaults(), enabled: 是否声明}，
        # 已声明的 World 默认启用，其余默认关闭；已有配置段保持原样（只补缺失键）。
        worlds = self.cfg.setdefault("worlds", {})
        sec = worlds.get(wid)
        if not isinstance(sec, dict):
            sec = dict(defaults or {})
            sec["enabled"] = bool(declared)
            worlds[wid] = sec
        else:
            for k, v in (defaults or {}).items():
                sec.setdefault(k, v)
            sec.setdefault("enabled", bool(declared))
        if enabled is not None:
            sec["enabled"] = bool(enabled)

        ctx = self._context(definition)
        try:
            if definition.preflight:
                definition.preflight(ctx)
        except Exception as e:
            self.missing.append(MissingWorld(wid, definition.label, f"激活前置检查失败: {e}"))
            return WorldSlot(wid, definition.label, declared, definition, None, False,
                             f"激活前置检查失败: {e}")

        try:
            instance = definition.create(ctx) if definition.create else None
        except Exception as e:
            self.missing.append(MissingWorld(wid, definition.label, f"构造失败: {e}"))
            return WorldSlot(wid, definition.label, declared, definition, None, False,
                             f"构造失败: {e}")
        if instance is None:
            self.missing.append(MissingWorld(wid, definition.label, "定义未提供 create()"))
            return WorldSlot(wid, definition.label, declared, definition, None, False,
                             "定义未提供 create()")

        slot = WorldSlot(wid, definition.label or wid, declared, definition, instance, False)
        old = next((s for s in self.slots if s.id == wid), None)
        if old is not None:
            self.slots.remove(old)
        self.slots.append(slot)
        self._mounting.setdefault(wid, asyncio.Lock())
        return slot

    def add_prebuilt(self, instance: World, label: str = "") -> WorldSlot:
        """加入一个预建实例（不经 create 构造，重启复用原对象）。"""
        slot = WorldSlot(instance.id, label or instance.id, True, None, instance, False)
        old = next((s for s in self.slots if s.id == slot.id), None)
        if old is not None:
            self.slots.remove(old)
        self.slots.append(slot)
        self._mounting.setdefault(slot.id, asyncio.Lock())
        return slot

    def set_reserved_tool_names(self, names: List[str]) -> None:
        """Core/Persona 保留的工具名（World 不得占用）。"""
        self._reserved = list(names or [])

    def _context(self, definition: WorldDefinition) -> WorldContext:
        return WorldContext(
            id=definition.id,
            cfg=self.section(definition.id),
            timezone=self.timezone,
            bot_name=self.bot_name,
            bot_dir=_data_dir(),
            package_dir=_data_dir(),
            data_dir=_data_dir(),
            repo_root=os.path.dirname(_HERE),
            secret=self._secrets,
            store_secret=self._store_secret,
            persist=lambda patch: self.persist(definition.id, patch),
            restart=lambda self=self, wid=definition.id: self.sync(wid),
        )

    def _host(self, wid: str) -> FeiyuWorldHost:
        h = self.hosts.get(wid)
        if h is None:
            h = FeiyuWorldHost(wid, self.store, deliver=self._deliver, paused=self._paused)
            self.hosts[wid] = h
        return h

    # ---- 工具撞名 ----
    def tool_clash(self, world: World, others: List[World]) -> Optional[str]:
        """返回撞名原因；不撞返回 None。"""
        names = {t.name for t in (world.tools() or [])}
        reserved = [n for n in self._reserved if n in names]
        if reserved:
            return f"工具名已被 Core 或 Persona 占用: {', '.join(sorted(reserved))}"
        for other in others:
            shared = [t.name for t in (other.tools() or []) if t.name in names]
            if shared:
                return f"工具名撞名: {', '.join(sorted(shared))}"
        return None

    # ---- 生命周期 ----
    def slot(self, wid: str) -> Optional[WorldSlot]:
        return next((s for s in self.slots if s.id == wid), None)

    def mounted(self) -> List[WorldSlot]:
        return [s for s in self.slots if s.mounted and s.instance is not None]

    def instances(self) -> List[World]:
        return [s.instance for s in self.slots if s.instance is not None]

    async def activate(self, wid: str, language: str = "zh") -> str:
        slot = self.slot(wid)
        if slot is None:
            return f"未知 World: {wid}"
        if slot.mounted:
            return f"{slot.label} 已启用"
        if slot.instance is None:
            return f"{slot.label} 无法启用: {slot.reason or '没有实例'}"
        async with self._mounting.setdefault(wid, asyncio.Lock()):
            clash = self.tool_clash(slot.instance, [s.instance for s in self.mounted() if s.instance])
            if clash:
                slot.reason = clash
                self.missing.append(MissingWorld(wid, slot.label, clash))
                return f"拒绝挂载：{clash}"
            try:
                await slot.instance.start(self._host(wid))
            except Exception as e:
                slot.reason = f"启动失败: {e}"
                return f"{slot.label} 启动失败: {e}"
            slot.mounted = True
            slot.reason = ""
            self.section(wid)["enabled"] = True
            self._persist()
            return f"{slot.label}（{wid}）已启用"

    async def deactivate(self, wid: str) -> str:
        slot = self.slot(wid)
        if slot is None:
            return f"未知 World: {wid}"
        was = slot.mounted
        if was:
            await self._stop_slot(slot)
        self.section(wid)["enabled"] = False
        self._persist()
        return f"{slot.label} 已停用" if was else f"{slot.label} 未启用"

    async def restart(self, wid: str) -> str:
        slot = self.slot(wid)
        if slot is None:
            return f"未知 World: {wid}"
        if not slot.mounted:
            return f"{slot.label} 未启用，没有可重启的实例"
        await self._stop_slot(slot)
        res = await self.activate(wid)
        return f"{slot.label} 已重启" if "已启用" in res else res

    async def _stop_slot(self, slot: WorldSlot) -> None:
        if slot.instance is not None:
            try:
                await slot.instance.stop()
            except Exception as e:
                print(f"[CORTICO] World {slot.id} 停止异常: {e}")
        slot.mounted = False
        if slot.definition is not None:
            try:
                slot.instance = slot.definition.create(self._context(slot.definition))
            except Exception as e:
                slot.instance = None
                slot.reason = f"重启后重建失败: {e}"

    async def sync(self, wid: str) -> None:
        """按 `worlds.<id>.enabled` 同步挂载状态。"""
        slot = self.slot(wid)
        if slot is None:
            return
        enabled = bool(self.section(wid).get("enabled"))
        if enabled and not slot.mounted:
            await self.activate(wid)
        elif not enabled and slot.mounted:
            await self.deactivate(wid)

    async def start_enabled(self) -> List[str]:
        """启动所有 enabled 的 World（启动期调用）。返回回执列表。"""
        out = []
        for slot in list(self.slots):
            if not bool(self.section(slot.id).get("enabled")):
                continue
            out.append(await self.activate(slot.id))
        return out

    async def stop_all(self) -> None:
        for slot in reversed(self.slots):
            if slot.mounted:
                await self._stop_slot(slot)

    # ---- 汇总：工具 / 配置 / 环境提示词 ----
    def tools(self) -> List[ToolDef]:
        """已挂载 World 的全部工具（含 handler）。"""
        out: List[ToolDef] = []
        for s in self.mounted():
            if s.instance is None:
                continue
            try:
                out.extend(s.instance.tools() or [])
            except Exception as e:
                print(f"[CORTICO] World {s.id} tools() 失败: {e}")
        return out

    def tool_names(self) -> List[str]:
        return [t.name for t in self.tools()]

    def config_groups(self) -> List[ConfigGroup]:
        """全部槽位（含未挂载）的配置组，供配置页展示。"""
        out: List[ConfigGroup] = []
        for s in self.slots:
            if s.instance is None:
                continue
            try:
                decl = s.instance.console("zh")
            except Exception as e:
                print(f"[CORTICO] World {s.id} console() 失败: {e}")
                continue
            if decl and decl.config:
                out.extend(decl.config)
        return out

    def config_view(self) -> List[Dict[str, Any]]:
        return [describe_group(g, self.cfg) for g in self.config_groups()]

    async def env_prompt_segments(self, language: str = "zh") -> List[Dict[str, str]]:
        """渲染各 World 的环境提示词段（role=envPrompt 的模板 + envPromptVars）。

        返回 [{"id", "title", "text", "source_key"}]；World 返回 None 时跳过该段。
        """
        segs: List[Dict[str, str]] = []
        for s in self.mounted():
            inst = s.instance
            if inst is None:
                continue
            try:
                decl = inst.console(language)
            except Exception as e:
                print(f"[CORTICO] World {s.id} console() 失败: {e}")
                continue
            docs = [d for d in (decl.prompt_docs if decl else []) if d.role == "envPrompt"]
            if not docs:
                continue
            try:
                variables = inst.env_prompt_vars()
                # Node 桥的 World 变量在远端，每次渲染前重新拉一次（同步接口是缓存快照）
                refresh = getattr(inst, "refresh_vars", None)
                if callable(refresh):
                    got = refresh()
                    if hasattr(got, "__await__"):
                        got = await got
                    variables = got if got is not None else variables
            except Exception as e:
                print(f"[CORTICO] World {s.id} envPromptVars() 失败: {e}")
                variables = None
            if variables is None:
                continue
            variables = dict(variables or {})
            for doc in docs:
                text = env_prompt.render_file(doc.path, variables).strip()
                if not text:
                    continue
                segs.append({"id": s.id, "title": doc.title or s.label,
                             "text": text, "source_key": doc.key})
        return segs

    def status(self) -> Dict[str, Any]:
        return {
            "mounted": [s.to_dict() for s in self.mounted()],
            "slots": [s.to_dict() for s in self.slots],
            "missing": [m.to_dict() for m in self.missing],
            "tools": self.tool_names(),
            "events": self.store.stats(),
        }
