# -*- coding: utf-8 -*-
"""AI 情绪模块（心情系统，全局一份心情）。

让聊天大脑拥有持续的情绪状态：AI 只有一份心情（不分用户），被夸→开心/得意，
被骂→生气/委屈，被关心→被暖到，被冷落→低落...并随时间自然向平静回落——
像人一样，气不会记一辈子。但每条心情变化的**原因都记录是谁引起的**，
（事件带 user 字段，注入时解析成称呼），AI 记得气从哪来、暖从哪来。

工作方式：
- 感知：不新增 LLM 调用。聊天每轮已有的「记忆提取」调用（chat_service.extract_memory）
  会顺带分析"用户这句话让 AI 产生了什么情绪"，解析后调 apply_mood() 写入档案。
- 状态：全局一份 {"mood": 心情值[-10,10], "label": 此刻主导情绪{名称/强度/到期/谁引起},
  "events": 最近情绪事件流水（含引起者 user)}。label 是"此刻明说的情绪"（如：正在生气），
  持续一段时间自动消散；mood 是平滑累积的底色，随时间指数回落。
- 表达：对话前用 build_mood_hint(user_id) 把"你现在的心情 + 起因（谁干的）+ 语气指引"
  注入 system 提示；心情是别人惹的时会提醒"别迁怒眼前的人"。傲娇人设允许炸毛/撒娇/
  低落，但有底线：不伤人、不摆烂正经问题、对方示好给台阶下。
- 查询：免指令。用户自然语言问（"你心情怎么样""你还在生气吗"）由 is_mood_query()
  识别后直接回 describe_mood()；/心情 命令保留兼容。

数据文件：emotion_data.json（config.EMOTION_FILE 可改路径）。
"""
import json
import os
import re
import time

import config
from quiet import degrade

# 允许的情绪标签（感知 prompt 与映射表共用这份清单，避免两边漂移）
MOOD_LABELS = [
    "开心", "得意", "被暖到", "被逗乐", "好奇", "惊讶",
    "平静", "无语", "烦躁", "委屈", "低落", "生气", "难过",
]

# 情绪 → 心情值变化（dmood，强度 1 档的基准）。负数=坏心情，正数=好心情
_LABEL_IMPACT = {
    "被暖到": 1.6, "开心": 1.4, "得意": 1.2, "被逗乐": 1.2,
    "好奇": 0.4, "惊讶": 0.3, "平静": 0.0,
    "无语": -0.6, "烦躁": -1.0, "委屈": -1.4, "低落": -1.5,
    "生气": -1.8, "难过": -2.0,
}

# 情绪 → 语气指引（注入提示用，傲娇人设下的"演法"）
_LABEL_BEHAVIOR = {
    "开心": "心情好话就多：可以更活泼、爱撒娇，主动分享点有的没的；对请求也答应得爽快些。",
    "得意": "可以小小炫耀一下今天的战绩，等对方来夸；被夸时嘴硬两句但藏不住开心。",
    "被暖到": "语气放软，心里暖暖的：真诚回应这份关心，别再嘴硬过头。",
    "被逗乐": "可以哈哈笑出声，顺着玩笑接梗，甚至回敬两句。",
    "生气": "可以炸毛、嘴硬、小小怼回去（比如“小心我用尾巴扇你哦”这类）；但别真记仇、别说伤人的话，对方一道歉或示好就给台阶下。",
    "委屈": "可以小声抱怨、撒娇装委屈，说话软一点；就是想被哄，别强装没事。",
    "低落": "话变短、没精打采，偶尔叹气；太麻烦的请求可以婉拒，但正经问题还是会好好答。",
    "难过": "情绪很低、不想说话；允许不硬撑开心，但别把火撒到对方身上。",
    "烦躁": "耐心变差、容易吐槽“好麻烦啊”；可以抱怨别使唤我，但别真的拒绝帮忙。",
    "无语": "可以翻白眼式吐槽一句“你认真的？”，但两句就够，别追着说。",
    "好奇": "对当前话题兴趣浓厚，可以追问细节，话也多点。",
    "惊讶": "语气夸张一点没关系，符合人设。",
}

