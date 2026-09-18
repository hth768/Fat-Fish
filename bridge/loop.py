# -*- coding: utf-8 -*-
"""独立事件循环线程：HTTP 线程与 asyncio 核心解耦。"""
import asyncio
import threading

from quiet import degrade


class LoopThread:
    """后台跑一个常驻 asyncio 事件循环，供 AgentCore / ChatService 使用。"""

    def __init__(self):
        self.loop: asyncio.AbstractEventLoop = None
        self._thread: threading.Thread = None
        self._ready = threading.Event()

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._ready.clear()
        self._thread = threading.Thread(target=self._run, name="feiyu-app-loop", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=10)

    def _run(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self._ready.set()
        try:
            self.loop.run_forever()
        finally:
            try:
                self.loop.close()
            except Exception as e:
                degrade("loop.LoopThread._run", e, "关闭事件循环失败")

    def run_coro(self, coro, timeout: float = 300):
        """在循环线程里执行协程并等结果（HTTP 线程调用）。"""
        if not self.loop:
            raise RuntimeError("事件循环未启动")
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout=timeout)

    def schedule(self, coro):
        """投递协程不等待（fire-and-forget），返回 concurrent Future。"""
        if not self.loop:
            raise RuntimeError("事件循环未启动")
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def stop(self):
        if not self.loop:
            return
        try:
            self.loop.call_soon_threadsafe(self.loop.stop)
        except Exception as e:
            degrade("loop.LoopThread.stop", e, "通知事件循环停止失败")
        if self._thread:
            self._thread.join(timeout=5)
