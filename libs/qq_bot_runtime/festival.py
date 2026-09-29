# -*- coding: utf-8 -*-
"""节日 / 节气 / 农历感知（对齐新版 cortico-world-qq-better 的 festival.ts）。

把「今天」换算成农历日期与节日信息，注入上下文，让 AI 具备节日与节气意识
（公历节日 + 农历节日 + 二十四节气）。

- 农历数据基于公开标准 lunarInfo(1900-2100) 表，节气用标准天文近似公式。
- 覆盖 1900~2100，运行期零依赖、零网络。
"""
import datetime as _dt

# 农历 1900-2100 润大小信息表（公开标准数据，每项 16bit）
LUNAR_INFO = [
    0x04bd8, 0x04ae0, 0x0a570, 0x054d5, 0x0d260, 0x0d950, 0x16554, 0x056a0, 0x09ad0, 0x055d2,  # 1900-1909
    0x04ae0, 0x0a5b6, 0x0a4d0, 0x0d250, 0x1d255, 0x0b540, 0x0d6a0, 0x0ada2, 0x095b0, 0x14977,  # 1910-1919
    0x04970, 0x0a4b0, 0x0b4b5, 0x06a50, 0x06d40, 0x1ab54, 0x02b60, 0x09570, 0x052f2, 0x04970,  # 1920-1929
    0x06566, 0x0d4a0, 0x0ea50, 0x06e95, 0x05ad0, 0x02b60, 0x186e3, 0x092e0, 0x1c8d7, 0x0c950,  # 1930-1939
    0x0d4a0, 0x1d8a6, 0x0b550, 0x056a0, 0x1a5b4, 0x025d0, 0x092d0, 0x0d2b2, 0x0a950, 0x0b557,  # 1940-1949
    0x06ca0, 0x0b550, 0x15355, 0x04da0, 0x0a5b0, 0x14573, 0x052b0, 0x0a9a8, 0x0e950, 0x06aa0,  # 1950-1959
    0x0aea6, 0x0ab50, 0x04b60, 0x0aae4, 0x0a570, 0x05260, 0x0f263, 0x0d950, 0x05b57, 0x056a0,  # 1960-1969
    0x096d0, 0x04dd5, 0x04ad0, 0x0a4d0, 0x0d4d4, 0x0d250, 0x0d558, 0x0b540, 0x0b6a0, 0x195a6,  # 1970-1979
    0x095b0, 0x049b0, 0x0a974, 0x0a4b0, 0x0b27a, 0x06a50, 0x06d40, 0x0af46, 0x0ab60, 0x09570,  # 1980-1989
    0x04af5, 0x04970, 0x064b0, 0x074a3, 0x0ea50, 0x06b58, 0x055c0, 0x0ab60, 0x096d5, 0x092e0,  # 1990-1999
    0x0c960, 0x0d954, 0x0d4a0, 0x0da50, 0x07552, 0x056a0, 0x0abb7, 0x025d0, 0x092d0, 0x0cab5,  # 2000-2009
    0x0a950, 0x0b4a0, 0x0baa4, 0x0ad50, 0x055d9, 0x04ba0, 0x0a5b0, 0x15176, 0x052b0, 0x0a930,  # 2010-2019
    0x07954, 0x06aa0, 0x0ad50, 0x05b52, 0x04b60, 0x0a6e6, 0x0a4e0, 0x0d260, 0x0ea65, 0x0d530,  # 2020-2029
    0x05aa0, 0x076a3, 0x096d0, 0x04afb, 0x04ad0, 0x0a4d0, 0x1d0b6, 0x0d250, 0x0d520, 0x0dd45,  # 2030-2039
    0x0b5a0, 0x056d0, 0x055b2, 0x049b0, 0x0a577, 0x0a4b0, 0x0aa50, 0x1b255, 0x06d20, 0x0ada0,  # 2040-2049
    0x14b63, 0x09370, 0x049f8, 0x04970, 0x064b0, 0x168a6, 0x0ea50, 0x06b20, 0x1a6c4, 0x0aae0,  # 2050-2059
    0x0a2e0, 0x0d2e3, 0x0c960, 0x0d557, 0x0d4a0, 0x0da50, 0x05d55, 0x056a0, 0x0a6d0, 0x055d4,  # 2060-2069
    0x052d0, 0x0a9b8, 0x0a950, 0x0b4a0, 0x0b6a6, 0x0ad50, 0x055a0, 0x0aba4, 0x0a5b0, 0x052b0,  # 2070-2079
    0x0b273, 0x06930, 0x07337, 0x06aa0, 0x0ad50, 0x14b55, 0x04b60, 0x0a570, 0x054e4, 0x0d160,  # 2080-2089
    0x0e968, 0x0d520, 0x0daa0, 0x16aa6, 0x056d0, 0x04ae0, 0x0a9d4, 0x0a2d0, 0x0d150, 0x0f252,  # 2090-2099
    0x0d520,  # 2100
]