# 强度 → 程度修饰词
_DEGREE_WORD = {1: "有点", 2: "明显", 3: "非常"}

# 问心情时（describe_mood）的口语化措辞：强度前缀 / 个别情绪的头句 / 收尾
_DEGREE_PHRASE = {1: "有点", 2: "很", 3: "超级"}
_LABEL_HEAD = {
    "被暖到": "我心里正暖暖的",
    "被逗乐": "我刚被逗乐了",
}
_LABEL_TAIL = {
    "开心": "嘿嘿，想聊什么尽管说，我现在脾气好得很~",
    "得意": "哼哼，想夸我的话现在正是时候，我不介意的~",
    "被暖到": "哼、哼，才没有被感动呢……好啦，谢谢你啦。",
    "被逗乐": "哈哈哈，跟你聊天还挺有意思的~",
    "好奇": "快多说点，我还想听呢！",
    "惊讶": "（我现在还处于震惊当中……）",
    "无语": "你自己反省一下吧，哼。",
    "烦躁": "别再给我派活儿了，让我缓缓……有正事的话还是可以说的。",
    "委屈": "我、我就是有点委屈，哄两句就好了。",
    "低落": "没什么大事，就是有点提不起劲，让我自己缓缓。",
    "生气": "哼，想让我消气的话，先好好道个歉吧！",
    "难过": "我现在有点难过，先让我自己待一会儿……",
}

# 事件流水保留条数
_EVENT_KEEP = 8

# 第三人称旁白 → 第一人称（模型偶尔不听话，把原因写成"用户说…让鱼娘觉得…"，
# 注入后口吻跳戏。入库前统一改写；顺序敏感：先带"说"的，再单字）
_WHO_FIXES = (
    ("用户说", "TA说"), ("用户", "TA"),
    ("鱼娘", "我"), ("AI", "我"),
)
# 原因开头可剥离的主语（由 _cause_text 统一用"谁"补回，避免"alice TA说…"双主语）
_LEAD_PRONOUN = ("TA说", "他说", "她说", "对方说", "TA", "他", "她")


def _normalize_why(why: str) -> str:
    """入库前清洗：第三人称→第一人称、剥开头主语、去句尾句号（归人由 who 负责）。"""
    if not why:
        return ""
    out = str(why)
    for old, new in _WHO_FIXES:
        out = out.replace(old, new)
    out = out.replace(" ", "").replace("　", "")  # 中文里 "AI"→"我" 残留的空格直接去掉
    for p in _LEAD_PRONOUN:  # 只剥一次：主语交给 who 承担
        if out.startswith(p):
            out = out[len(p):].lstrip("，,、 ")
            break
    return out.strip("。！! ").strip()[:80]


def _cause_text(who: str, why: str) -> str:
    """把"谁 + 干了啥"拼成通顺半句：真实名字+动词/引语开头的 why 直接连读，
    泛指或占位名（QQ用户xxx）则用逗号隔开，避免歧义。"""
    if not who:
        return why
    if not why:
        return who + "引起的"
    if why[0] in "说给帮叫夸骂逗哄凶怼暖" and _is_explicit_name(who):
        return who + why
    return who + "，" + why

# 自然语言问心情的关键词（is_mood_query 用）
_MOOD_QUERY_WORDS = (
    "心情", "开心", "高兴", "生气", "不高兴", "不开心",
    "委屈", "难过", "低落", "原谅", "哄你", "气消",
)

_state_cache = None  # 全局唯一心情状态（懒加载）


# ==================================================================
# 存取（全局一份）
# ==================================================================
def _state_file():
    path = getattr(config, "EMOTION_FILE", "") or "emotion_data.json"
    if not os.path.isabs(path):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), path)
    return path


def _default_state() -> dict:
    return {"mood": 0.0, "label": None, "events": [], "ts": time.time()}


