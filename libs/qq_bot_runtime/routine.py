# -*- coding: utf-8 -*-
"""AI 作息（睡眠 / 午休 / 活跃）。

对齐新版 cortico-world-qq-better 的 routine：
按真实时间切换三段——睡眠段暂停「主动冒泡」（被动回复照常，即"睡着了被戳醒仍会回"）、
午休段放缓节奏、活跃段正常；时段切换由 LLM 现场生成随性播报（非固定套话），
生成失败才回退到 config 里的兜底文案。

段状态与「上次切换到的段」落盘 data/routine_state.json：
首次上线（无存档）不补播报，从下一次真实切换开始播。
"""
import asyncio
import json
import os
import time
import datetime as _dt

import config
import agent_ctx


def _base_dir():
    return os.path.dirname(os.path.abspath(__file__))


def _state_file():
    return os.path.join(agent_ctx.agent_storage_dir(_base_dir()), "routine_state.json")


def _parse_hhmm(s: str):
    try:
        h, m = str(s).strip().split(":")
        return int(h), int(m)
    except (ValueError, AttributeError):
        return None


def _minutes(now=None) -> int:
    now = now or _dt.datetime.now()
    return now.hour * 60 + now.minute


def _in_range(start: str, end: str, minute_of_day: int) -> bool:
    """判断分钟是否落在 [start, end) 内，支持跨午夜（start 数值 > end 时）。"""
    a = _parse_hhmm(start)
    b = _parse_hhmm(end)
    if not a or not b:
        return False
    sa, sb = a[0] * 60 + a[1], b[0] * 60 + b[1]
    if sa == sb:
        return False
    if sa < sb:
        return sa <= minute_of_day < sb
    return minute_of_day >= sa or minute_of_day < sb


def phase(now=None) -> str:
    """当前作息段：sleep / lazy / active。"""
    if not getattr(config, "ROUTINE_ENABLED", False):
        return "active"
    m = _minutes(now)
    if _in_range(getattr(config, "ROUTINE_SLEEP_START", "23:00"),
                 getattr(config, "ROUTINE_SLEEP_END", "07:30"), m):
        return "sleep"
    if _in_range(getattr(config, "ROUTINE_LAZY_START", "12:00"),
                 getattr(config, "ROUTINE_LAZY_END", "14:00"), m):
        return "lazy"
    return "active"


def is_sleeping(now=None) -> bool:
    return phase(now) == "sleep"


def should_pause_proactive(now=None) -> bool:
    """睡眠段是否暂停主动冒泡（被动回复照常）。"""
    return is_sleeping(now)


def build_hint(now=None) -> str:
    """注入上下文的作息状态提示（让玩家语气/节奏跟着作息走）。"""
    if not getattr(config, "ROUTINE_ENABLED", False):
        return ""
    p = phase(now)
    if p == "sleep":
        return ("【作息】现在是你的睡眠时段：你困了、想睡，说话可以懒一点、短一点，"
                "别主动开新话题；但对方找你，你还是会回（被戳醒的状态）。")
    if p == "lazy":
        return "【作息】现在是午休时段：你有点犯困、在摸鱼，节奏放缓，话少一点、更随意一点。"
    return ""


def _load_last() -> str:
    try:
        if os.path.exists(_state_file()):
            with open(_state_file(), "r", encoding="utf-8") as f:
                return str(json.load(f).get("phase", ""))
    except Exception:
        pass
    return ""


def _save_last(p: str):
    try:
        path = _state_file()
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"phase": p, "ts": time.time()}, f, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception as e:
        print(f"[WARN] 作息状态落盘失败: {e}")


_FALLBACK = {
    "sleep": "时间不早啦，我先去睡一会儿，你们也别熬太狠~有事留言，我醒了回。",
    "wake": "早呀，我醒啦，今天也要好好摸鱼。",
    "lazy": "中午啦，我去扒口饭眯一眯，有事儿留言~",
}


