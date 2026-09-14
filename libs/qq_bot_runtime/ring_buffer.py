# -*- coding: utf-8 -*-
"""线程安全的环形缓冲区，用于生产者/消费者视频帧流水线。

设计要点：
- 固定容量，满了覆盖最旧帧（实时感知优先最新）；
- 支持按时间戳查询最近 N 秒内的帧；
- 支持 numpy 数组和元数据（timestamp, source）。
"""
import time
from collections import deque
from threading import Lock
from typing import Deque, List, Optional, Tuple

import numpy as np


class RingBuffer:
    def __init__(self, capacity: int = 64):
        self.capacity = capacity
        self._buffer: Deque[Tuple[float, np.ndarray, dict]] = deque(maxlen=capacity)
        self._lock = Lock()

    def put(self, frame: np.ndarray, timestamp: float = None, meta: dict = None):
        """放入一帧。如果已满，自动丢弃最旧帧。"""
        if timestamp is None:
            timestamp = time.time()
        if meta is None:
            meta = {}
        with self._lock:
            self._buffer.append((timestamp, frame, meta))

    def get_latest(self, count: int = 1) -> List[Tuple[float, np.ndarray, dict]]:
        """获取最新的 count 帧。"""
        with self._lock:
            return list(self._buffer)[-count:]

    def get_recent(self, seconds: float) -> List[Tuple[float, np.ndarray, dict]]:
        """获取最近 seconds 秒内的帧。"""
        cutoff = time.time() - seconds
        with self._lock:
            return [item for item in self._buffer if item[0] >= cutoff]

    def get_snapshot(self) -> List[Tuple[float, np.ndarray, dict]]:
        """获取当前缓冲区全部内容的快照。"""
        with self._lock:
            return list(self._buffer)

    def clear(self):
        with self._lock:
            self._buffer.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._buffer)

    def is_empty(self) -> bool:
        with self._lock:
            return len(self._buffer) == 0

    def latest_timestamp(self) -> Optional[float]:
        with self._lock:
            if not self._buffer:
                return None
            return self._buffer[-1][0]
