# -*- coding: utf-8 -*-
"""记忆服务客户端：把 vector_memory 等重负载模块的调用代理到 sidecar 进程。

使用方式（在 chat_service 等模块中）：
    from memory_client import vector_memory
    results = await vector_memory.hybrid_search(user_id, query, top_k=10)

行为：
    - ENABLE_MEMORY_SERVER 为 False 时：所有调用直接走主进程内 import（与改造前一致）。
    - ENABLE_MEMORY_SERVER 为 True 且 sidecar 在线：走本地 HTTP RPC（重负载在子进程）。
    - sidecar 掉线/启动慢：自动降级回主进程内调用，bot 永不停摆。

所有代理方法都是 async（即便底层是同步函数），方便在事件循环中无阻塞调用。
"""
import asyncio
import importlib
import inspect
import json
import urllib.error
import urllib.request

import config
from quiet import degrade

ENABLE = getattr(config, "ENABLE_MEMORY_SERVER", False)
HOST = getattr(config, "MEMORY_SERVER_HOST", "127.0.0.1")
PORT = getattr(config, "MEMORY_SERVER_PORT", 8766)
TIMEOUT = getattr(config, "MEMORY_SERVER_TIMEOUT", 120)
URL = f"http://{HOST}:{PORT}"


class _ServerUnavailable(Exception):
    pass


async def _http_call(module, func, args, kwargs):
    payload = json.dumps(
        {"module": module, "func": func, "args": args, "kwargs": kwargs}
    ).encode("utf-8")

    def _do():
        req = urllib.request.Request(
            URL + "/call",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))

    try:
        obj = await asyncio.to_thread(_do)
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        raise _ServerUnavailable() from e
    if not obj.get("ok"):
        raise RuntimeError(obj.get("error", "memory_server error"))
    return obj.get("result")


async def _local_call(module, func, args, kwargs):
    mod = importlib.import_module(module)
    f = getattr(mod, func)
    r = f(*(args or []), **(kwargs or {}))
    if inspect.iscoroutine(r):
        r = await r
    return r


class _ModuleProxy:
    """懒加载的模块代理：任意属性访问都返回一个 async 调用包装器。"""

    def __init__(self, module):
        self._module = module

    def __getattr__(self, func):
        async def _call(*args, **kwargs):
            if ENABLE:
                try:
                    return await _http_call(self._module, func, args, kwargs)
                except _ServerUnavailable as e:
                    degrade("libs/qq_bot_runtime/memory_client.py:78 _ModuleProxy.__getattr__._call", e, "降级：return await _http_call(self._module, func, args, ")
            return await _local_call(self._module, func, args, kwargs)

        return _call


# 暴露为与真实模块同名的代理，调用方几乎无需改动语义（仅需在调用处 await）。
vector_memory = _ModuleProxy("vector_memory")


async def server_healthy() -> bool:
    """探测 sidecar 是否在线（用于启动自检 / 日志提示）。"""
    try:
        with urllib.request.urlopen(URL + "/health", timeout=2):
            return True
    except (urllib.error.URLError, OSError):
        return False
