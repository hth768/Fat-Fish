# -*- coding: utf-8 -*-
"""feiyu 侧的 WorldHost 实现：World 与智能体之间的唯一通道。

职责（对齐 Cortico `WorldHost`）：
- push_event: 落库 + 分配游标 + 按 trigger 投递给宿主回调（唤醒大脑/进上下文）
- drain_pending_events: 取走尚未投递的外部事件（不重复投递）
- store / model_facts / report_usage / is_paused / log

投递回调由装配层注入（典型是 `brain.py` 的唤醒器）：
    async def deliver(envelope: EventEnvelope, trigger: TriggerMode) -> None
"""
from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Any, Callable, Deque, Dict, List, Optional

from .types import (DeferredEventSpec, EventEnvelope, EventStoreReader, LLMUsage,
                    ModelFacts, PushOptions, TriggerMode, WorldHost)


class FeiyuWorldHost(WorldHost):
    """World 可用的宿主接口实现。"""

    def __init__(self,
                 world_id: str,
                 store: EventStoreReader,
                 deliver: Optional[Callable[[EventEnvelope, TriggerMode], Any]] = None,
                 paused: Optional[Callable[[], bool]] = None,
                 logger: Optional[Callable[[str, str, Dict[str, Any]], None]] = None,
                 model: str = "",
                 context_window: Optional[int] = None,
                 accepts: Optional[List[str]] = None):
        self.world_id = world_id
        self._store = store
        self._deliver = deliver
        self._paused = paused
        self._logger = logger
        self._model_facts = ModelFacts(_model=model or self._current_model(),
                                       _accepts=accepts or ["image/png", "image/jpeg"],
                                       _context_window=context_window)
        # 尚未投递的外部事件（供 drain_pending_events 取走）
        self._pending: Deque[EventEnvelope] = deque()
        self._lock = asyncio.Lock()
        self._usage: Dict[str, int] = {"prompt": 0, "completion": 0, "calls": 0}
        self._stall_times: Deque[float] = deque()

    # ---- 常量/配置 ----
    @staticmethod
    def _current_model() -> str:
        try:
            import config
            return str(getattr(config, "CHAT_MODEL", "") or getattr(config, "MODEL_NAME", "") or "")
        except Exception:
            return ""

    # ---- 事件 ----
    async def push_event(self, e: EventEnvelope, opts: Optional[PushOptions] = None) -> EventEnvelope:
        opts = opts or PushOptions()
        if not e.source:
            e.source = self.world_id
        if not e.origin:
            e.origin = "external"
        saved = await self._store.append(e)

        if saved.origin == "external" and saved.context_delivery == "deliver":
            async with self._lock:
                self._pending.append(saved)

        if opts.deliver and self._deliver is not None:
            trigger: TriggerMode = opts.trigger or (
                "debounce" if saved.origin == "external" else "flush")
            try:
                # 投递回调可以是同步的（返回 None）也可以是协程
                res = self._deliver(saved, trigger)
                if hasattr(res, "__await__"):
                    await res
            except Exception as err:
                self.log("warn", f"事件投递失败: {err}", type=saved.type)
        return saved

    def push_deferred(self, spec: DeferredEventSpec,
                      trigger: Optional[TriggerMode] = None) -> None:
        """延迟事件：投递时才渲染。这里同步渲染并入 pending（render 不做长时间采样）。

        渲染失败/返回 None 时不落库、不投递（与 Cortico 一致）。
        """
        if spec.render is None:
            return
        try:
            out = spec.render()
        except Exception as err:
            self.log("warn", f"延迟事件渲染失败: {err}", type=spec.type)
            return
        if out is None:
            return
        text = out if isinstance(out, str) else str(getattr(out, "text", out))
        e = EventEnvelope(type=spec.type, ts=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                          source=spec.source or self.world_id, text=text,
                          origin=spec.origin, sender_key=spec.sender_key, meta=dict(spec.meta or {}))
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(self.push_event(e, PushOptions(trigger=trigger)))
        except RuntimeError:
            pass

    async def drain_pending_events(self,
                                   filter_fn: Callable[[EventEnvelope], bool]) -> List[EventEnvelope]:
        """取走尚未投递的外部即时事件，保持原序且不等待。"""
        taken: List[EventEnvelope] = []
        rest: Deque[EventEnvelope] = deque()
        async with self._lock:
            for e in self._pending:
                try:
                    keep = bool(filter_fn(e)) if filter_fn else True
                except Exception:
                    keep = False
                (taken if keep else rest).append(e)
            self._pending = rest
        return taken

    def pending_count(self) -> int:
        return len(self._pending)

    # ---- 能力查询 ----
    @property
    def store(self) -> EventStoreReader:
        return self._store

    @property
    def model_facts(self) -> ModelFacts:
        return self._model_facts

    def report_usage(self, usage: LLMUsage, **kwargs: Any) -> None:
        self._usage["prompt"] += int(getattr(usage, "prompt_tokens", 0) or 0)
        self._usage["completion"] += int(getattr(usage, "completion_tokens", 0) or 0)
        self._usage["calls"] += 1

    def note_stall(self) -> None:
        """记录一次模型调用失败/流中断（供 llm_stalls 查询）。"""
        now = time.time()
        self._stall_times.append(now)
        while self._stall_times and now - self._stall_times[0] > 3600_000:
            self._stall_times.popleft()

    async def llm_stalls(self, within_ms: int) -> int:
        cutoff = time.time() - max(0, within_ms) / 1000.0
        return sum(1 for t in self._stall_times if t >= cutoff)

    def is_paused(self) -> bool:
        return bool(self._paused and self._paused())

    def usage_snapshot(self) -> Dict[str, int]:
        return dict(self._usage)

    def log(self, level: str, message: str, **data: Any) -> None:
        if self._logger is not None:
            try:
                self._logger(level, message, dict(data))
                return
            except Exception:
                pass
        print(f"[WORLD:{self.world_id}][{level}] {message}" + (f" {data}" if data else ""))
