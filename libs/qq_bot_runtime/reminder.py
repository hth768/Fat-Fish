# -*- coding: utf-8 -*-
"""闹钟 / 记事本（到点提醒）。

对齐新版 cortico-world-qq-better 的 reminder.ts：
让智能体把「需要定时做的事 / 约定」记下来，到点自动在该会话提醒。
- 时间写法：绝对 ISO（2026-09-24T15:00）、HH:MM（今天，已过顺延明天）、
  今天/明天 HH:MM、相对（in 30m / 30分钟后 / 2小时后 / 3天后）。
- 落盘 data/reminders.json，进程重启后仍会补发离线期间到期的提醒。
- 记录 / 查询 / 取消始终可用；`REMINDER_ENABLED` 只控制「到点自动发」。
"""
import asyncio
import json
import os
import random
import re
import time
import datetime as _dt

import config
import agent_ctx

_REL_RE = re.compile(
    r'^(?:in\s+)?(\d+)\s*(minutes|min|mins|m|hours|hour|hr|h|days|day|d|天|日|分钟|小时)\s*(?:后|以后|之后)?$',
    re.I)
_CN_HOUR_RE = re.compile(r'^(?:(今天|明天|后天)\s*)?(\d{1,2})\s*[点時时](\d{1,2})?分?$')


def _base_dir():
    return os.path.dirname(os.path.abspath(__file__))


def _store_file():
    return os.path.join(agent_ctx.agent_storage_dir(_base_dir()), "reminders.json")


def gen_id() -> str:
    return "rm" + str(int(time.time() * 1000))[-8:] + "".join(random.choice("0123456789abcdefghijklmnopqrstuvwxyz") for _ in range(4))


def parse_when(text: str, now=None) -> float:
    """把时间描述解析成 epoch 秒（本地时区）。解析失败返回 0。"""
    s = (text or "").strip()
    if not s:
        return 0.0
    now = now or _dt.datetime.now()

    # 相对：in 30m / 30分钟后 / 2小时 / 3天
    m = _REL_RE.match(s)
    if m:
        n = int(m.group(1))
        unit = m.group(2).lower()
        if unit.startswith('m') or unit.startswith('分'):
            delta = n * 60
        elif unit.startswith('h') or unit.startswith('小时') or unit.startswith('时'):
            delta = n * 3600
        else:
            delta = n * 86400
        return time.mktime((now + _dt.timedelta(seconds=delta)).timetuple())

    # 中文：明天9点 / 今天21点30 / 3点
    m = _CN_HOUR_RE.match(s)
    if m:
        day_word = m.group(1) or "今天"
        hh = int(m.group(2))
        mm = int(m.group(3) or 0)
        if hh > 23 or mm > 59:
            return 0.0
        base = now + _dt.timedelta(days={"今天": 0, "明天": 1, "后天": 2}.get(day_word, 0))
        target = base.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if day_word == "今天" and target <= now:
            target += _dt.timedelta(days=1)
        return time.mktime(target.timetuple())

    # 绝对 ISO / 日期
    if re.search(r'T|\d{4}-\d{2}-\d{2}|Z$|[+-]\d{2}:?\d{2}$', s, re.I):
        try:
            return _dt.datetime.fromisoformat(s.replace('Z', '+00:00')).timestamp()
        except ValueError:
            pass

    # 今天 / 明天 HH:MM[:SS]
    low = s.lower()
    base = now
    tmr = re.match(r'^(?:tomorrow|明天)\s*(\d{1,2}):(\d{1,2})(?::(\d{1,2}))?$', low)
    tm = re.match(r'^(?:today|今天)?\s*(\d{1,2}):(\d{1,2})(?::(\d{1,2}))?$', low)
    mm = tmr or tm
    if mm:
        if tmr:
            base = now + _dt.timedelta(days=1)
        hh, mi = int(mm.group(1)), int(mm.group(2))
        ss = int(mm.group(3) or 0)
        if hh > 23 or mi > 59 or ss > 59:
            return 0.0
        target = base.replace(hour=hh, minute=mi, second=ss, microsecond=0)
        if not tmr and target <= now:
            target += _dt.timedelta(days=1)
        return time.mktime(target.timetuple())

    return 0.0


