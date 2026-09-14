# -*- coding: utf-8 -*-
"""统一时间上下文：让智能体在发消息时感知当前时间。

- 模块加载（=进程/智能体启动）时记录启动时刻 _BOOT。
- now_str() 返回人类可读的当前时间（含星期、上下午）。
- uptime_str() 返回自启动以来的运行时长。
两条 LLM 调用路径（ai_provider.chat / realtime_voice._llm_stream_raw）都会
在发消息前把 now_str() 注入 system 消息，使模型在需要时自然提及时间。
"""

import datetime

# 智能体启动时刻（模块首次导入即记录）
_BOOT = datetime.datetime.now()


def _fmt(dt: datetime.datetime) -> str:
    """把 datetime 格式化成中文可读串，带星期与上下午。"""
    week = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][dt.weekday()]
    ampm = "上午" if dt.hour < 12 else ("下午" if dt.hour < 18 else "晚上")
    return (f"{dt.year}年{dt.month}月{dt.day}日 {week} {ampm}"
            f"{dt.hour:02d}:{dt.minute:02d}:{dt.second:02d}")


def now_str() -> str:
    """当前时间的可读字符串（每次调用取实时时间）。"""
    return _fmt(datetime.datetime.now())


def uptime_str() -> str:
    """自智能体启动至今的运行时长。"""
    delta = datetime.datetime.now() - _BOOT
    secs = int(delta.total_seconds())
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}小时{m}分{s}秒"
    if m:
        return f"{m}分{s}秒"
    return f"{s}秒"


def time_system_message() -> dict:
    """构造一条注入到 messages 的 system 消息，告知模型当前时间与运行时长。"""
    return {
        "role": "system",
        "content": (
            f"[当前时间] 现在是 {now_str()}，你已经连续运行了 {uptime_str()}。"
            f"如果用户问起时间、日期、星期或“现在几点”，请基于上面的信息自然回答；"
            f"不需要刻意提及时间，除非对话相关。"
        ),
    }


def inject_time(messages: list) -> list:
    """在 messages 头部插入时间 system 消息（不修改原列表，返回新列表）。"""
    return [time_system_message()] + list(messages)
