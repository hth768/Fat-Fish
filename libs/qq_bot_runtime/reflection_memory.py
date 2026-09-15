# -*- coding: utf-8 -*-
"""反思记忆模块（参考 N.E.K.O. 猫娘计划的反思记忆设计）。

五维记忆系统的第四维：反思记忆（Reflection Memory）
- 工作记忆 -> Memory（短期上下文）
- 近期记忆 -> chat_history（全文历史）
- 事实记忆 -> long_term_memory（人物档案）
- 反思记忆 -> 本模块（自我反思、交互风格调整）
- 人格记忆 -> emotion + ai_profile（情绪+人设）

反思记忆做什么：
1. 对话后自动提炼「我为什么这样回复」「用户喜欢/不喜欢什么」
2. 记录交互偏好变化（如「用户不喜欢被叫某个称呼」「用户喜欢简短回复」）
3. 定期回顾调整交互风格（每 N 次对话触发一次反思）
4. 沉淀为可复用的交互规则，注入后续对话

数据文件：reflection_data.json（config.REFLECTION_FILE 可改路径）
"""
import json
import os
import time
from typing import Dict, List, Optional

import config
import file_lock

# 全局单例
_reflection_cache: Optional[dict] = None

# 反思触发间隔（每 N 次对话触发一次深度反思）
REFLECTION_INTERVAL = getattr(config, "REFLECTION_INTERVAL", 10)

# 反思记忆最大条数
MAX_REFLECTIONS = getattr(config, "MAX_REFLECTIONS", 100)

# 反思类型
REFLECTION_TYPE_PREFERENCE = "preference"      # 用户偏好（喜欢/不喜欢什么）
REFLECTION_TYPE_STYLE = "style"                # 交互风格（回复长度、语气等）
REFLECTION_TYPE_INSIGHT = "insight"            # 洞察（用户性格、习惯等）
REFLECTION_TYPE_CORRECTION = "correction"      # 纠错（我回复不当的地方）
REFLECTION_TYPE_PATTERN = "pattern"            # 模式（反复出现的对话模式）


def _base_dir():
    return os.path.dirname(os.path.abspath(__file__))


def _reflection_file():
    path = getattr(config, "REFLECTION_FILE", "") or "reflection_data.json"
    if not os.path.isabs(path):
        path = os.path.join(_base_dir(), path)
    return path


def _default_data() -> dict:
    return {
        "reflections": [],          # 反思条目列表
        "interaction_rules": [],    # 提炼出的交互规则
        "stats": {
            "total_conversations": 0,   # 总对话轮数
            "total_reflections": 0,     # 总反思次数
            "last_reflection_ts": 0,    # 上次反思时间
            "last_reflection_conv": 0,  # 上次反思时的对话轮数
        }
    }


def _load_data() -> dict:
    global _reflection_cache
    if _reflection_cache is not None:
        return _reflection_cache
    path = _reflection_file()
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                _reflection_cache = data
                _reflection_cache.setdefault("reflections", [])
                _reflection_cache.setdefault("interaction_rules", [])
                _reflection_cache.setdefault("stats", _default_data()["stats"])
                return _reflection_cache
        except (json.JSONDecodeError, OSError) as e:
            print(f"[REFLECT] 反思档案加载失败: {e}")
    _reflection_cache = _default_data()
    return _reflection_cache


def _save_data():
    path = _reflection_file()
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(_load_data(), f, ensure_ascii=False, indent=2)
    except OSError as e:
        print(f"[REFLECT] 反思档案保存失败: {e}")


def _add_reflection(reflection: dict):
    """添加一条反思记录。"""
    data = _load_data()
    reflections = data["reflections"]
    # 去重：相同类型+相同用户+相似内容不重复添加
    for existing in reflections:
        if (existing.get("type") == reflection.get("type") and
            existing.get("user_id") == reflection.get("user_id") and
            existing.get("content", "")[:50] == reflection.get("content", "")[:50]):
            return  # 已存在类似记录
    reflections.append(reflection)
    # 超限淘汰最旧的
    if len(reflections) > MAX_REFLECTIONS:
        data["reflections"] = reflections[-MAX_REFLECTIONS:]
    data["stats"]["total_reflections"] += 1
    data["stats"]["last_reflection_ts"] = time.time()
    _save_data()