CN_MONTH = ['正', '二', '三', '四', '五', '六', '七', '八', '九', '十', '冬', '腊']
CN_DAY = [
    '初一', '初二', '初三', '初四', '初五', '初六', '初七', '初八', '初九', '初十',
    '十一', '十二', '十三', '十四', '十五', '十六', '十七', '十八', '十九', '二十',
    '廿一', '廿二', '廿三', '廿四', '廿五', '廿六', '廿七', '廿八', '廿九', '三十',
]

# 公历节日（月-日 → 名称）
SOLAR_FESTIVALS = {
    '1-1': '元旦', '2-14': '情人节', '3-8': '妇女节', '3-12': '植树节',
    '3-15': '消费者权益日', '4-1': '愚人节', '5-1': '劳动节', '5-4': '青年节',
    '6-1': '儿童节', '7-1': '建党节', '8-1': '建军节', '9-10': '教师节',
    '10-1': '国庆节', '11-11': '双十一', '12-24': '平安夜', '12-25': '圣诞节',
    '12-31': '跨年',
}

# 农历节日（农历月-日 → 名称；除夕单独处理）
LUNAR_FESTIVALS = {
    '1-1': '春节', '1-15': '元宵节', '2-2': '龙抬头', '5-5': '端午节',
    '7-7': '七夕', '7-15': '中元节', '8-15': '中秋节', '9-9': '重阳节',
    '12-8': '腊八节', '12-23': '北方小年', '12-24': '南方小年',
}

# 二十四节气名（按序号 1..24）
TERM_NAMES = [
    '小寒', '大寒', '立春', '雨水', '惊蛰', '春分', '清明', '谷雨',
    '立夏', '小满', '芒种', '夏至', '小暑', '大暑', '立秋', '处暑',
    '白露', '秋分', '寒露', '霜降', '立冬', '小雪', '大雪', '冬至',
]

# 节气计算系数（1900 年起，单位分钟，标准天文近似）
S_TERM_INFO = [
    0, 21208, 42467, 63836, 85337, 107014, 128867, 150921, 173149, 195551,
    218072, 240693, 263343, 285989, 308563, 331033, 353350, 375494, 397447,
    419210, 440795, 462224, 483532, 504758,
]

_TERM_EPOCH = _dt.datetime(1900, 1, 6, 2, 5)


def _leap_month(y: int) -> int:
    """农历 y 年闰月（0=无）。"""
    if y < 1900 or y > 2100:
        return 0
    return LUNAR_INFO[y - 1900] & 0xf


def _leap_days(y: int) -> int:
    """农历 y 年闰月天数（0=无闰月）。"""
    if _leap_month(y):
        return 30 if (LUNAR_INFO[y - 1900] & 0x10000) else 29
    return 0


def _month_days(y: int, m: int) -> int:
    """农历 y 年 m 月（非闰月）天数。"""
    return 30 if (LUNAR_INFO[y - 1900] & (0x10000 >> m)) else 29


def _year_days(y: int) -> int:
    """农历 y 年总天数。"""
    total = 348
    i = 0x8000
    while i > 0x8:
        if LUNAR_INFO[y - 1900] & i:
            total += 1
        i >>= 1
    return total + _leap_days(y)