def format_when(ts: float, now=None) -> str:
    """把 epoch 秒格式化成「今天/明天/X月X日 HH:MM」。"""
    now = now or _dt.datetime.now()
    try:
        d = _dt.datetime.fromtimestamp(float(ts))
    except (TypeError, ValueError, OSError):
        return "时间未知"
    same_day = lambda a, b: (a.year, a.month, a.day) == (b.year, b.month, b.day)
    if same_day(d, now):
        date_part = "今天"
    elif same_day(d, now + _dt.timedelta(days=1)):
        date_part = "明天"
    else:
        date_part = f"{d.month}月{d.day}日"
    return f"{date_part} {d.hour:02d}:{d.minute:02d}"


def _load() -> list:
    path = _store_file()
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (json.JSONDecodeError, OSError):
        return []
    return [r for r in raw if isinstance(r, dict) and r.get("id")] if isinstance(raw, list) else []


def _save(items: list):
    try:
        path = _store_file()
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except OSError as e:
        print(f"[WARN] 提醒存档写入失败: {e}")


def add(text: str, when_ts: float, channel_type: str = "", channel_id: str = "") -> dict:
    """记一条提醒，返回该条记录。"""
    item = {
        "id": gen_id(),
        "text": str(text)[:300],
        "when": float(when_ts),
        "channel_type": str(channel_type or ""),
        "channel_id": str(channel_id or ""),
        "createdAt": time.time(),
        "done": False,
    }
    items = _load()
    items.append(item)
    _save(items)
    return item


def list_pending() -> list:
    items = [r for r in _load() if not r.get("done")]
    items.sort(key=lambda r: r.get("when", 0))
    return items


def get(rid: str) -> dict:
    for r in _load():
        if r.get("id") == rid:
            return r
    return {}


def cancel(rid: str) -> bool:
    items = _load()
    rest = [r for r in items if r.get("id") != rid]
    if len(rest) == len(items):
        return False
    _save(rest)
    return True


def mark_done(rid: str):
    items = _load()
    changed = False
    for r in items:
        if r.get("id") == rid and not r.get("done"):
            r["done"] = True
            r["doneAt"] = time.time()
            changed = True
    if changed:
        _save(items)


def list_text() -> str:
    items = list_pending()
    if not items:
        return "目前没有待提醒的事~"
    lines = [f"【待提醒】共 {len(items)} 条"]
    for r in items[:30]:
        lines.append(f"- {r['id']}｜{format_when(r['when'])}｜{r['text']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 到点扫描（后台任务）
# ---------------------------------------------------------------------------
_scan_task = None


async def _scan_loop():
    """每 REMINDER_SCAN_SEC 秒扫一次，到点未完成的提醒自动发到原会话。"""
    while True:
        try:
            interval = max(5, int(getattr(config, "REMINDER_SCAN_SEC", 30)))
        except Exception:
            interval = 30
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            raise
        try:
            items = [r for r in _load() if not r.get("done") and float(r.get("when", 0)) <= time.time()]
        except Exception as e:
            print(f"[WARN] 提醒扫描读取失败: {e}")
            continue
        if not items:
            continue
        # REMINDER_ENABLED 只控制「到点自动发」：关掉时保留记录，等开启后再补发
        if not getattr(config, "REMINDER_ENABLED", True):
            continue
        sender = None
        try:
            from message_bus import get_sender
            sender = get_sender()
        except Exception:
            sender = None
        for r in items:
            msg = f"⏰ 到点提醒：{r.get('text', '')}"
            ok = False
            if sender is not None:
                try:
                    if r.get("channel_type") == "group":
                        ok = await sender.send_group(r.get("channel_id"), msg)
                    else:
                        ok = await sender.send_private(r.get("channel_id"), msg)
                except Exception as e:
                    print(f"[WARN] 提醒发送失败: {e}")
            if ok:
                mark_done(r["id"])
                print(f"[REMINDER] 已提醒（{r['id']}）: {r.get('text', '')[:30]}")
            elif not getattr(config, "REMINDER_RETRY_ON_FAIL", True):
                # 发不出去（多半是不在线）：标记完成，避免每次重启都补发一堆旧提醒
                mark_done(r["id"])


def start_scan() -> bool:
    """启动到点提醒扫描（幂等）。"""
    global _scan_task
    if _scan_task is not None and not _scan_task.done():
        return True
    try:
        _scan_task = asyncio.ensure_future(_scan_loop())
        print("[REMINDER] 到点提醒扫描已启动")
        return True
    except RuntimeError as e:
        print(f"[WARN] 到点提醒扫描启动失败（无事件循环）: {e}")
        return False


def stop_scan():
    global _scan_task
    if _scan_task is not None:
        _scan_task.cancel()
        _scan_task = None
