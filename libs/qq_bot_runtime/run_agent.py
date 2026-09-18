# -*- coding: utf-8 -*-
"""独立智能体核心入口（不依赖 QQ）。

以控制台平台启动智能体核心，终端直接聊天：
    python run_agent.py            # 控制台聊天模式
    python run_agent.py --mc       # 额外启动 Minecraft 自主游戏
"""
import asyncio
import sys

import config

from agent_core import get_core
from quiet import degrade


async def main():
    core = get_core()
    # 控制台平台（不依赖 QQ；余额监控等功能插件照常启动）
    core.register_builtin_plugins(platforms=["console"])
    await core.start()

    if "--mc" in sys.argv:
        from mc_watcher import mc_watcher
        print("启动 Minecraft 自主游戏...")
        if not mc_watcher.is_http_api_up():
            print("[!] FeiyuAPI 接口不在线（游戏未开或 mod 未加载）")
        elif not getattr(config, "ENABLE_MC_AGENT", False):
            print("[!] config.ENABLE_MC_AGENT 未开启")
        else:
            # 经核心大脑注册表统一启动（与 /mc自动 同一入口，行为等价）
            ok = bool(await core.brains.get("mc_mod").start())
            if ok:
                print("自主游戏已启动（核心大脑注册表: mc_mod）")
            else:
                print("[!] 自主游戏启动失败：FeiyuAPI 未在线或大脑异常")

    try:
        await asyncio.Future()
    except asyncio.CancelledError as e:
        degrade("libs/qq_bot_runtime/run_agent.py:39 main", e, "降级：await asyncio.Future()")
    finally:
        await core.shutdown()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[INFO] 正在停止智能体核心...")
