# -*- coding: utf-8 -*-
"""实时时间工具：让 AI 知道当前真实时间。

提供当前日期、时间、星期、季节等格式化信息，供 AI 决策和说话时参考。
"""
import datetime
from typing import Dict


# 星期中文
_WEEKDAYS = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]

# 季节判断（北半球）
def _season(month: int) -> str:
    if month in (3, 4, 5):
        return "春季"
    if month in (6, 7, 8):
        return "夏季"
    if month in (9, 10, 11):
        return "秋季"
    return "冬季"


def now() -> datetime.datetime:
    """获取当前本地时间。"""
    return datetime.datetime.now()


def get_time_dict() -> Dict:
    """获取当前时间的结构化信息。"""
    dt = now()
    return {
        "year": dt.year,
        "month": dt.month,
        "day": dt.day,
        "hour": dt.hour,
        "minute": dt.minute,
        "weekday": _WEEKDAYS[dt.weekday()],
        "season": _season(dt.month),
        "is_night": dt.hour >= 19 or dt.hour < 6,
        "period": _period(dt.hour),
    }


def _period(hour: int) -> str:
    """时间段描述。"""
    if 5 <= hour < 9:
        return "早上"
    if 9 <= hour < 12:
        return "上午"
    if 12 <= hour < 14:
        return "中午"
    if 14 <= hour < 18:
        return "下午"
    if 18 <= hour < 22:
        return "晚上"
    return "深夜"


def format_now() -> str:
    """格式化成给 AI 看的中文时间文本。"""
    d = get_time_dict()
    return (
        f"现在是{d['year']}年{d['month']}月{d['day']}日 {d['weekday']}，"
        f"{d['period']} {d['hour']:02d}:{d['minute']:02d}（{d['season']}）"
        f"{'，已是夜晚' if d['is_night'] else ''}"
    )


def get_time_context() -> str:
    """生成注入 AI 上下文的实时时间信息（含时间段建议）。"""
    d = get_time_dict()
    tips = []
    if d["is_night"]:
        tips.append("现在是夜晚，注意休息")
    if d["hour"] >= 12 and d["hour"] < 14:
        tips.append("到午休时间了")
    if 6 <= d["hour"] <= 8:
        tips.append("早上好，新的一天开始了")
    context = format_now()
    if tips:
        context += "；" + "，".join(tips)
    return context
