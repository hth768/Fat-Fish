# -*- coding: utf-8 -*-
"""Cortico 世界大脑：世界事件 -> 唤醒 -> 模型 -> 工具 -> 输出。

这是 Cortico 的运行模型在 feiyu 里的落点：World 推事件，宿主按 trigger 合批唤醒，
模型看到「环境提示词 + 最近事件」，用 World 的工具回应，说出来的话经统一通道播报。

与聊天主线（`chat_service`）解耦：不占用聊天会话，不阻塞消息处理。
想让聊天也「知道」世界，用 `env_prompt_segments()` 注入 system 前缀即可。
"""
from __future__ import annotations

import asyncio
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
        self.started = True
        print("[CORTICO] 世界大脑已启动")

    async def stop(self):
        if self._loop_task:
            self._loop_task.cancel()
            self._loop_task = None
        self.started = False
        print("[CORTICO] 世界大脑已停止")

    def pause(self, on: bool = True):
        """暂停/恢复唤醒（对应控制台「运行/暂停」）。World 的自发行为查 host.isPaused()。"""
        self._paused = bool(on)

    @property
    def is_paused(self) -> bool:
        return self._paused

    # ---- 主循环 ----
    async def _run(self):
        cfg = _cfg()
        quiet_gap = float(getattr(cfg, "CORTICO_QUIET_GAP_MS", 800)) / 1000.0
        max_batch = int(getattr(cfg, "CORTICO_MAX_BATCH", 20))
        max_age = float(getattr(cfg, "CORTICO_MAX_BATCH_AGE_MS", 6000)) / 1000.0
        max_rounds = int(getattr(cfg, "CORTICO_MAX_TOOL_ROUNDS", 8))

        while True:
            try:
                batch: List[EventEnvelope] = []
                first = await self._queue.get()
                batch.append(first[0])
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

                await self._handle_batch(batch, max_rounds=max_rounds)
            except asyncio.CancelledError:
                return
            except Exception as e:
                print(f"[CORTICO] 世界大脑一轮失败: {e}")
                await asyncio.sleep(1.0)

    async def _handle_batch(self, batch: List[EventEnvelope], max_rounds: int = 8):
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
        if system_parts:
            messages.append({"role": "system", "content": "\n\n".join(system_parts)})
        messages.append({"role": "user", "content": body})

        self.rounds += 1
        text = await run_tool_turn(messages, tools, max_rounds=max_rounds, role="cortico")
        text = (text or "").strip()
        self.last_text = text
        if not text:
            return
        await self._emit(text)

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
