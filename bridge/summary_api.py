# -*- coding: utf-8 -*-
"""总结中心 API：会话摘要管理 + 手动总结对话（LLM）并沉淀到记忆。"""
import json
import os
import time
import agent_ctx

_MEMORY_FILE = "memory_data.json"


def _read_memory_file():
    try:
        with open(os.path.join(os.getcwd(), _MEMORY_FILE), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _core_for(bridge, bot_id):
    if bridge is not None and getattr(bridge, "bot_manager", None) is not None:
        c = bridge.bot_manager.core_for(bot_id)
        if c is not None:
            return c
    return getattr(bridge, "core", None)


def summary_overview(bridge=None, bot_id=None) -> dict:
    """当前 App 会话摘要（实时，按 bot 隔离）+ 落盘的所有会话摘要。"""
    bid = bot_id or "feiyu"
    data = _read_memory_file()
    summaries = data.get("summary") or {}
    topics = data.get("topic") or {}
    store = data.get("store") or {}

    live = {}
    if bridge is not None and bridge.is_running():
        try:
            core = _core_for(bridge, bid)
            mem = core.chat.memory
            live = {
                "key": bid,
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
    items.sort(key=lambda i: (i["key"] != "app_owner", i["key"] != bid, i["key"]))
    return {"live": live, "items": items[:80]}


def set_session_summary(bridge, text: str, bot_id=None) -> dict:
    """更新某 bot 的 App 会话（app_owner）摘要记忆（经 ChatService 记忆适配层，立即生效）。"""
    bid = bot_id or "feiyu"
    text = (text or "").strip()
    if not bridge.is_running():
        # 核心没开时直接改落盘文件（下次启动会被 Memory.load 读回）
        data = _read_memory_file()
        data.setdefault("summary", {})[bid] = text
        with open(os.path.join(os.getcwd(), _MEMORY_FILE), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return {"ok": True, "mode": "file"}
    mem = _core_for(bridge, bid).chat.memory
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
                      keyword: str = "", bot_id: str = None) -> dict:
    """取某用户最近 N 条历史 -> LLM 总结 -> 按 save_to 沉淀。

    注意：本函数在事件循环线程执行（经 run_coro），HTTP 线程设置的 agent_ctx 不会
    传播到这里，因此必须自行设置 bot 上下文以隔离各 bot 的历史 / 事项。
    """
    tok = agent_ctx.set_agent(bot_id or "feiyu")
    try:
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
    finally:
        agent_ctx.reset_agent(tok)
