# -*- coding: utf-8 -*-
"""总结中心 API：会话摘要管理 + 手动总结对话（LLM）并沉淀到记忆。"""
import json
import os
import time

_MEMORY_FILE = "memory_data.json"


def _read_memory_file():
    try:
        with open(os.path.join(os.getcwd(), _MEMORY_FILE), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def summary_overview(bridge=None) -> dict:
    """当前 App 会话摘要（实时）+ 落盘的所有会话摘要。"""
    data = _read_memory_file()
    summaries = data.get("summary") or {}
    topics = data.get("topic") or {}
    store = data.get("store") or {}

    live = {}
    if bridge is not None and bridge.is_running():
        try:
            mem = bridge.core.chat.memory
            live = {
                "key": "app_owner",
                "summary": mem.get_summary("private", None, "app_owner"),
                "topic": mem.get_topic("private", None, "app_owner"),
                "short_turns": len(mem.get("private", None, "app_owner") or []),
            }
        except Exception as e:
            live = {"error": repr(e)}

    items = []
    for key in set(summaries) | set(topics):
        items.append({"key": key, "summary": summaries.get(key, ""),
                      "topic": topics.get(key, ""), "turns": len(store.get(key) or [])})
    items.sort(key=lambda i: (i["key"] != "app_owner", i["key"]))
    return {"live": live, "items": items[:80]}


def set_session_summary(bridge, text: str) -> dict:
    """更新 App 会话（app_owner）的摘要记忆（经 ChatService 的记忆适配层，立即生效）。"""
    text = (text or "").strip()
    if not bridge.is_running():
        # 核心没开时直接改落盘文件（下次启动会被 Memory.load 读回）
        data = _read_memory_file()
        data.setdefault("summary", {})["app_owner"] = text
        with open(os.path.join(os.getcwd(), _MEMORY_FILE), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return {"ok": True, "mode": "file"}
    mem = bridge.core.chat.memory
    mem.set_summary("private", None, "app_owner", text)
    return {"ok": True, "mode": "live"}


async def _summarize_llm(text: str) -> str:
    from ai_provider import get_llm
    prompt = (
        "下面是你（肥鱼娘）和主人的一段对话记录。请做一份总结沉淀，方便以后回忆：\n"
        "1. 先用 2-3 句话概括这段对话聊了什么；\n"
        "2. 再列出「值得长期记住的事实」（关于主人的信息、约定、结论、偏好等），"
        "没有就写「无」。\n"
        "直接输出，不要客套。\n\n[对话记录]\n" + text[:12000]
    )
    out = await get_llm().chat([{"role": "user", "content": prompt}], capability="chat")
    return str(out or "").strip()


async def run_summary(user_id: str, count: int = 60, save_to: str = "none",
                      keyword: str = "") -> dict:
    """取某用户最近 N 条历史 -> LLM 总结 -> 按 save_to 沉淀。"""
    import long_term_memory
    if keyword:
        records = long_term_memory.search_history(user_id, keyword, limit=count)
    else:
        records = long_term_memory.get_user_history(user_id, limit=count)
    records = records or []
    if not records:
        return {"ok": False, "error": "没有找到对话历史"}

    lines = []
    for r in records:
        role = str(r.get("role", "?"))
        content = str(r.get("content", r.get("text", "")))
        ts = r.get("time") or r.get("ts") or ""
        lines.append(f"[{ts}] {role}: {content}")
    text = "\n".join(lines)

    summary = await _summarize_llm(text)
    saved = []
    if save_to == "notes" and summary:
        import important_notes
        important_notes.add_note(user_id, "【对话总结】" + summary[:1500], category="总结")
        saved.append("重要事项")
    elif save_to == "knowledge" and summary:
        import knowledge_store
        knowledge_store.add_fact(summary[:1500])
        saved.append("知识库")
    return {"ok": True, "count": len(records), "summary": summary, "saved": saved}
