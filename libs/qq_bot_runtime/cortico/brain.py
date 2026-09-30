# -*- coding: utf-8 -*-
"""Cortico 世界大脑：世界事件 -> 唤醒 -> 模型 -> 工具 -> 输出。

这是 Cortico 的运行模型在 feiyu 里的落点：World 推事件，宿主按 trigger 合批唤醒，
模型看到「环境提示词 + 最近事件」，用 World 的工具回应，说出来的话经统一通道播报。

与聊天主线（`chat_service`）解耦：不占用聊天会话，不阻塞消息处理。
想让聊天也「知道」世界，用 `env_prompt_segments()` 注入 system 前缀即可。
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Dict, List, Optional

from brain_base import AgentBrain
from .registry import WorldAssembly
from .tool_loop import run_tool_turn
from .types import EventEnvelope, TriggerMode


def _cfg():
    import config
    return config


class CorticoWorldBrain(AgentBrain):
    """世界大脑：消费 World 事件并驱动模型。"""

    name = "cortico"
    title = "Cortico 世界大脑"
    kind = "autonomous"
    description = "把 Cortico World 的事件喂给模型，用 World 的工具回应（双架构兼容运行时）"
    auto_start_on_core = False     # 默认不随核心自启：由 CorticoWorldPlugin 按需拉起

    def __init__(self, core, assembly: WorldAssembly):
        super().__init__(core)
        self.assembly = assembly
        self._queue: asyncio.Queue = asyncio.Queue()
        self._loop_task: Optional[asyncio.Task] = None
        self._paused = False
        self.last_text = ""
        self.rounds = 0

    # ---- 宿主投递入口 ----
    async def deliver(self, e: EventEnvelope, trigger: TriggerMode) -> None:
        """装配层回调：World 推来的事件进唤醒队列。"""
        if self._paused:
            return
        await self._queue.put((e, trigger or "debounce"))

    # ---- 生命周期 ----
    async def start(self):
        if self.started:
            return
        self._paused = False
        self._loop_task = asyncio.get_event_loop().create_task(self._run())
        self._started = True
        print("[CORTICO] 世界大脑已启动")

    async def stop(self):
        if self._loop_task:
            self._loop_task.cancel()
            self._loop_task = None
        self._started = False
        print("[CORTICO] 世界大脑已停止")

    def pause(self, on: bool = True):
        """暂停/恢复唤醒（对应控制台「运行/暂停」）。World 的自发行为查 host.isPaused()。"""
        self._paused = bool(on)

    @property
    def is_paused(self) -> bool:
        return self._paused

    # ---- 主循环 ----
    SELF_PLAY_PROMPT = (
        "（自主行动提示）你此刻没有收到新的外部消息，请自己把剧情玩下去："
        "先用 dungeon_look 观察当前状态，再依据世界观与你的角色目标，"
        "做出一个合理的主动行动（探索、养成、社交、接任务、推进主线等）。"
        "保持角色一致，不要等待用户，持续主动推进你的冒险。"
    )

    async def _run(self):
        cfg = _cfg()
        quiet_gap = float(getattr(cfg, "CORTICO_QUIET_GAP_MS", 800)) / 1000.0
        max_batch = int(getattr(cfg, "CORTICO_MAX_BATCH", 20))
        max_age = float(getattr(cfg, "CORTICO_MAX_BATCH_AGE_MS", 6000)) / 1000.0
        max_rounds = int(getattr(cfg, "CORTICO_MAX_TOOL_ROUNDS", 8))
        self_play = bool(getattr(cfg, "CORTICO_SELF_PLAY", True))
        idle_gap = float(getattr(cfg, "CORTICO_SELF_PLAY_IDLE_S", 30))
        pace = float(getattr(cfg, "CORTICO_SELF_PLAY_PACE_S", 5))

        while True:
            try:
                trigger = "debounce"
                try:
                    first = await asyncio.wait_for(
                        self._queue.get(), timeout=idle_gap if self_play else None
                    )
                    batch = [first[0]]
                    trigger = first[1]
                    started = time.time()
                    if trigger in ("debounce", "piggyback"):
                        # 合批窗口：静默 quiet_gap 或达到上限/批龄
                        while len(batch) < max_batch and (time.time() - started) < max_age:
                            try:
                                nxt = await asyncio.wait_for(self._queue.get(), timeout=quiet_gap)
                                batch.append(nxt[0])
                                if nxt[1] == "preempt":
                                    trigger = "preempt"
                                    break
                            except asyncio.TimeoutError:
                                break
                    else:
                        while len(batch) < max_batch and not self._queue.empty():
                            try:
                                batch.append(self._queue.get_nowait()[0])
                            except asyncio.QueueEmpty:
                                break
                    await self._handle_batch(batch, max_rounds=max_rounds, self_prompt=False)
                except asyncio.TimeoutError:
                    # 空闲自驱：像 Cortico 的 onIdle，主动推进游戏
                    if not self_play:
                        continue
                    await self._handle_batch([], max_rounds=max_rounds, self_prompt=True)
                    await asyncio.sleep(pace)
            except asyncio.CancelledError:
                return
            except Exception as e:
                print(f"[CORTICO] 世界大脑一轮失败: {e}")
                await asyncio.sleep(1.0)

    async def _handle_batch(self, batch: List[EventEnvelope], max_rounds: int = 8,
                            self_prompt: bool = False):
        cfg = _cfg()
        tools = self.assembly.tools()
        segments = await self.assembly.env_prompt_segments()
        history_n = int(getattr(cfg, "CORTICO_CONTEXT_EVENTS", 30))

        recent = self.assembly.store.since(0, limit=1000)[-history_n:]
        lines = [f"[{e.source}] {e.text}" for e in recent]
        body = "\n".join(lines) if lines else "（没有事件）"
        if batch:
            body += "\n\n刚刚到达：\n" + "\n".join(f"[{e.type}] {e.text}" for e in batch)

        messages: List[Dict[str, Any]] = []
        system_parts = [s["text"] for s in segments]
        if getattr(cfg, "CORTICO_MEMORY_INJECT", True):
            mb = self._memory_block()
            if mb:
                system_parts.append(mb)
        if system_parts:
            messages.append({"role": "system", "content": "\n\n".join(system_parts)})
        if self_prompt and not batch:
            messages.append({"role": "user", "content": self.SELF_PLAY_PROMPT})
        else:
            messages.append({"role": "user", "content": body})

        self.rounds += 1
        text = await run_tool_turn(messages, tools, max_rounds=max_rounds, role="cortico")
        text = (text or "").strip()
        self.last_text = text
        if not text:
            return
        await self._emit(text)
        self._writeback_memory(text)
        self._writeback_events(batch)
        tag = "自驱" if self_prompt else "事件"
        print(f"[CORTICO] 世界大脑一轮完成({tag},{self.rounds}): {text[:100]}")

    # ---- 记忆：注入宿主记忆 + 回写游戏内容 ----
    def _host_user(self) -> str:
        """宿主记忆来源 user_id：优先主人 QQ，否则配置里的注入来源。"""
        try:
            from identity import owner_qq
            o = owner_qq()
            if o:
                return str(o)
        except Exception:
            pass
        return str(getattr(_cfg(), "CORTICO_INJECT_MEMORY_FROM", "") or "")

    def _world_user(self) -> str:
        """游戏内容回写到的记忆 user_id（与世界隔离，避免污染聊天记忆）。"""
        if getattr(self, "_wu", None):
            return self._wu
        try:
            mounted = self.assembly.mounted()
            wid = mounted[0].id if mounted else "cortico"
        except Exception:
            wid = "cortico"
        self._wu = f"cortico:{wid}"
        return self._wu

    def _memory_block(self) -> str:
        """把宿主记忆（档案/人格/重要事项）拼成注入块，喂给世界模型。"""
        host = self._host_user()
        if not host:
            return ""
        parts: List[str] = []
        try:
            import long_term_memory as ltm
            prof = ltm.get_profile(host)
            if prof:
                parts.append("【宿主档案】" + json.dumps(prof, ensure_ascii=False)[:800])
        except Exception:
            pass
        try:
            import persona_memory as pm
            hint = pm.build_persona_hint(host)
            if hint:
                parts.append("【宿主人格】" + hint[:600])
        except Exception:
            pass
        try:
            import important_notes as inm
            notes = inm.get_user_notes(host)[-10:]
            if notes:
                joined = "; ".join(str(n) for n in notes)
                parts.append("【宿主重要事项】" + joined[:600])
        except Exception:
            pass
        return "\n".join(parts)

    def _writeback_memory(self, text: str) -> None:
        """把游戏内容写回记忆系统（让事件流落盘，可被宿主记忆检索）。"""
        cfg = _cfg()
        if not getattr(cfg, "CORTICO_MEMORY_WRITEBACK", True):
            return
        try:
            import long_term_memory as ltm
        except Exception:
            return
        wu = getattr(cfg, "CORTICO_MEMORY_WRITE_TO", "") or self._world_user()
        try:
            ltm.append_history(wu, "assistant", text, message_type="cortico")
        except Exception as e:
            print(f"[CORTICO] 记忆回写失败: {e}")
        # 里程碑事件额外记进重要事项
        if any(k in text for k in ("首杀", "升级", "获得", "击败", "死亡",
                                   "join", "创建角色", "里程碑", "完成任务", "胜利")):
            try:
                import important_notes as inm
                inm.add_note(wu, text[:200], category="cortico-dungeon")
            except Exception:
                pass

    def _writeback_events(self, batch: List[EventEnvelope]) -> None:
        """把原始事件流以第一人称写进记忆（即便模型本轮未叙述也不丢事件）。"""
        cfg = _cfg()
        if not getattr(cfg, "CORTICO_MEMORY_WRITEBACK", True):
            return
        if not batch:
            return
        try:
            import long_term_memory as ltm
        except Exception:
            return
        wu = getattr(cfg, "CORTICO_MEMORY_WRITE_TO", "") or self._world_user()
        lines = [f"我（游戏角色）刚刚经历了：[{e.type}] {e.text}" for e in batch]
        entry = "\n".join(lines)
        try:
            ltm.append_history(wu, "user", entry, message_type="cortico-event")
        except Exception as e:
            print(f"[CORTICO] 事件记忆写入失败: {e}")

    async def _emit(self, text: str):
        """把模型最后说的话播报出去：统一大脑事件 + 按需私聊主人。"""
        try:
            await brain_event(self.core, self.name, "notice", text)
        except Exception as e:
            print(f"[CORTICO] 事件播报失败: {e}")
        cfg = _cfg()
        if not getattr(cfg, "CORTICO_REPORT_TO_OWNER", False):
            return
        try:
            from message_bus import get_sender
            sender = get_sender()
            owner = str(getattr(cfg, "PROACTIVE_PRIVATE_USER_ID", "") or "").strip()
            if sender and owner:
                await sender.send_private(owner, text[:1500])
        except Exception as e:
            print(f"[CORTICO] 播报私聊失败: {e}")

    def status(self) -> Dict[str, Any]:
        return {
            **super().status(),
            "paused": self._paused,
            "queued": self._queue.qsize(),
            "rounds": self.rounds,
            "worlds": [s.id for s in self.assembly.mounted()],
            "tools": self.assembly.tool_names(),
        }


from brain_base import brain_event  # noqa: E402  （延迟导入避免循环）
