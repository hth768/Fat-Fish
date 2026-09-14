# -*- coding: utf-8 -*-
"""临时会话脚本：启动智能体并监控指定时长，结束后落盘遥测并打印统计。

用法: python _run_monitored.py [秒数=360]
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mc_agent import get_agent
from mc_monitor import get_monitor

monitor = get_monitor()
# 轮转旧遥测日志，保证本次会话数据干净
log_path = monitor.log_path()
if os.path.isfile(log_path) and os.path.getsize(log_path) > 0:
    rotated = log_path.replace(".jsonl", f".{time.strftime('%Y%m%d_%H%M%S')}.jsonl")
    os.replace(log_path, rotated)
    print(f"[RUN] 旧遥测已轮转到 {rotated}")

agent = get_agent()
if not agent.start():
    print("[RUN] 智能体启动失败")
    sys.exit(1)
print("[RUN] 智能体已启动，开始监控")

duration = float(sys.argv[1]) if len(sys.argv) > 1 else 360.0
deadline = time.time() + duration
try:
    while time.time() < deadline and agent.is_running():
        time.sleep(5)
finally:
    agent.stop()
    monitor.flush()
    print("[RUN] 会话结束，统计:")
    for k, v in agent.get_stats().items():
        print(f"  {k}: {v}")
    print("[RUN] 最近动作:")
    for a in agent.get_recent_actions(12):
        print(f"  - {a}")