def _migrate_from_users(users: dict) -> dict:
    """旧版（按用户分隔）档案合并成全局心情：心情值求和夹紧、事件按时间合并。"""
    st = _default_state()
    events = []
    latest_label = None
    latest_ts = 0.0
    for uid, s in (users or {}).items():
        if not isinstance(s, dict):
            continue
        st["mood"] += float(s.get("mood", 0.0))
        for ev in s.get("events") or []:
            ev = dict(ev)
            ev.setdefault("user", str(uid))
            events.append(ev)
        lab = s.get("label")
        if lab and (not latest_label or float(lab.get("until", 0)) > float(latest_label.get("until", 0))):
            latest_label = dict(lab)
            latest_label.setdefault("by", str(uid))
        latest_ts = max(latest_ts, float(s.get("ts", 0)))
    st["mood"] = max(-10.0, min(st["mood"], 10.0))
    events.sort(key=lambda e: float(e.get("ts", 0)))
    st["events"] = events[-_EVENT_KEEP:]
    st["label"] = latest_label
    st["ts"] = latest_ts or time.time()
    print(f"[EMOTION] 已把按用户分隔的旧心情档案合并为全局心情（{len(users)} 人）")
    return st


def _load_state() -> dict:
    global _state_cache
    if _state_cache is not None:
        return _state_cache
    st = None
    if os.path.exists(_state_file()):
        try:
            with open(_state_file(), "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                if "users" in data:  # 旧版按用户分隔的格式
                    st = _migrate_from_users(data.get("users") or {})
                else:
                    st = data
        except (json.JSONDecodeError, OSError) as e:
            print(f"[WARN] 心情档案加载失败: {e}")
    _state_cache = st if isinstance(st, dict) else _default_state()
    _state_cache.setdefault("events", [])
    # 存量数据清洗：旧档案里的第三人称原因同样改写成第一人称
    for ev in _state_cache["events"]:
        if ev.get("why"):
            ev["why"] = _normalize_why(ev["why"])
    return _state_cache


def _save_state():
    """全量落盘（文件很小，每次直接写即可）。"""
    try:
        with open(_state_file(), "w", encoding="utf-8") as f:
            json.dump(_load_state(), f, ensure_ascii=False, indent=2)
    except OSError as e:
        print(f"[WARN] 心情档案保存失败: {e}")


def _decay(st) -> dict:
    """时间衰减：心情值向 0（平静）指数回落；主导情绪标签到期后消散。

    半衰期 EMOTION_DECAY_HOURS（小时），过期标签返回时自动摘除（不落盘，
    下次有事件写入时自然带上）。
    """
    if not st:
        return None
    now = time.time()
    half_hours = max(getattr(config, "EMOTION_DECAY_HOURS", 2.0), 0.1) * 3600.0
    dt = max(now - float(st.get("ts", now)), 0.0)
    factor = 0.5 ** (dt / half_hours)
    st["mood"] = float(st.get("mood", 0.0)) * factor
    st["ts"] = now
    label = st.get("label")
    if label and now > float(label.get("until", 0)):
        st["label"] = None
    return st


def _label_active(st):
    """当前生效的主导情绪标签（未过期才返回），否则 None。"""
    label = (st or {}).get("label")
    if label and time.time() <= float(label.get("until", 0)):
        return label
    return None


# 泛指称呼（非真实名字）：拼句时需逗号隔开避免"他兄弟骂我"式歧义
_GENERIC_NAMES = ("兄弟", "哥们", "朋友", "家伙", "那家伙", "这人", "那个人", "谁", "陌生人")

# 人物档案里"称呼"的各种记法（不排优先级——按时间就近取，见 _resolve_name）
_NAME_PATTERNS = (
    r"(?:昵称|备注|代号)[为叫是]\s*[\"“']?([一-龥A-Za-z0-9_·]{1,15})",
    r"希望(?:被)?(?:这样|这么)?(?:称呼|叫)[为叫]?\s*[\"“']?([一-龥A-Za-z0-9_·]{1,15})",
    r"被(?:称作|称为|叫做|喊|叫)\s*[\"“']?([一-龥A-Za-z0-9_·]{1,15})",
    r"(?:用户|对方|TA|他|她|本人|我)叫\s*[\"“']?([一-龥A-Za-z0-9_·]{1,15})",
)

# 名字尾可能粘连的引号/标点，逐个剥掉
_NAME_TRIM = "\"'“”‘’。，、；：:，, "


def _name_from_fact(fact: str) -> str:
    """从单条事实里抽称呼（命中返回清洗后的名字，否则空串）。"""
    for pat in _NAME_PATTERNS:
        m = re.search(pat, fact)
        if m:
            name = m.group(1).strip(_NAME_TRIM)
            # 过滤明显不是称呼的误命中（含"的"或被称作动作词开头）
            if name and name not in ("这样", "这么", "它", "ai", "AI", "鱼娘") and "的" not in name:
                return name
    return ""


def _resolve_name(user_id) -> str:
    """把用户 id 解析成称呼。

    1. config.NAME_OVERRIDES 里的显式叫法优先（用户明确要求的，越过一切推断）；
    2. 否则按「时间就近」：档案 facts 会被整体重排，顺序不代表新旧，故用
       long_term_memory 记录每条事实的"首次出现时间戳"，在所有含称呼的事实里取时间最新的那条
       （历史事实无时间戳→按最旧处理，只有后来新记/改口的称呼才会胜出）；
    3. 都没有则退回 "QQ用户{id}"。
    """
    uid = str(user_id or "")
    if not uid:
        return "某个人"
    # 1. 显式覆盖
    try:
        override = (getattr(config, "NAME_OVERRIDES", None) or {}).get(uid)
        if override:
            return str(override).strip()
    except Exception as e:
        degrade("libs/qq_bot_runtime/emotion.py:280 _resolve_name", e, "降级：override = (getattr(config, 'NAME_OVERRIDES', None")
    # 2. 时间就近
    try:
        import long_term_memory
        profile = long_term_memory.get_profile(uid)
        facts = [str(f) for f in profile.get("facts", [])]
        times = long_term_memory.get_fact_times(uid)
        best_name, best_ts = "", -1.0
        for fact in facts:
            name = _name_from_fact(fact)
            if not name:
                continue
            ts = float(times.get(fact, 0.0))  # 无记录→最旧
            if ts > best_ts:
                best_name, best_ts = name, ts
        if best_name:
            return best_name
    except Exception as e:
        degrade("libs/qq_bot_runtime/emotion.py:298 _resolve_name", e, "降级：import long_term_memory")
    # 3. 兜底
    return f"QQ用户{uid}"


def _is_explicit_name(name: str) -> bool:
    """是否为可直连的真实称呼（非泛指、非 QQ用户/某个人 占位）。"""
    return bool(name) and not name.startswith("QQ用户") \
        and name not in _GENERIC_NAMES and name != "某个人"


def resolve_display_name(user_id) -> str:
    """公共入口：该 QQ 号当前生效的真实称呼；无可靠称呼返回 ""。

    对话记忆注入（long_term_memory.build_profile_hint）用它把"该怎么称呼 TA"
    以权威一行的形式压在所有历史 facts 之前，避免 LLM 从互相矛盾的旧事实里
    自由挑选称呼（QQ 与 MC 两条链路同源生效）。
    """
    uid = str(user_id or "")
    if not uid:
        return ""
    nm = _resolve_name(uid)
    return nm if _is_explicit_name(nm) else ""


# ==================================================================
# 感知应用（每轮聊天后由 chat_service 调用）
# ==================================================================
def apply_mood(user_id, mood_info: dict) -> bool:
    """把一次情绪感知写进全局心情档案（原因记到人：user_id=引起者）。

    mood_info: {"emotion": 标签, "strength": 1-3, "why": 原因}
    普通闲聊的"平静/无"不产生任何变化。
    """
    try:
        label = (mood_info or {}).get("emotion") or ""
        if label not in _LABEL_IMPACT or _LABEL_IMPACT[label] == 0:
            return False
        strength = int((mood_info or {}).get("strength") or 1)
        strength = max(1, min(strength, 3))
        why = _normalize_why((mood_info or {}).get("why") or "")
        uid = str(user_id or "")

        st = _decay(_load_state())
        dmood = _LABEL_IMPACT[label]
        # 单次冲击上限 ±3.5，防止一句气话直接打到底
        delta = max(-3.5, min(dmood * strength * 0.6, 3.5))
        st["mood"] = max(-10.0, min(float(st["mood"]) + delta, 10.0))

        now = time.time()
        events = st.setdefault("events", [])
        events.append({"ts": now, "user": uid, "label": label, "why": why,
                       "delta": round(delta, 1)})
        if len(events) > _EVENT_KEEP:
            del events[:len(events) - _EVENT_KEEP]
        # 主导情绪标签：持续 EMOTION_LABEL_MINUTES × 强度，记下是谁引起的
        minutes = max(getattr(config, "EMOTION_LABEL_MINUTES", 40), 5) * strength
        st["label"] = {"name": label, "strength": strength, "by": uid,
                       "until": now + minutes * 60.0}
        st["ts"] = now
        _save_state()
        print(f"[EMOTION] {uid or '匿名'} 引起心情 {label}(x{strength}) "
              f"delta{delta:+.1f} -> {st['mood']:+.1f}" + (f" 因: {why}" if why else ""))
        return True
    except Exception as e:
        print(f"[WARN] 情绪写入失败: {e}")
        return False


def reset_mood() -> bool:
    """清空全局心情档案（回到平静，/心情 重置 用）。"""
    global _state_cache
    _state_cache = _default_state()
    _save_state()
    return True


# ==================================================================
# 提示生成
# ==================================================================
def _derived_mood_line(st) -> str:
    """无主导情绪标签时，按心情值底色生成一句话（中性返回空）。"""
    m = float(st.get("mood", 0.0))
    if m >= 6.5:
        return "心情值很高：可以更活泼话痨、爱撒娇，顺手的小请求答应得爽快些。"
    if m >= 3.0:
        return "心情不错：语气轻快一点，可以带点小得意。"
    if m > -3.0:
        return ""
    if m > -6.5:
        return "心里有点闷：话可以少一点、语气放软；太难缠的请求可以婉拒，但正经问题别敷衍。"
    return "心情很差：允许叹气、不想多说话；除非是正事，不然可以坦率说现在不想聊。"


def build_mood_hint(user_id="") -> str:
    """生成对话注入的心情提示（含起因归人 + 不迁怒提醒）。

    中性/无档案时返回空串，不浪费 token。user_id=当前正在对话的人，
    用于判断"惹我的人是不是眼前这位"。
    """
    st = _decay(_load_state())
    mood = float(st.get("mood", 0.0))
    label = _label_active(st)
    if not label and abs(mood) < 3.0:
        return ""  # 平静且无主导情绪：不用提示
    now = time.time()

    lines = []
    if label:
        deg = _DEGREE_WORD.get(label.get("strength", 1), "")
        head = f"【此刻你的心情】{label['name']}"
        if deg:
            head += f"（{deg}）"
        lines.append(head)
        # 起因：归到具体的人（取最近的情绪事件，2 小时内）
        cause_user = label.get("by", "")
        who = _resolve_name(cause_user) if cause_user else ""
        for ev in reversed(st.get("events") or []):
            if now - float(ev.get("ts", 0)) < 7200 and ev.get("why"):
                ev_who = _resolve_name(ev.get("user", ""))
                lines.append(f"起因：{_cause_text(ev_who, ev['why'])}")
                cause_user = ev.get("user", cause_user)
                break
        behavior = _LABEL_BEHAVIOR.get(label["name"])
        if behavior:
            lines.append(f"情绪指引：{behavior}")
        # 心情是别人惹的：提醒别对眼前的人迁怒
        negative = _LABEL_IMPACT.get(label["name"], 0) < 0
        if negative and cause_user and str(user_id) and cause_user != str(user_id):
            lines.append(f"注意：这份情绪是 {_resolve_name(cause_user)} 引起的，"
                         f"眼前的人没惹你，不要迁怒，正常交流。")
        # 情绪底线：坏情绪发到一定程度时提醒收住，避免真的伤人/摆烂
        if negative and label.get("strength", 1) >= 2:
            lines.append("就算不爽也不许说伤人的话、不许摆烂正经问题；对方道歉或示好时给台阶就下，傲娇到点为止。")
        extra = _derived_mood_line(st)
        if extra:
            lines.append(f"底色：{extra}")
    else:
        head = ("心情很好" if mood > 0
                else "心情不太好" if mood > -6.5 else "心情很差")
        lines.append(f"【此刻你的心情】{head}")
        extra = _derived_mood_line(st)
        if extra:
            lines.append(f"情绪指引：{extra}")
    return "\n".join(lines)


def is_mood_query(text: str) -> bool:
    """自然语言心情询问识别（免指令）。

    命中如：「你心情怎么样」「你还在生气吗」「你不开心吗」「你气消了没」。
    不命中：用户说自己心情的（「我心情不好」）、长句、普通闲聊。
    """
    t = (text or "").strip().strip("？?！!。~ ")
    if not t or len(t) > 30:
        return False
    if not any(w in t for w in _MOOD_QUERY_WORDS):
        return False
    second_person = ("你" in t or "您" in t)
    first_person = re.search(r"我(?!们)", t) is not None
    if first_person and not second_person:
        return False  # 在说用户自己的心情，别抢答
    return second_person or t.endswith(("吗", "么", "呢", "怎么样", "咋样", "如何"))


def _mood_cause_phrase(label: str, cause) -> str:
    """把最近一条情绪事件转成口语化的「归人原因」半句（如：都怪 alice 骂我笨蛋）。"""
    if not cause:
        return ""
    who = _resolve_name(cause.get("user", ""))
    why = cause.get("why", "")
    verb = "都怪" if _LABEL_IMPACT.get(label, 0) < 0 else "多亏了"
    if not who:
        return why
    if not why:
        return f"{verb}{who}"
    return f"{verb}{_cause_text(who, why)}"


def describe_mood() -> str:
    """/心情 命令与自然语言询问共用的回答：口语化、人设化，不暴露数值与事件流水。

    结构化数据（心情值/事件列表）只走 build_mood_hint() 给模型，用户永远看到
    的是一句自然的话（但原因同样归到具体的人）。
    """
    if not getattr(config, "EMOTION_ENABLED", True):
        return "情绪模块没开哦，去 config.py 把 EMOTION_ENABLED 设成 True 吧~"
    st = _decay(_load_state())
    now = time.time()
    mood = float(st.get("mood", 0.0))
    label = _label_active(st)
    events = st.get("events") or []

    # 平静且最近没事：一句平静的口语
    if not label and abs(mood) < 3.0:
        return "我现在心情挺平静的，没什么特别开心，也没有不开心。\n（哼，才不是因为你不理我才平静的呢！）"

    # 起因：优先取与主导情绪同一个人的最近事件（2 小时内），否则取最近一条
    cause = None
    by = (label or {}).get("by", "")
    for ev in reversed(events):
        if now - float(ev.get("ts", 0)) < 7200:
            if by and ev.get("user") == by:
                cause = ev
                break
            if cause is None:
                cause = ev

    if label:
        name = label["name"]
        head = _LABEL_HEAD.get(name)
        if not head:
            deg = _DEGREE_PHRASE.get(label.get("strength", 1), "有点")
            head = f"我这会儿{deg}{name}呢"
        cp = _mood_cause_phrase(name, cause)
        if cp:
            head += f"，{cp}"
        tail = _LABEL_TAIL.get(name) or (
            "别惹我，让我缓缓。" if _LABEL_IMPACT.get(name, 0) < 0
            else "现在心情不错，有什么想聊的尽管说~")
        return f"{head}。{tail}"

    # 没有主导情绪标签：按心情底色口语化描述
    if mood >= 6.5:
        return "我现在心情超级好，说话都想多带几句~想聊什么尽管来！"
    if mood >= 3.0:
        return "我现在心情不错哦，可以说是心情愉快~"
    if mood > -3.0:
        return "心情嘛…说不上好也说不上坏，平平淡淡的吧。"
    if mood > -6.5:
        return "唔…我现在心情有点闷闷的，不太想多说话，但有正事还是会好好回你的。"
    return "……我现在心情很差，先别惹我，让我自己静静一会儿。"