async def _gen_greeting(kind: str) -> str:
    """用 LLM 现场生成一句播报（每次说法不固定）；失败返回空串。"""
    topic = {"sleep": "你要去睡了，跟大家说一声",
             "wake": "你刚睡醒，跟大家打个招呼",
             "lazy": "你要午休/摸鱼一会儿，跟大家说一声"}.get(kind, "跟大家说句话")
    prompt = (f"{topic}。要求：口语化、像真人随手发的一句，1-2 句，"
              "不要客套、不要「您好」、不要列点，直接输出这句话本身，不要任何前缀。")
    try:
        from ai_provider import get_llm
        out = await get_llm().chat([{"role": "user", "content": prompt}],
                                   capability="chat", role="routine")
        if isinstance(out, dict):
            out = out.get("content", "") or out.get("text", "") or ""
        return str(out).strip()
    except Exception as e:
        print(f"[WARN] 作息播报生成失败: {e}")
        return ""


async def _broadcast(text: str):
    """把播报发到主动说话的目标（私聊/群）。"""
    if not text:
        return
    try:
        from message_bus import get_sender
        sender = get_sender()
    except Exception:
        sender = None
    if sender is None:
        return
    priv = getattr(config, "PROACTIVE_PRIVATE_USER_ID", "")
    grp = getattr(config, "PROACTIVE_GROUP_ID", "")
    try:
        if priv:
            await sender.send_private(priv, text)
        if grp:
            await sender.send_group(grp, text)
    except Exception as e:
        print(f"[WARN] 作息播报发送失败: {e}")


async def check_and_announce():
    """检测作息段切换并播报（首次上线无存档不补播）。"""
    if not getattr(config, "ROUTINE_ENABLED", False):
        return
    cur = phase()
    last = _load_last()
    if not last:
        _save_last(cur)
        return
    if last == cur:
        return
    _save_last(cur)
    if cur == "sleep":
        kind, fallback = "sleep", getattr(config, "ROUTINE_GREET_SLEEP", "") or _FALLBACK["sleep"]
    elif last == "sleep":
        kind, fallback = "wake", getattr(config, "ROUTINE_GREET_WAKE", "") or _FALLBACK["wake"]
    elif cur == "lazy":
        kind, fallback = "lazy", getattr(config, "ROUTINE_GREET_LAZY", "") or _FALLBACK["lazy"]
    else:
        return
    text = await _gen_greeting(kind)
    if not text and not fallback:
        return
    await _broadcast(text or fallback)
    print(f"[ROUTINE] 作息切换 {last}→{cur}，已播报")


_check_task = None


async def _check_loop():
    """每 60 秒检查一次作息段切换。"""
    while True:
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            raise
        try:
            await check_and_announce()
        except Exception as e:
            print(f"[WARN] 作息检查异常: {e}")


def start_check() -> bool:
    """启动作息播报检查（幂等）。"""
    global _check_task
    if _check_task is not None and not _check_task.done():
        return True
    if not getattr(config, "ROUTINE_ENABLED", False):
        return False
    try:
        _check_task = asyncio.ensure_future(_check_loop())
        print("[ROUTINE] 作息播报检查已启动")
        return True
    except RuntimeError as e:
        print(f"[WARN] 作息检查启动失败（无事件循环）: {e}")
        return False


def stop_check():
    global _check_task
    if _check_task is not None:
        _check_task.cancel()
        _check_task = None


def status_text(now=None) -> str:
    """作息状态（命令展示用）。"""
    if not getattr(config, "ROUTINE_ENABLED", False):
        return "作息功能未开启（config.ROUTINE_ENABLED=False）"
    p = phase(now)
    name = {"sleep": "睡眠段（不主动冒泡，被戳仍回）", "lazy": "午休段（节奏放缓）", "active": "活跃段"}[p]
    return (f"【AI 作息】当前：{name}\n"
            f"睡眠 {getattr(config, 'ROUTINE_SLEEP_START', '23:00')}-{getattr(config, 'ROUTINE_SLEEP_END', '07:30')}｜"
            f"午休 {getattr(config, 'ROUTINE_LAZY_START', '12:00')}-{getattr(config, 'ROUTINE_LAZY_END', '14:00')}")
