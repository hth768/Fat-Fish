# -*- coding: utf-8 -*-
"""好感度（关系分）模块。

对齐新版 cortico-world-qq-better 的 affinity.ts：
对**每个用户独立**维护一份好感度（-100~100，可正可负、随互动上下浮动，不是只增不减），
并在与该用户对话时把「当前好感度 + 关系状态 + 该把握的分寸」注入上下文，影响语气与分寸。

- 不注入数字、不刻意强调关系分，避免生硬。
- 落盘 data/affinity.json，跨重启不丢（按 bot 命名空间隔离）。
"""
import json
import os
import time

import agent_ctx

MIN_SCORE = -100
MAX_SCORE = 100


def _base_dir():
    return os.path.dirname(os.path.abspath(__file__))


def _store_file():
    return os.path.join(agent_ctx.agent_storage_dir(_base_dir()), "affinity.json")


def _clamp(n) -> int:
    try:
        n = float(n)
    except (TypeError, ValueError):
        return 0
    if n != n:  # NaN
        return 0
    if n < MIN_SCORE:
        return MIN_SCORE
    if n > MAX_SCORE:
        return MAX_SCORE
    return int(round(n))


def label(score) -> str:
    """好感度 → 关系状态描述（含态度指引）。"""
    try:
        s = int(score)
    except (TypeError, ValueError):
        s = 0
    if s >= 80:
        return '挚友（非常亲近，可以很放松很亲昵）'
    if s >= 50:
        return '很要好（明显有好感，态度可以热络）'
    if s >= 20:
        return '有好感（偏亲近，比普通朋友多一点）'
    if -10 < s < 20:
        return '普通（中性，正常有礼貌地相处即可）'
    if -40 < s <= -10:
        return '有点不爽（略带冷淡/客气，保持分寸）'
    if -70 < s <= -40:
        return '反感（明显不待见，客气但疏远）'
    return '厌恶（很负面，尽量公事公办、保持距离，别硬凑）'


def _load() -> dict:
    path = _store_file()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}
    entries = raw.get("entries") if isinstance(raw, dict) else None
    if not isinstance(entries, dict):
        return {}
    out = {}
    for k, v in entries.items():
        if isinstance(v, dict):
            out[str(k)] = {
                "score": _clamp(v.get("score", 0)),
                "name": v.get("name", "") or "",
                "note": v.get("note", "") or "",
                "updatedAt": float(v.get("updatedAt", 0) or 0),
                "lastReason": v.get("lastReason", "") or "",
            }
    return out


def _save(data: dict):
    try:
        path = _store_file()
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"entries": data}, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except OSError as e:
        print(f"[WARN] 好感度存档写入失败: {e}")


def get(user_id) -> dict:
    """取某用户的好感度条目（不存在则视为 0 分普通）。"""
    return _load().get(str(user_id), {"score": 0, "name": "", "note": "", "updatedAt": 0, "lastReason": ""})


def get_score(user_id) -> int:
    return int(get(user_id).get("score", 0))


def adjust(user_id, delta, reason: str = "", name: str = "") -> dict:
    """调整好感度（delta 可正可负；delta=0 仅用于记录昵称/备注）。返回调整后的条目。"""
    data = _load()
    key = str(user_id)
    cur = data.get(key) or {"score": 0, "name": key, "note": "", "updatedAt": 0, "lastReason": ""}
    try:
        delta = float(delta)
    except (TypeError, ValueError):
        delta = 0.0
    if delta:
        cur["score"] = _clamp(float(cur.get("score", 0)) + delta)
    if name:
        cur["name"] = str(name)
    if reason:
        cur["lastReason"] = str(reason)[:120]
    cur["updatedAt"] = time.time()
    data[key] = cur
    _save(data)
    return cur


def set_note(user_id, note: str, name: str = "") -> dict:
    data = _load()
    key = str(user_id)
    cur = data.get(key) or {"score": 0, "name": key, "note": "", "updatedAt": 0, "lastReason": ""}
    if name:
        cur["name"] = str(name)
    cur["note"] = str(note)[:200]
    cur["updatedAt"] = time.time()
    data[key] = cur
    _save(data)
    return cur


def list_all() -> list:
    """全部条目，按好感度从高到低。"""
    data = _load()
    items = [dict(v, qq=k) for k, v in data.items()]
    items.sort(key=lambda x: x.get("score", 0), reverse=True)
    return items


def count() -> int:
    return len(_load())


def build_hint(user_id) -> str:
    """生成注入上下文的「关系备忘」（不暴露数字，只说分寸）。"""
    entry = get(user_id)
    score = int(entry.get("score", 0))
    if score == 0 and not entry.get("note"):
        return ""
    lines = [f"【关系备忘】你对这个人当前的感觉：{label(score)}。"]
    if entry.get("note"):
        lines.append(f"备注：{entry['note']}")
    if entry.get("lastReason"):
        lines.append(f"最近一次变动原因：{entry['lastReason']}")
    lines.append("据此自然调节你的语气与分寸：亲近就放松亲昵，生疏/负面就客气疏远。"
                 "不要主动提到好感度这个词，也不要报数字。")
    return "\n".join(lines)


def list_text(limit: int = 50) -> str:
    """好感度总览（命令展示用）。"""
    items = list_all()
    if not items:
        return "还没有记录过任何人的好感度呢~"
    lines = [f"【好感度总览】共 {len(items)} 人"]
    for it in items[:limit]:
        qq = it.get("qq", "?")
        who = it.get("name") or qq
        reason = f"（{it['lastReason']}）" if it.get("lastReason") else ""
        lines.append(f"- {who}({qq})：{it.get('score', 0)} 分 · {label(it.get('score', 0))}{reason}")
    return "\n".join(lines)