def _add_interaction_rule(rule: dict):
    """添加一条交互规则（从反思中提炼的可复用规则）。"""
    data = _load_data()
    rules = data["interaction_rules"]
    # 去重
    for existing in rules:
        if existing.get("rule", "")[:50] == rule.get("rule", "")[:50]:
            # 更新置信度
            existing["confidence"] = min(existing.get("confidence", 0.5) + 0.1, 1.0)
            existing["last_seen"] = time.time()
            _save_data()
            return
    rule.setdefault("confidence", 0.5)
    rule.setdefault("created_at", time.time())
    rule.setdefault("last_seen", time.time())
    rules.append(rule)
    # 规则上限 50 条，按置信度淘汰
    if len(rules) > 50:
        rules.sort(key=lambda r: r.get("confidence", 0), reverse=True)
        data["interaction_rules"] = rules[:50]
    _save_data()


# ==================================================================
# 反思触发与执行
# ==================================================================

# 反思用的系统提示词
_REFLECTION_SYSTEM_PROMPT = """你是一个善于自我反思的 AI 助手。你需要根据最近的对话内容，提炼出有价值的反思。

主体区分（最关键）：本段对话有「用户」（人类）和「AI」（肥鱼娘）两方，二者不是同一人。
- preference / style / insight / pattern 这四类反思，主体必须是「用户」（人类）——
  写的是用户喜欢什么、习惯怎样、性格如何；不要写成 AI 的特征或喜好。
- correction 类反思的主体是「AI」（肥鱼娘自己回复得不对的地方），这是正常的，照常写。

反思类型：
1. preference（用户偏好）：用户喜欢/不喜欢什么回复方式、话题、称呼等
2. style（交互风格）：用户偏好的回复长度、语气、格式等
3. insight（洞察）：用户的性格特点、习惯、当前状态等
4. correction（纠错）：AI 回复不当的地方（如太啰嗦、语气不对、误解了用户）
5. pattern（模式）：反复出现的对话模式（如用户总是在深夜聊天、总是问某类问题）

输出格式（JSON 数组，每条反思一个对象）：
[
  {
    "type": "preference/style/insight/correction/pattern",
    "user_id": "用户ID",
    "content": "反思内容（一句话概括）",
    "evidence": "支撑这条反思的具体对话片段",
    "confidence": 0.0-1.0的置信度,
    "actionable": true/false（是否可据此调整后续行为）
  }
]

如果没有值得反思的内容，返回空数组 []。
只输出 JSON，不要其他文字。"""


async def reflect_on_conversation(user_id: str, recent_messages: List[dict]) -> List[dict]:
    """对一段对话进行反思，提炼出反思记录。

    Args:
        user_id: 用户 ID
        recent_messages: 最近的对话消息列表 [{"role": "user"/"assistant", "content": "..."}]

    Returns:
        提炼出的反思记录列表
    """
    if not getattr(config, "ENABLE_REFLECTION", True):
        return []
    if not recent_messages or len(recent_messages) < 4:
        return []  # 对话太短，不反思

    try:
        from ai_provider import get_llm
        llm = get_llm()

        # 构建反思输入
        conversation_text = "\n".join(
            f"{'用户' if m['role'] == 'user' else 'AI'}: {m.get('content', '')[:200]}"
            for m in recent_messages[-20:]  # 只看最近 20 条
        )

        messages = [
            {"role": "system", "content": _REFLECTION_SYSTEM_PROMPT},
            {"role": "user", "content": f"用户 ID: {user_id}\n\n最近对话：\n{conversation_text}\n\n请反思这段对话，输出 JSON 数组："}
        ]

        response = await llm.chat(
            messages, capability="chat", role="reflection",
            model=getattr(config, "DEEPSEEK_MODEL", None), think=False,
        )
        response = response.strip()

        # 解析 JSON
        if response.startswith("```"):
            response = response.split("```")[1]
            if response.startswith("json"):
                response = response[4:]
        reflections = json.loads(response)

        if not isinstance(reflections, list):
            return []

        # 写入反思档案
        now = time.time()
        for r in reflections:
            r["user_id"] = str(user_id)
            r["created_at"] = now
            _add_reflection(r)
            # 可执行的反思提炼成交互规则
            if r.get("actionable") and r.get("confidence", 0) >= 0.6:
                _add_interaction_rule({
                    "rule": r.get("content", ""),
                    "type": r.get("type", "insight"),
                    "user_id": str(user_id),
                    "evidence": r.get("evidence", ""),
                })

        print(f"[REFLECT] 用户 {user_id} 对话反思完成，提炼 {len(reflections)} 条")
        return reflections

    except Exception as e:
        print(f"[REFLECT] 反思失败: {e}")
        return []


def increment_conversation_count(user_id: str = ""):
    """增加对话计数，用于判断是否触发反思。"""
    data = _load_data()
    data["stats"]["total_conversations"] += 1
    data["stats"]["last_reflection_ts"] = time.time()
    _save_data()


