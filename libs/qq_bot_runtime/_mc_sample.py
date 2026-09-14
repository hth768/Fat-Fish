# -*- coding: utf-8 -*-
"""单次紧凑采样：游戏状态 + 智能体遥测摘要。监听会话用。"""
import json
import sys
import time
from collections import Counter

import httpx

API = "http://127.0.0.1:8765/api/state"
LOG = "F:/qq_bot/data/mc_behavior.jsonl"


def sample():
    print(f"--- 采样 {time.strftime('%H:%M:%S')} ---")
    try:
        d = httpx.get(API, timeout=3).json()
        p = d.get("player") or {}
        w = d.get("world") or {}
        if p.get("name"):
            inv = (d.get("inventory") or {}).get("items") or []
            nav = d.get("nav") or {}
            look = d.get("look") or {}
            print(f"位置 {round(p.get('x',0),1)},{round(p.get('y',0),1)},{round(p.get('z',0),1)} | "
                  f"hp {p.get('health')} 饥饿 {p.get('hunger')} | 第{w.get('day')}天 {w.get('time_of_day')}")
            print(f"背包: { {i.get('item','').split(':')[-1]: i.get('count') for i in inv[:12]} or '空'}")
            print(f"地形: 被挡{nav.get('blocked_directions')} 阻挡{nav.get('front_blocked')} "
                  f"悬崖{nav.get('front_cliff')} | 正对 {look.get('looking_at_block')}")
        else:
            print("游戏离线/未进世界")
    except Exception as e:
        print(f"游戏离线: {type(e).__name__}")
    try:
        events = [json.loads(l) for l in open(LOG, encoding="utf-8") if l.strip()]
        starts = [i for i, e in enumerate(events) if e["kind"] == "round_start" and e.get("round") == 0]
        seg = events[starts[-1]:] if starts else events
        rs = [e for e in seg if e["kind"] == "round_start"]
        sc = [e for e in seg if e["kind"] == "tool_call"]
        pos = [tuple(e["pos"]) for e in rs if e.get("pos")]
        print(f"会话: 轮次 {len(rs)} | 工具 {len(sc)} | brain_error "
              f"{sum(1 for e in seg if e['kind']=='brain_error')} | 近20轮唯一位置 {len(set(pos[-20:]))}")
        goals = [e.get("current") for e in seg if e["kind"] == "goal"]
        if goals:
            print(f"目标: {goals[-1][:60]}")
        recent = [(e["tool"], "ok" if e["ok"] else "FAIL", e["result"][:32]) for e in sc[-5:]]
        print(f"最近: {recent}")
    except Exception as e:
        print(f"遥测读取失败: {e}")


if __name__ == "__main__":
    sample()
