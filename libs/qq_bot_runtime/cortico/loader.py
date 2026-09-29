# -*- coding: utf-8 -*-
"""把 Cortico 扩展包（TS）装配进 feiyu。

两类来源：
1. `CORTICO_PACKAGE_ROOTS` 下发现的 `cortico-world-*` 包（TS，经 Node 桥运行）
2. Python 侧按 api=5 契约自己定义的 World（由 `register_python_world` 登记）

两者都进同一个 `WorldAssembly`，共享事件库、工具表与配置组。
"""
from __future__ import annotations

import os
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import manifest as _manifest
from .manifest import CorticoManifest, discover
from .node_bridge import NodeWorld, NodeWorldBridge
from .registry import WorldAssembly
from .types import WorldDefinition

_PYTHON_WORLDS: Dict[str, WorldDefinition] = {}


def register_python_world(definition: WorldDefinition) -> None:
    """登记一个 Python 实现的 World 定义（按 api=5 契约写的）。"""
    _PYTHON_WORLDS[definition.id] = definition


def python_worlds() -> Dict[str, WorldDefinition]:
    return dict(_PYTHON_WORLDS)


def _cfg():
    import config
    return config


def package_roots() -> List[str]:
    cfg = _cfg()
    roots = list(getattr(cfg, "CORTICO_PACKAGE_ROOTS", []) or [])
    return [r for r in roots if r and os.path.isdir(r)]


def core_root() -> str:
    return str(getattr(_cfg(), "CORTICO_CORE_ROOT", "") or "")


def discover_packages() -> List[CorticoManifest]:
    """发现全部可用的扩展包（含不可加载的，供面板展示原因）。"""
    return discover(package_roots())


def host_callbacks(assembly: WorldAssembly, wid: str) -> Dict[str, Callable]:
    """Node 侧回调 Python 的能力表（对齐 `node/cortico_host.mjs` 里的方法名）。"""
    host = assembly._host(wid)

    def _push(p: Dict[str, Any]):
        from .types import EventEnvelope, PushOptions
        e = EventEnvelope(
            type=str(p.get("type") or ""),
            ts=str(p.get("ts") or ""),
            source=str(p.get("source") or wid),
            text=str(p.get("text") or ""),
            origin=str(p.get("origin") or "external"),
            tags=list(p.get("tags") or []),
            sender_key=str(p.get("senderKey") or ""),
            meta=dict(p.get("meta") or {}),
        )
        return _spawn(host.push_event(e, PushOptions(
            deliver=bool(p.get("deliver", True)),
            trigger=p.get("trigger") or None)))

    def _spawn(coro):
        try:
            loop = __import__("asyncio").get_event_loop()
        except RuntimeError:
            return None
        if loop.is_running():
            return loop.create_task(coro)
        return loop.run_until_complete(coro)

    def _push_deferred(p: Dict[str, Any]):
        from .types import DeferredEventSpec, EventEnvelope, PushOptions
        text = str(p.get("text") or "")
        spec = DeferredEventSpec(
            type=str(p.get("type") or ""),
            source=str(p.get("source") or wid),
            origin=str(p.get("origin") or "external"),
            sender_key=str(p.get("senderKey") or ""),
            meta=dict(p.get("meta") or {}),
            render=lambda: text,
        )
        host.push_deferred(spec, p.get("trigger"))
        return True

    return {
        "host.pushEvent": _push,
        "host.pushDeferred": _push_deferred,
        "host.drainPending": lambda p: _spawn(host.drain_pending_events(
            lambda e: e.source == wid)),
        "host.storeSince": lambda p: host.store.since(int(p.get("cursor") or 0),
                                                      int(p.get("limit") or 200)),
        "host.storeLatest": lambda p: host.store.latest(),
        "host.storeCount": lambda p: host.store.count(),
        "host.reportUsage": lambda p: host.report_usage(type("U", (), {
            "prompt_tokens": int(p.get("promptTokens") or 0),
            "completion_tokens": int(p.get("completionTokens") or 0)})()),
        "host.log": lambda p: host.log(str(p.get("level") or "info"),
                                       str(p.get("message") or ""), **(p.get("data") or {})),
        "host.persist": lambda p: assembly.persist(wid, dict(p.get("patch") or {})),
        "host.storeSecret": lambda p: assembly._store_secret(str(p.get("name") or ""),
                                                             str(p.get("value") or "")),
        "host.restart": lambda p: _spawn(assembly.sync(wid)),
    }


async def mount_packages(assembly: WorldAssembly,
                         enabled: Optional[Dict[str, bool]] = None,
                         only: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """发现扩展包并登记进装配层。返回每个包的处理结果（供面板展示）。

    不在这里 start：挂载由 `assembly.start_enabled()` 按 `enabled` 统一进行。
    """
    enabled = enabled or {}
    results: List[Dict[str, Any]] = []

    # 1) Python 侧定义的 World
    for wid, definition in python_worlds().items():
        if only and wid not in only:
            continue
        slot = assembly.register(definition, declared=True, enabled=enabled.get(wid))
        results.append({"id": wid, "source": "python", "ok": bool(slot.instance),
                        "reason": slot.reason})

    # 2) Node 侧的 TS 包
    for m in discover_packages():
        if only and m.name not in only:
            continue
        ok, why = m.compatible()
        if not ok:
            assembly.missing.append(type("MissingWorld", (), {})(
                id=m.name, label=m.label, reason=why) if False else _missing(m.name, m.label, why))
            results.append({"id": m.name, "source": "node", "ok": False, "reason": why})
            continue
        wid = m.name
        cfg_section = assembly.section(wid)
        if enabled.get(wid) is not None:
            cfg_section["enabled"] = bool(enabled[wid])
        bridge = NodeWorldBridge(
            manifest=m,
            cfg=cfg_section,
            timezone=assembly.timezone,
            bot_name=assembly.bot_name,
            secrets={"DUNGEON_INVITE_CODE": assembly._secrets("DUNGEON_INVITE_CODE"),
                     "DUNGEON_CREDENTIAL": assembly._secrets("DUNGEON_CREDENTIAL")},
            host_callbacks=host_callbacks(assembly, wid),
            core_root=core_root(),
        )
        try:
            await bridge.start()
            world = NodeWorld(bridge, fallback_id=wid)
            await world._prepare()
            await world.refresh_vars()
            slot = assembly.add_prebuilt(world, label=m.label)
            # 用包自己的 defaults 补默认配置段（首次运行）
            try:
                defaults = await bridge.defaults()
                for k, v in (defaults or {}).items():
                    cfg_section.setdefault(k, v)
            except Exception:
                pass
            results.append({"id": wid, "source": "node", "ok": True,
                            "worldId": world.id, "tools": [t.name for t in world.tools()]})
        except Exception as e:
            assembly.missing.append(_missing(wid, m.label, str(e)))
            results.append({"id": wid, "source": "node", "ok": False, "reason": str(e)})
            try:
                await bridge.close()
            except Exception:
                pass
    return results


def _missing(wid: str, label: str, reason: str):
    from .registry import MissingWorld
    return MissingWorld(id=wid, label=label, reason=reason)