def should_reflect_now() -> bool:
    """判断是否应该触发一次反思（每 N 次对话触发一次）。"""
    data = _load_data()
    stats = data["stats"]
    conv_count = stats.get("total_conversations", 0)
    last_reflection_conv = stats.get("last_reflection_conv", 0)
    return (conv_count - last_reflection_conv) >= REFLECTION_INTERVAL


def mark_reflection_done():
    """标记本次反思已完成。"""
    data = _load_data()
    data["stats"]["last_reflection_conv"] = data["stats"]["total_conversations"]
    data["stats"]["last_reflection_ts"] = time.time()
    _save_data()


# ==================================================================
# 反思注入（供聊天大脑调用）
# ==================================================================

def build_reflection_hint(user_id: str) -> str:
    """构建反思记忆提示，注入到对话 system 中。

    返回该用户相关的交互规则和反思洞察，帮助 AI 调整回复风格。
    """
    data = _load_data()
    uid = str(user_id)

    # 收集该用户相关的交互规则
    user_rules = [
        r for r in data.get("interaction_rules", [])
        if r.get("user_id") == uid or r.get("user_id") == ""
    ]
    # 按置信度排序，取前 5 条
    user_rules.sort(key=lambda r: r.get("confidence", 0), reverse=True)
    user_rules = user_rules[:5]

    # 收集该用户相关的反思（最近 3 条）
    user_reflections = [
        r for r in data.get("reflections", [])
        if r.get("user_id") == uid
    ]
    user_reflections.sort(key=lambda r: r.get("created_at", 0), reverse=True)
    user_reflections = user_reflections[:3]

    if not user_rules and not user_reflections:
        return ""

    lines = ["【反思记忆（交互风格调整参考）】"]

    if user_rules:
        lines.append("交互规则：")
        for r in user_rules:
            conf = r.get("confidence", 0.5)
            conf_tag = "高" if conf >= 0.7 else "中" if conf >= 0.5 else "低"
            lines.append(f"  - [{conf_tag}置信] {r.get('rule', '')}")

    if user_reflections:
        lines.append("近期反思：")
        for r in user_reflections:
            rtype = r.get("type", "insight")
            lines.append(f"  - [{rtype}] {r.get('content', '')}")

    return "\n".join(lines)


def get_reflection_stats() -> dict:
    """获取反思统计信息（供 /反思 命令使用）。"""
    data = _load_data()
    stats = data["stats"]
    return {
        "total_conversations": stats.get("total_conversations", 0),
        "total_reflections": stats.get("total_reflections", 0),
        "interaction_rules_count": len(data.get("interaction_rules", [])),
        "last_reflection_ts": stats.get("last_reflection_ts", 0),
    }


def clear_reflections(user_id: str = ""):
    """清空反思记录（/反思 清 命令使用）。"""
    data = _load_data()
    if user_id:
        uid = str(user_id)
        data["reflections"] = [r for r in data["reflections"] if r.get("user_id") != uid]
        data["interaction_rules"] = [r for r in data["interaction_rules"] if r.get("user_id") != uid]
    else:
        data["reflections"] = []
        data["interaction_rules"] = []
    _save_data()


def clear_interaction_rules(user_id: str = ""):
    """清空交互规则（记忆浏览器整类清空使用）。"""
    data = _load_data()
    if user_id:
        uid = str(user_id)
        data["interaction_rules"] = [r for r in data["interaction_rules"] if r.get("user_id") != uid]
    else:
        data["interaction_rules"] = []
    _save_data()


def get_reflections():
    return list(_load_data().get('reflections', []))

def get_interaction_rules():
    return list(_load_data().get('interaction_rules', []))


def delete_reflection(ts=None, content=None):
    data = _load_data()
    before = len(data['reflections'])
    if ts is not None:
        try:
            ts = float(ts)
        except (TypeError, ValueError):
            return 0
        data['reflections'] = [r for r in data['reflections'] if abs((r.get('ts') or 0) - ts) > 1e-6]
    elif content:
        data['reflections'] = [r for r in data['reflections'] if r.get('content', '') != content]
    removed = before - len(data['reflections'])
    if removed:
        _save_data()
    return removed

def delete_interaction_rule(rule=None):
    if not rule:
        return 0
    data = _load_data()
    before = len(data['interaction_rules'])
    data['interaction_rules'] = [r for r in data['interaction_rules'] if r.get('rule', '') != rule]
    removed = before - len(data['interaction_rules'])
    if removed:
        _save_data()
    return removed
