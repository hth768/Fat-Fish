# -*- coding: utf-8 -*-
"""QQ 发言护栏：群聊发言频率限制 + 话题自动结束 / 防死循环。

对齐新版 cortico-world-qq-better：
- `groupSpeak`：每个群在滑动窗口内可发消息总量的硬上限（含主动与回复），防刷屏。
- `antiLoop`：单会话 bot 连续发言超上限、或对方长时间静默则自动收尾，防话题死循环。

所有发送出口（QQ 回复目标 / QQ 适配器主动发送）都先过这里，保证一致节流。
"""
import time
from collections import deque

import config

# 会话 -> bot 连续发言计数 / 最后一条用户消息时间
_BOT_TURNS = {}          # (channel_type, channel_id) -> int
_LAST_USER_TS = {}       # (channel_type, channel_id) -> float
# 群 -> 已发送时间戳滑动窗口
_GROUP_WINDOW = {}       # channel_id -> deque([ts, ...])


def _win_seconds() -> int:
    return max(1, int(getattr(config, "QQ_GROUPSPEAK_WINDOW_SEC", 60)))


def _max_per_window() -> int:
    return max(1, int(getattr(config, "QQ_GROUPSPEAK_MAX_PER_WINDOW", 12)))


def note_incoming(channel_type: str, channel_id):
    """收到对方消息：清零 bot 连续发言计数（话题重新有人接了）。"""
    key = (str(channel_type), str(channel_id))
    _BOT_TURNS[key] = 0
    _LAST_USER_TS[key] = time.time()


def note_outgoing(channel_type: str, channel_id):
    """bot 发过一条：连续发言 +1，并记入群窗口。"""
    key = (str(channel_type), str(channel_id))
    _BOT_TURNS[key] = _BOT_TURNS.get(key, 0) + 1
    if str(channel_type) == "group":
        q = _GROUP_WINDOW.get(str(channel_id))
        if q is None:
            q = _GROUP_WINDOW[str(channel_id)] = deque()
        q.append(time.time())


def allow_outgoing(channel_type: str, channel_id) -> tuple:
    """是否允许本次发送。返回 (是否允许, 拦截原因)。"""
    ct, cid = str(channel_type), str(channel_id)
    key = (ct, cid)

    # 1) 群聊发言频率限制
    if ct == "group" and getattr(config, "QQ_GROUPSPEAK_ENABLED", True):
        window = _win_seconds()
        limit = _max_per_window()
        q = _GROUP_WINDOW.get(cid)
        if q is not None:
            now = time.time()
            while q and now - q[0] > window:
                q.popleft()
            if len(q) >= limit:
                return False, f"发言频率已达上限（{limit} 条/{window} 秒），先缓缓"

    # 2) 话题自动结束 / 防死循环
    if getattr(config, "QQ_ANTILOOP_ENABLED", True):
        max_turns = max(1, int(getattr(config, "QQ_ANTILOOP_MAX_BOT_TURNS", 5)))
        idle_sec = max(0, int(getattr(config, "QQ_ANTILOOP_IDLE_TO_END_SEC", 300)))
        turns = _BOT_TURNS.get(key, 0)
        if turns >= max_turns:
            # 收尾：本次拦截；计数保持，直到对方发来消息（note_incoming）才清零重新开话题
            return False, f"话题自动收尾（我已连续说了 {turns} 条，先打住等你接话）"
        if idle_sec > 0 and turns > 0:
            last_user = _LAST_USER_TS.get(key, 0.0)
            if last_user and time.time() - last_user > idle_sec:
                # 对方久未接话：同样收尾，等对方开口
                return False, "话题自动收尾（你太久没接话啦，先到这儿）"
    return True, ""


def reset(channel_type: str = "", channel_id=""):
    """清空护栏状态（测试/命令用）。"""
    if not channel_type:
        _BOT_TURNS.clear()
        _LAST_USER_TS.clear()
        _GROUP_WINDOW.clear()
        return
    key = (str(channel_type), str(channel_id))
    _BOT_TURNS.pop(key, None)
    _LAST_USER_TS.pop(key, None)
    _GROUP_WINDOW.pop(str(channel_id), None)


def stats(channel_type: str, channel_id) -> dict:
    """当前护栏状态（调试/状态展示用）。"""
    key = (str(channel_type), str(channel_id))
    q = _GROUP_WINDOW.get(str(channel_id)) or []
    return {
        "bot_turns": _BOT_TURNS.get(key, 0),
        "last_user_ago": round(time.time() - _LAST_USER_TS.get(key, 0.0), 1) if _LAST_USER_TS.get(key) else None,
        "group_window_used": len(q),
        "group_window_limit": _max_per_window(),
    }
