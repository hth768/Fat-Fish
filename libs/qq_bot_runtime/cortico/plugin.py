# -*- coding: utf-8 -*-
"""Cortico 兼容层的功能插件：把 World 装配层挂进 feiyu 核心生命周期。

启动顺序（对齐 Cortico：读配置 -> 装载包 -> 构造 World -> 按 enabled 挂载）：
1. 建装配层（事件库 / 投递回调 / 暂停回调）
2. 登记 Python World 定义 + 发现并装载 Node 侧 TS 包
3. `start_enabled()`：只挂载 `worlds.<id>.enabled` 为真的
4. 拉起世界大脑（消费事件、驱动模型）

停止：反向停掉全部已挂载 World，关闭 Node 子进程。
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

from plugin_base import FeaturePlugin
from .brain import CorticoWorldBrain
from .loader import mount_packages
from .registry import WorldAssembly

_assembly: Optional[WorldAssembly] = None
_brain: Optional[CorticoWorldBrain] = None
_plugin: Optional["CorticoWorldPlugin"] = None


def _cfg():
    import config
    return config


def get_assembly() -> Optional[WorldAssembly]:
    """当前装配层（未初始化时 None）。"""
    return _assembly


def get_brain() -> Optional[CorticoWorldBrain]:
    return _brain


class CorticoWorldPlugin(FeaturePlugin):
    """Cortico World 兼容层的生命周期宿主。"""

    name = "cortico_worlds"

    def __init__(self, core):
        super().__init__(core)
        self.enabled_ids: List[str] = []
        self.mount_results: List[Dict[str, Any]] = []

    # ---- 生命周期 ----
    async def start(self):
        global _assembly, _brain, _plugin
        cfg = _cfg()
        if not getattr(cfg, "CORTICO_ENABLED", False):
            print("[CORTICO] 兼容层未启用（config.CORTICO_ENABLED=False）")
            return

        _plugin = self
        _assembly = WorldAssembly(
            cfg={"worlds": dict(getattr(cfg, "CORTICO_WORLDS", {}) or {})},
            deliver=self._deliver,
            paused=lambda: bool(_brain and _brain.is_paused),
            timezone=str(getattr(cfg, "CORTICO_TIMEZONE", "") or ""),
            bot_name=str(getattr(cfg, "DISPLAY_NAME", "") or getattr(cfg, "BOT_NAME", "") or ""),
        )
        _brain = CorticoWorldBrain(self.core, _assembly)

        worlds_cfg = getattr(cfg, "CORTICO_WORLDS", {}) or {}
        enabled = {wid: bool((sec or {}).get("enabled"))
                   for wid, sec in worlds_cfg.items() if isinstance(sec, dict)}
        only = list(getattr(cfg, "CORTICO_LOAD_ONLY", []) or []) or None

        self.mount_results = await mount_packages(_assembly, enabled=enabled, only=only)
        for r in self.mount_results:
            print(f"[CORTICO] 装载 {r['id']}（{r.get('source', '?')}）:"
                  f"{'OK' if r.get('ok') else '失败 - ' + str(r.get('reason', ''))}")

        receipts = await _assembly.start_enabled()
        for line in receipts:
            print(f"[CORTICO] {line}")

        if getattr(cfg, "CORTICO_BRAIN_ENABLED", True) and _assembly.mounted():
            await _brain.start()

        self.enabled_ids = [s.id for s in _assembly.mounted()]
        await super().start()
        print(f"[CORTICO] 兼容层已启动，已挂载: {', '.join(self.enabled_ids) or '（无）'}")

    async def _deliver(self, e, trigger):
        if _brain is None:
            return
        await _brain.deliver(e, trigger)

    async def stop(self):
        global _assembly, _brain
        if _brain is not None:
            await _brain.stop()
        if _assembly is not None:
            await _assembly.stop_all()
            # 关掉所有 Node 子进程
            for slot in _assembly.slots:
                bridge = getattr(slot.instance, "bridge", None)
                if bridge is not None:
                    try:
                        await bridge.close()
                    except Exception as e:
                        print(f"[CORTICO] 关闭 Node 桥失败: {e}")
        _brain = None
        _assembly = None
        await super().stop()
        print("[CORTICO] 兼容层已停止")

    # ---- 状态 ----
    def status(self) -> Dict[str, Any]:
        d = {**super().status()}
        if _assembly is not None:
            d["assembly"] = _assembly.status()
            d["configGroups"] = _assembly.config_view()
        if _brain is not None:
            d["brain"] = _brain.status()
        d["mountResults"] = self.mount_results
        return d


# ---------------------------------------------------------------------------
# 对外便捷接口（供 bridge / 命令 / 测试调用）
# ---------------------------------------------------------------------------

def status() -> Dict[str, Any]:
    if _plugin is not None:
        return _plugin.status()
    return {"enabled": False, "reason": "兼容层未启动"}


def assembly() -> Optional[WorldAssembly]:
    return _assembly


async def activate(wid: str) -> str:
    if _assembly is None:
        return "兼容层未启动"
    return await _assembly.activate(wid)


async def deactivate(wid: str) -> str:
    if _assembly is None:
        return "兼容层未启动"
    return await _assembly.deactivate(wid)


async def restart(wid: str) -> str:
    if _assembly is None:
        return "兼容层未启动"
    return await _assembly.restart(wid)


async def env_prompt_segments() -> List[Dict[str, str]]:
    """给聊天主线注入的环境提示词段（World 未启用时为空列表）。"""
    if _assembly is None:
        return []
    try:
        return await _assembly.env_prompt_segments()
    except Exception as e:
        print(f"[CORTICO] 环境提示词渲染失败: {e}")
        return []


def config_view() -> List[Dict[str, Any]]:
    return _assembly.config_view() if _assembly else []


def save_config(group_id: str, values: Dict[str, Any]) -> Dict[str, Any]:
    """保存一个配置组的值。返回 {changed, errors, needRestart}。"""
    if _assembly is None:
        return {"ok": False, "error": "兼容层未启动"}
    from . import config_schema
    group = next((g for g in _assembly.config_groups() if g.id == group_id), None)
    if group is None:
        return {"ok": False, "error": f"没有配置组 {group_id}"}
    changed, errors = config_schema.apply_patch(_assembly.cfg, group, values)
    if changed:
        _assembly._persist()
    need_restart = [p for p in changed
                    if not config_schema.is_hot(group.schema.properties.get(p, {}))]
    return {"ok": not errors, "changed": changed, "errors": errors, "needRestart": need_restart}
