# -*- coding: utf-8 -*-
"""事件库：账本式 JSONL 追加 + 全局递增游标。

对齐 Cortico 的事件库语义：cursor 落库时分配、跨 run 递增、重启后接着涨；
事件库是「账本」，不是队列——投递状态另由 host 的 pending 集合维护。
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
from typing import Dict, List, Optional

from .types import EventEnvelope, EventStoreReader

_HERE = os.path.dirname(os.path.abspath(__file__))


def _base_dir() -> str:
    return os.path.dirname(_HERE)


def _data_dir() -> str:
    import agent_ctx
    return agent_ctx.agent_storage_dir(_base_dir())


class EventStore(EventStoreReader):
    """事件库（JSONL，一行一条）。"""

    def __init__(self, path: Optional[str] = None):
        self.path = path or os.path.join(_data_dir(), "cortico_events.jsonl")
        self._lock = asyncio.Lock()
        self._tl = threading.Lock()
        self._cursor = 0
        self._loaded = False

    # ---- 游标 ----
    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if not os.path.exists(self.path):
            self._cursor = 0
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                lines = f.readlines()
        except OSError:
            self._cursor = 0
            return
        last = 0
        for line in reversed(lines[-500:]):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict) and isinstance(obj.get("cursor"), int):
                last = obj["cursor"]
                break
        self._cursor = last

    def latest(self) -> int:
        self._ensure_loaded()
        return self._cursor

    def count(self) -> int:
        if not os.path.exists(self.path):
            return 0
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                return sum(1 for line in f if line.strip())
        except OSError:
            return 0

    # ---- 写入 ----
    async def append(self, e: EventEnvelope) -> EventEnvelope:
        """落库并分配游标。"""
        async with self._lock:
            self._ensure_loaded()
            self._cursor += 1
            e.cursor = self._cursor
            self._write(e)
            return e

    def _write(self, e: EventEnvelope) -> None:
        d = os.path.dirname(self.path)
        if d:
            os.makedirs(d, exist_ok=True)
        try:
            with self._tl:
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(e.to_dict(), ensure_ascii=False) + "\n")
        except OSError as err:
            print(f"[CORTICO] 事件落库失败: {err}")

    # ---- 读取 ----
    def since(self, cursor: int = 0, limit: int = 200) -> List[EventEnvelope]:
        """取 cursor 之后的事件（默认全部，最多 limit 条）。"""
        if not os.path.exists(self.path):
            return []
        out: List[EventEnvelope] = []
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(obj, dict):
                        continue
                    c = obj.get("cursor")
                    if not isinstance(c, int) or c <= cursor:
                        continue
                    out.append(EventEnvelope.from_dict(obj))
                    if len(out) >= limit:
                        break
        except OSError as err:
            print(f"[CORTICO] 事件读取失败: {err}")
        return out

    def by_source(self, source: str, limit: int = 100) -> List[EventEnvelope]:
        return [e for e in self.since(0, limit=10000) if e.source == source][-limit:]

    def stats(self) -> Dict[str, int]:
        return {"count": self.count(), "latest": self.latest()}

    def clear(self) -> str:
        """清空事件库（控制台「数据」页签用）。"""
        n = self.count()
        try:
            if os.path.exists(self.path):
                os.remove(self.path)
        except OSError as err:
            raise RuntimeError(f"清空事件库失败: {err}")
        self._cursor = 0
        self._loaded = True
        return f"已清空 {n} 条事件"
