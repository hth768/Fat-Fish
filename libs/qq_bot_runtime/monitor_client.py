# -*- coding: utf-8 -*-
"""监控客户端：主进程经此把状态快照推送给 monitor_server。

使用：
    from monitor_client import push_status, server_healthy
    await push_status({"core": core.status(), "memory_sidecar": await mem_healthy()})

特性：
    - ENABLE_MONITOR 为 False 或 monitor 掉线时，push_status 静默 no-op，零副作用。
    - 所有调用均为 best-effort，绝不因监控异常影响主流程。
"""
import asyncio
import json
import urllib.error
import urllib.request

import config

ENABLE = getattr(config, "ENABLE_MONITOR", False)
HOST = getattr(config, "MONITOR_HOST", "127.0.0.1")
PORT = getattr(config, "MONITOR_PORT", 8770)
URL = f"http://{HOST}:{PORT}"


async def server_healthy() -> bool:
    try:
        with urllib.request.urlopen(URL + "/health", timeout=2):
            return True
    except (urllib.error.URLError, OSError):
        return False


async def push_status(payload: dict) -> bool:
    """把状态快照推送给 monitor；不可用时返回 False（不抛异常）。"""
    if not ENABLE:
        return False
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def _do():
        req = urllib.request.Request(
            URL + "/status", data=data,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))

    try:
        obj = await asyncio.to_thread(_do)
        return bool(obj.get("ok"))
    except (urllib.error.URLError, OSError, TimeoutError):
        return False
