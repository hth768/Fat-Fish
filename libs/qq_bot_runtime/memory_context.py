# -*- coding: utf-8 -*-
"""统一记忆上下文模块。

让 AI 在任何对话场景（正常聊天、主动说话、群聊接话、未来新场景）都能注入完整记忆库。

用法（所有场景统一调用）：
    from memory_context import build_memory_messages
    msgs = build_memory_messages(user_id)
    messages = [system_prompt] + msgs + [user_message]

支持类型（可通过 include 参数控制）：
- profile: 人物档案（该用户的长期记忆）
- notes: 重要信息（用户主动要求记住的内容）
- ai_profile: AI 自我认知档案
- history: 主动历史检索（从全文历史找相关内容）
- reflection: 反思记忆（交互风格调整参考）
"""
import config


def build_memory_messages(user_id: str = "", include: dict = None) -> list:
    """构建完整记忆库的 system 消息列表。

    Args:
        user_id: 目标用户 QQ 号（用于人物档案/重要信息/历史检索）
        include: 可选，{"profile":bool,"notes":bool,"ai_profile":bool,"history":bool,"history_text":str,"reflection":bool}
                 控制注入哪些类型；缺省全部按 config 开关注入。

    Returns:
        list[{"role":"system","content":...}, ...]
    """
    include = include or {}
    msgs = []

    # 1. 人物档案（该用户的长期记忆）
    if include.get("profile", config.ENABLE_PROFILE) and user_id:
        try:
            import long_term_memory
            hint = long_term_memory.build_profile_hint(user_id)
            if hint:
                msgs.append({"role": "system", "content": hint})
        except Exception as e:
            print(f"[MEMORY-CTX] 人物档案注入失败: {e}")

    # 2. 重要信息（用户主动要求记住的内容）
    if include.get("notes", True) and user_id:
        try:
            import important_notes
            hint = important_notes.build_notes_hint(user_id)
            if hint:
                msgs.append({"role": "system", "content": hint})
        except Exception as e:
            print(f"[MEMORY-CTX] 重要信息注入失败: {e}")

    # 3. AI 自我认知档案
    if include.get("ai_profile", config.ENABLE_AI_PROFILE):
        try:
            import ai_profile
            hint = ai_profile.build_ai_profile_hint()
            if hint:
                msgs.append({"role": "system", "content": hint})
        except Exception as e:
            print(f"[MEMORY-CTX] AI自我认知注入失败: {e}")

    # 4. 主动历史检索（从全文历史找相关内容，需提供 user_id + 当前话题）
    history_text = include.get("history_text", "")
    if (include.get("history", config.ENABLE_HISTORY_RETRIEVAL)
            and user_id and history_text and not include.get("skip_history", False)):
        try:
            import asyncio
            from chat_service import retrieve_relevant_history
            loop = asyncio.get_event_loop()
            # 该函数是 async，需要异步调用；这里提供同步版本则跳过
            # 历史检索需要 async 上下文，这里用 asyncio.run 尝试（若无运行循环）
            try:
                if not loop.is_running():
                    related = asyncio.run(retrieve_relevant_history(user_id, history_text))
                else:
                    related = None
            except RuntimeError:
                related = None
            if related:
                msgs.append({
                    "role": "system",
                    "content": f"【从历史对话中检索到的相关内容，可能有助于回答】\n{related}"
                })
        except Exception as e:
            print(f"[MEMORY-CTX] 历史检索注入失败: {e}")

    # 5. 反思记忆（交互风格调整参考）
    if include.get("reflection", getattr(config, "ENABLE_REFLECTION", True)) and user_id:
        try:
            import reflection_memory
            hint = reflection_memory.build_reflection_hint(user_id)
            if hint:
                msgs.append({"role": "system", "content": hint})
        except Exception as e:
            print(f"[MEMORY-CTX] 反思记忆注入失败: {e}")

    # 6. 人格记忆（与该用户的独特相处模式）
    if include.get("persona", getattr(config, "ENABLE_PERSONA", True)) and user_id:
        try:
            import persona_memory
            hint = persona_memory.build_persona_hint(user_id)
            if hint:
                msgs.append({"role": "system", "content": hint})
        except Exception as e:
            print(f"[MEMORY-CTX] 人格记忆注入失败: {e}")

    return msgs


def inject_memory_into(messages: list, user_id: str = "", include: dict = None) -> list:
    """把记忆库注入到现有的 messages 列表中（插在 system prompt 之后、user 之前）。

    Args:
        messages: 现有的 messages 列表（第一个通常是 system prompt）
        user_id: 目标用户 QQ 号
        include: 传给 build_memory_messages 的 include 参数

    Returns:
        注入记忆后的 messages 列表
    """
    memory_msgs = build_memory_messages(user_id, include)
    if not memory_msgs:
        return messages
    # 插入到第一个 user 消息之前（保留已有的 system prompt 在最前）
    result = []
    inserted = False
    for m in messages:
        if not inserted and m.get("role") == "user":
            result.extend(memory_msgs)
            inserted = True
        result.append(m)
    if not inserted:
        # 没有 user 消息，追加到末尾
        result = messages + memory_msgs
    return result
