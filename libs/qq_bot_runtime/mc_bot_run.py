# -*- coding: utf-8 -*-
"""Bot 大脑启动器：python mc_bot_run.py [--minutes N]

要求：mc_bot 桥已连上原版世界机器人（桥监听 8767）。
"""
import argparse
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mc_bot_brain import BotAgent, _bridge_get

# 轮转旧遥测日志，保证 dashboard/analyze 只看到本次 vanilla 会话
try:
    from mc_monitor import get_monitor
    _log_path = get_monitor().log_path()
    if os.path.isfile(_log_path) and os.path.getsize(_log_path) > 0:
        _rotated = _log_path.replace(".jsonl", f".{time.strftime('%Y%m%d_%H%M%S')}.jsonl")
        os.replace(_log_path, _rotated)
        print(f"[BOT-RUN] 旧遥测已轮转到 {_rotated}")
except Exception as e:
    print(f"[BOT-RUN] 遥测轮转跳过: {e}")


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=None, help="跑 N 分钟后自动退出")
    args = ap.parse_args()

    st = _bridge_get("/state", timeout=3.0)
    if not st or not st.get("connected"):
        print("[BOT-RUN] 机器人还没连上桥（connected=false）。")
        print("          请确认：1) 原版世界已对局域网开放 2) mc_bot 桥以正确端口运行")
        print("          我每 5 秒重试，机器人一上线就开始。")
    agent = BotAgent()
    deadline = time.time() + args.minutes * 60 if args.minutes else None
    try:
        if deadline:
            task = asyncio.create_task(agent.run())
            while time.time() < deadline and not task.done():
                await asyncio.sleep(5)
            agent.stop()
            try:
                await task
            except asyncio.CancelledError:
                pass
            print("[BOT-RUN] 到时退出")
        else:
            await agent.run()
    except KeyboardInterrupt:
        agent.stop()
        print("\n[BOT-RUN] 已停止")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