def solar_to_lunar(y: int, m: int, d: int) -> dict:
    """公历转农历，返回 {"year","month","day","is_leap"}。"""
    if y < 1900 or y > 2100:
        return {"year": y, "month": m, "day": d, "is_leap": False}
    offset = (_dt.date(y, m, d) - _dt.date(1900, 1, 31)).days
    i = 1900
    temp = 0
    while i < 2101 and offset > 0:
        temp = _year_days(i)
        offset -= temp
        i += 1
    if offset < 0:
        offset += temp
        i -= 1
    year = i
    leap = _leap_month(i)
    is_leap = False
    i = 1
    while i < 13 and offset > 0:
        if leap > 0 and i == leap + 1 and not is_leap:
            i -= 1
            is_leap = True
            temp = _leap_days(year)
        else:
            temp = _month_days(year, i)
        if is_leap and i == leap + 1:
            is_leap = False
        offset -= temp
        i += 1
    if offset == 0 and leap > 0 and i == leap + 1:
        if is_leap:
            is_leap = False
        else:
            is_leap = True
            i -= 1
    if offset < 0:
        offset += temp
        i -= 1
    return {"year": year, "month": i, "day": offset + 1, "is_leap": is_leap}


def _get_term(y: int, n: int) -> int:
    """第 n 个节气（1..24）在 y 年落到的公历日。"""
    ms = 31556925974.7 * (y - 1900) + S_TERM_INFO[n - 1] * 60000
    return (_TERM_EPOCH + _dt.timedelta(milliseconds=ms)).day


def _term_at(y: int, m: int, d: int) -> str:
    """今天是否落在某节气（返回节气名或 ""）。"""
    for n in (2 * m - 1, 2 * m):
        if d == _get_term(y, n):
            return TERM_NAMES[n - 1]
    return ""


def _nth_weekday(y: int, m: int, weekday: int, n: int) -> int:
    """某年某月第 n 个周几（weekday: 0=周一 … 6=周日）的日期。"""
    first = _dt.date(y, m, 1).weekday()
    diff = (weekday - first) % 7
    return 1 + diff + (n - 1) * 7


def lunar_text(y: int, m: int, d: int) -> str:
    """农历中文描述（如「八月十五」）。"""
    lu = solar_to_lunar(y, m, d)
    idx = max(0, min(lu["month"] - 1, 11))
    day_idx = max(0, min(lu["day"] - 1, 29))
    return f"{'闰' if lu['is_leap'] else ''}{CN_MONTH[idx]}月{CN_DAY[day_idx]}"


def festival_text(now=None) -> str:
    """生成「今天」的节日/节气/农历感知文本（供上下文注入）。"""
    now = now or _dt.datetime.now()
    y, m, d = now.year, now.month, now.day
    names = []

    key = f"{m}-{d}"
    if key in SOLAR_FESTIVALS:
        names.append(SOLAR_FESTIVALS[key])
    # 母亲节（五月第二个周日）/ 父亲节（六月第三个周日）
    if m == 5 and d == _nth_weekday(y, 5, 6, 2):
        names.append('母亲节')
    if m == 6 and d == _nth_weekday(y, 6, 6, 3):
        names.append('父亲节')

    lu = solar_to_lunar(y, m, d)
    if lu["month"] == 12 and lu["day"] == _month_days(lu["year"], 12):
        names.append('除夕')
    else:
        lkey = f"{lu['month']}-{lu['day']}"
        if lkey in LUNAR_FESTIVALS:
            names.append(LUNAR_FESTIVALS[lkey])

    term = _term_at(y, m, d)
    if term == '清明':
        names.append('清明节')
    elif term == '冬至':
        names.append('冬至')

    text = f"今日节日：{'、'.join(names)}" if names else "今日没有特定节日"
    text += f"（农历 {lunar_text(y, m, d)}）"
    if term and term not in ('清明', '冬至'):
        text += f"；今日节气：{term}"
    text += "。若用户提到节日、节气或「今天是什么日子」，可自然呼应（问候、聊相关话题），但不要生硬念节日名。"
    return text


def today_festival_short(now=None) -> str:
    """简短版（命令行/状态展示用）：节日名 + 农历，无节日则只农历。"""
    now = now or _dt.datetime.now()
    y, m, d = now.year, now.month, now.day
    names = []
    if f"{m}-{d}" in SOLAR_FESTIVALS:
        names.append(SOLAR_FESTIVALS[f"{m}-{d}"])
    lu = solar_to_lunar(y, m, d)
    if lu["month"] == 12 and lu["day"] == _month_days(lu["year"], 12):
        names.append('除夕')
    elif f"{lu['month']}-{lu['day']}" in LUNAR_FESTIVALS:
        names.append(LUNAR_FESTIVALS[f"{lu['month']}-{lu['day']}"])
    term = _term_at(y, m, d)
    if term:
        names.append(term)
    head = "、".join(names) if names else "无节日"
    return f"{head}（农历 {lunar_text(y, m, d)}）"
