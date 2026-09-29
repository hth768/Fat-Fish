# -*- coding: utf-8 -*-
"""工具循环：把「模型 -> 工具调用 -> 回执 -> 再问模型」跑完一轮。

feiyu 的聊天主线（`chat_service`）不用工具；Cortico 的 World 是「环境 + 工具」，
必须有这条循环才能真正跑起来（Dungeon 的九件工具就是靠它使用的）。

屏障语义（对齐 Cortico ToolDef）：
- barrier_after: 该工具的回执读完才可继续；同一条输出中排在它后面的调用会被跳过
- ends_turn:     该工具完成后结束本次唤醒
"""
from __future__ import annotations

import json
from typing import Any, Callable, Dict, List, Optional

from .types import ToolCallContext, ToolDef, ToolOutcome


async def _invoke(tool: ToolDef, args: Dict[str, Any], ctx: ToolCallContext) -> ToolOutcome:
    if tool.handler is None:
        return ToolOutcome(text=f"工具 {tool.name} 没有处理函数", failed=True)
    out = tool.handler(args, ctx)
    if hasattr(out, "__await__"):
        out = await out
    return ToolOutcome.coerce(out)


def _parse_args(raw: str) -> Dict[str, Any]:
    if not raw:
        return {}
    try:
        v = json.loads(raw)
        return v if isinstance(v, dict) else {"value": v}
    except json.JSONDecodeError:
        return {}


async def run_tool_turn(messages: List[Dict[str, Any]],
                        tools: List[ToolDef],
                        max_rounds: int = 8,
                        role: str = "cortico",
                        on_tool_result: Optional[Callable[[str, ToolOutcome], None]] = None,
                        cancelled: Any = None) -> str:
    """跑一轮带工具的对话，返回模型最终给出的正文（没有则空串）。

    messages 会被就地追加（assistant / tool 消息），调用方负责初始上下文。
    """
    if not tools:
        return await _plain_chat(messages, role=role)

    from ai_provider import get_llm
    llm = get_llm()
    specs = [t.to_openai() for t in tools]
    by_name = {t.name: t for t in tools}
    final = ""

    for rd in range(max(1, int(max_rounds))):
        if cancelled is not None and getattr(cancelled, "is_set", lambda: False)():
            break
        msg = await _chat(llm, messages, specs, role=role)
        calls = msg.get("tool_calls") or []
        if not calls:
            final = str(msg.get("content") or "")
            break

        messages.append({
            "role": "assistant",
            "content": msg.get("content") or "",
            "tool_calls": calls,
        })

        barrier_hit = False
        ends = False
        for c in calls:
            fn = (c.get("function") or {}) if isinstance(c, dict) else {}
            name = str(fn.get("name") or "")
            call_id = str(c.get("id") or "")
            tool = by_name.get(name)
            if tool is None:
                messages.append({"role": "tool", "tool_call_id": call_id,
                                 "content": f"没有这个工具：{name}"})
                continue
            if barrier_hit:
                # 屏障之后的调用给「未执行」回执（与 Cortico 一致）
                messages.append({"role": "tool", "tool_call_id": call_id,
                                 "content": f"（{name} 未执行：前一步要求先读完回执）"})
                continue

            ctx = ToolCallContext(role=role, call_id=call_id, round=rd, cancelled=cancelled)
            try:
                out = await _invoke(tool, _parse_args(str(fn.get("arguments") or "{}")), ctx)
            except Exception as e:
                out = ToolOutcome(text=f"工具 {name} 执行失败：{e}", failed=True)

            messages.append({"role": "tool", "tool_call_id": call_id,
                             "content": out.render()})
            if on_tool_result is not None:
                try:
                    on_tool_result(name, out)
                except Exception:
                    pass
            if tool.barrier_after:
                barrier_hit = True
            if tool.ends_turn:
                ends = True
                break

        if ends:
            # 收尾再问一次，让模型用工具结果说句话
            msg = await _chat(llm, messages, [], role=role)
            final = str(msg.get("content") or "")
            break
    return final


async def _chat(llm, messages: List[Dict[str, Any]], specs: List[Dict[str, Any]],
                role: str) -> Dict[str, Any]:
    try:
        return await llm.chat(messages, capability="tools", tools=specs or None,
                              role=role, timeout=300)
    except TypeError:
        return await llm.chat_with_tools(messages, specs or [])


async def _plain_chat(messages: List[Dict[str, Any]], role: str) -> str:
    from ai_provider import get_llm
    llm = get_llm()
    msg = await llm.chat(messages, capability="chat", role=role, timeout=300)
    if isinstance(msg, dict):
        return str(msg.get("content") or "")
    return str(msg)
