# -*- coding: utf-8 -*-
"""智能体私人笔记本（对齐新版 cortico-world-qq-better 的 notebook）。

让 AI 有一本自己的「私人笔记本」，记录它想记的东西（灵感、吐槽、对某人的看法、
不想忘的小事），与对话历史、长期记忆、闹钟/记事本解耦：
- reminder（闹钟）是**带时间、会被触发**的提醒；
- notebook 是纯**随手记**，不会到期打扰，只在被问起/注入上下文时被动查看。

- AI 自发记录：走 `chat_service.extract_memory` 的【笔记】类（复用记忆提取那次调用，不新增 LLM 调用）
- 手动：命令 `/记 <内容>` / `/笔记 列表` / `/笔记 <id>` / `/忘 <id>`
- 落盘 `data/notebook.jsonl`（JSONL，一行一条，按 bot 命名空间隔离）
"""
import json
import os
import random
import time

import agent_ctx

MAX_LEN = 2000


def _base_dir():
    return os.path.dirname(os.path.abspath(__file__))


def _store_file():
    return os.path.join(agent_ctx.agent_storage_dir(_base_dir()), "notebook.jsonl")


def gen_id() -> str:
    return f"n_{int(time.time() * 1000)}_{''.join(random.choice('0123456789abcdef') for _ in range(4))}"


def _read_all() -> list:
    path = _store_file()
    if not os.path.exists(path):
        return []
    items = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict) and obj.get("id"):
                    items.append(obj)
    except OSError as e:
        print(f"[WARN] 笔记本读取失败: {e}")
    return items


def _rewrite(items: list):
    try:
        path = _store_file()
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            for it in items:
                f.write(json.dumps(it, ensure_ascii=False) + "\n")
        os.replace(tmp, path)
    except OSError as e:
        print(f"[WARN] 笔记本写入失败: {e}")


def save(text: str, source: str = "") -> dict:
    """记一条笔记，返回该条目。"""
    text = (text or "").strip()
    if not text:
        return {}
    entry = {
        "id": gen_id(),
        "text": text[:MAX_LEN],
        "source": str(source or "")[:40],
        "createdAt": time.time(),
    }
    items = _read_all()
    # 去重：近期已记过几乎一样的就跳过（防止每轮复读同一句话）
    for old in items[-30:]:
        if old.get("text", "") == entry["text"]:
            return old
    items.append(entry)
    _rewrite(items)
    return entry


def list_all() -> list:
    """全部笔记，按时间从旧到新。"""
    items = _read_all()
    items.sort(key=lambda x: x.get("createdAt", 0))
    return items


def get(nid: str) -> dict:
    for it in _read_all():
        if it.get("id") == nid:
            return it
    return {}


def forget(nid: str) -> bool:
    items = _read_all()
    rest = [x for x in items if x.get("id") != nid]
    if len(rest) == len(items):
        return False
    _rewrite(rest)
    return True


def count() -> int:
    return len(_read_all())


def tail_text(limit: int = 10) -> str:
    """最近若干条笔记（供提示词注入，克制以免占上下文）。"""
    items = list_all()[-limit:]
    if not items:
        return ""
    lines = ["【我的私人笔记本（最近几条）】"]
    for it in items:
        lines.append(f"- {it.get('text', '')}")
    return "\n".join(lines)


def list_text(limit: int = 50) -> str:
    """笔记总览（命令展示用，带 id）。"""
    items = list_all()
    if not items:
        return "笔记本里还什么都没有呢~（你可以用 /记 <内容> 让我记一笔）"
    total = len(items)
    show = items[-limit:]
    lines = [f"【私人笔记本】共 {total} 条" + (f"（显示最后 {len(show)} 条）" if total > len(show) else "")]
    for idx, it in enumerate(show, 1):
        stamp = time.strftime("%m-%d %H:%M", time.localtime(it.get("createdAt", time.time())))
        body = it.get("text", "")
        if len(body) > 40:
            body = body[:40] + "…"
        lines.append(f"#{idx} {it.get('id')} [{stamp}] {body}")
    lines.append("看某条全文：/笔记 <id>　删除：/忘 <id>")
    return "\n".join(lines)
