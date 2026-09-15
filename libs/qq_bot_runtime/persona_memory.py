# -*- coding: utf-8 -*-
"""人格记忆模块（参考 N.E.K.O. 猫娘计划的人格记忆设计）。

五维记忆系统的第五维：人格记忆（Persona Memory）
- 工作记忆 -> Memory（短期上下文）
- 近期记忆 -> chat_history（全文历史）
- 事实记忆 -> long_term_memory（人物档案）
- 反思记忆 -> reflection_memory（自我反思）
- 人格记忆 -> 本模块（与每个用户的独特相处模式）

人格记忆做什么：
1. 记录与每个用户的独特相处模式（如"对 alice 更温柔"、"对 bob 更爱吐槽"）
2. 从反思记忆中提炼人格演化规则
3. 动态调整对不同用户的回复风格
4. 维持角色设定的同时展现个性化

数据文件：persona_data.json（config.PERSONA_FILE 可改路径）
"""
import json
import os
import time
from typing import Dict, List, Optional

import config
import file_lock

# 全局单例
_persona_cache: Optional[dict] = None

# 人格记忆最大条数（每个用户）
MAX_PERSONA_PER_USER = getattr(config, "MAX_PERSONA_PER_USER", 20)


def _base_dir():
    return os.path.dirname(os.path.abspath(__file__))


def _persona_file():
    path = getattr(config, "PERSONA_FILE", "") or "persona_data.json"
    if not os.path.isabs(path):
        path = os.path.join(_base_dir(), path)
    return path


def _default_data() -> dict:
    return {
        "personas": {},  # {user_id: {"traits": [], "style": "", "updated": timestamp}}
        "global_style": "",  # 全局风格（可选）
    }


def _load_data() -> dict:
    global _persona_cache
    if _persona_cache is not None:
        return _persona_cache
    path = _persona_file()
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                _persona_cache = data
                _persona_cache.setdefault("personas", {})
                _persona_cache.setdefault("global_style", "")
                return _persona_cache
        except (json.JSONDecodeError, OSError) as e:
            print(f"[PERSONA] 人格档案加载失败: {e}")
    _persona_cache = _default_data()
    return _persona_cache


def _save_data():
    path = _persona_file()
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(_load_data(), f, ensure_ascii=False, indent=2)
    except OSError as e:
        print(f"[PERSONA] 人格档案保存失败: {e}")


def update_persona(user_id: str, traits: List[str], style: str = ""):
    """更新某用户的人格记忆。

    Args:
        user_id: 用户 ID
        traits: 与该用户的相处模式特征列表（如["更温柔", "爱开玩笑", "喜欢聊游戏"]）
        style: 整体风格描述（可选）
    """
    data = _load_data()
    uid = str(user_id)

    if uid not in data["personas"]:
        data["personas"][uid] = {"traits": [], "style": "", "updated": 0}

    persona = data["personas"][uid]

    # 合并特征（去重）
    existing_traits = set(persona.get("traits", []))
    for trait in traits:
        trait = trait.strip()
        if trait and trait not in existing_traits:
            existing_traits.add(trait)

    # 限制特征数量
    traits_list = list(existing_traits)
    if len(traits_list) > MAX_PERSONA_PER_USER:
        traits_list = traits_list[-MAX_PERSONA_PER_USER:]

    persona["traits"] = traits_list
    if style:
        persona["style"] = style.strip()
    persona["updated"] = time.time()

    _save_data()
    print(f"[PERSONA] 用户 {uid} 人格记忆更新：{len(traits_list)} 个特征")


def get_persona(user_id: str) -> dict:
    """获取某用户的人格记忆。"""
    data = _load_data()
    uid = str(user_id)
    return data["personas"].get(uid, {"traits": [], "style": "", "updated": 0})


def build_persona_hint(user_id: str) -> str:
    """构建人格记忆提示，注入到对话 system 中。

    返回与该用户的独特相处模式，帮助 AI 调整回复风格。
    """
    data = _load_data()
    uid = str(user_id)
    persona = data["personas"].get(uid)

    if not persona:
        return ""

    traits = persona.get("traits", [])
    style = persona.get("style", "")

    if not traits and not style:
        return ""

    lines = ["【人格记忆（与该用户的独特相处模式）】"]

    if style:
        lines.append(f"整体风格：{style}")

    if traits:
        lines.append("相处特征：")
        for trait in traits[:10]:  # 最多显示 10 个
            lines.append(f"  - {trait}")

    return "\n".join(lines)


async def extract_persona_from_reflection(user_id: str, reflections: List[dict]) -> bool:
    """从反思记忆中提取人格相关规则，更新人格记忆。

    写记忆前先与既有人格记忆比对：旧事实（已确认）优先，若新增特征与任一旧事实
    矛盾，则否定（丢弃）该新增特征，绝不覆盖已确认的事实。

    Args:
        user_id: 用户 ID
        reflections: 反思记录列表

    Returns:
        是否成功更新
    """
    if not reflections:
        return False

    # 提取与交互风格相关的反思
    style_traits = []
    for r in reflections:
        rtype = r.get("type", "")
        content = r.get("content", "")

        # 只关注 style 和 preference 类型的反思
        if rtype in ("style", "preference") and content:
            # 简化特征描述
            trait = content[:50]  # 限制长度
            if trait:
                style_traits.append(trait)

    if style_traits:
        # 与既有人格记忆比对：旧事实优先，冲突的新增特征被否定
        accepted = await reconcile_persona_traits(user_id, style_traits)
        if accepted:
            update_persona(user_id, accepted)
            return True
        return False

    return False


async def reconcile_persona_traits(user_id: str, candidates: List[str]) -> List[str]:
    """人格记忆冲突裁决：旧事实（已确认）优先，新增若与旧事实矛盾则否定（丢弃）。

    与 merge_profile_facts（用户档案：新覆盖旧）相反，人格记忆采用「旧优先」策略——
    已确认的人机相处模式不应被一次新的反思随意推翻。

    Args:
        user_id: 用户 ID
        candidates: 本轮想新增的特征列表
    Returns:
        经裁决后保留（不矛盾）的新增特征列表；若裁决失败则回退为全部保留。
    """
    existing = get_persona(user_id).get("traits", [])
    if not existing or not candidates:
        return list(candidates)

    try:
        from ai_provider import get_llm
        llm = get_llm()
        existing_text = "\n".join(f"- {t}" for t in existing)
        candidate_text = "\n".join(f"- {t}" for t in candidates)
        prompt = (
            "下面是一份已经确认的人格记忆（关于 AI 如何与某用户相处，属旧事实，不可推翻）：\n"
            f"{existing_text}\n\n"
            "下面是新提取、想补充的特征：\n"
            f"{candidate_text}\n\n"
            "请逐条判断每个【新特征】是否与【旧事实】矛盾（冲突、相反、互斥、无法共存）。\n"
            "裁决规则：旧事实优先——只要与任一条旧事实矛盾，该【新特征】判定为「reject」"
            "（否定、不采纳）；其余不矛盾的新特征判定为「keep」。\n"
            "只输出一个 JSON 数组，每个元素为 "
            '{"trait": 新特征原文, "verdict": "keep" 或 "reject", "reason": 简短理由}，'
            "不要输出其他任何文字。"
        )
        resp = await llm.chat([{"role": "user", "content": prompt}],
                              capability="chat", role="persona_reconcile", think=False)
        resp = resp.strip()
        if resp.startswith("```"):
            resp = resp.split("```")[1]
            if resp.startswith("json"):
                resp = resp[4:]
        decisions = json.loads(resp)
        kept = []
        for d in decisions:
            trait = (d.get("trait") or "").strip()
            if d.get("verdict") == "keep" and trait:
                kept.append(trait)
            else:
                print(f"[PERSONA] 因与既有事实冲突，已否定新特征：{trait}（{d.get('reason','')}）")
        return kept
    except Exception as e:
        print(f"[PERSONA] 人格冲突裁决失败，回退为直接追加：{e}")
        return list(candidates)


def clear_persona(user_id: str = ""):
    """清空人格记忆（/人格 清 命令使用）。"""
    data = _load_data()
    if user_id:
        uid = str(user_id)
        if uid in data["personas"]:
            del data["personas"][uid]
    else:
        data["personas"] = {}
    _save_data()


def delete_persona(user_id: str):
    """删除指定用户的人格记忆（记忆浏览器单条删除使用）。"""
    data = _load_data()
    uid = str(user_id)
    if uid in data["personas"]:
        del data["personas"][uid]
        _save_data()
        return 1
    return 0


def get_persona_stats() -> dict:
    """获取人格记忆统计信息。"""
    data = _load_data()
    personas = data.get("personas", {})
    return {
        "user_count": len(personas),
        "total_traits": sum(len(p.get("traits", [])) for p in personas.values()),
    }


def list_personas() -> str:
    """列出所有用户的人格记忆（供 /人格 命令使用）。"""
    data = _load_data()
    personas = data.get("personas", {})

    if not personas:
        return "还没有人格记忆记录呢。"

    lines = [f"【人格记忆列表】共 {len(personas)} 个用户"]

    for uid, persona in list(personas.items())[:10]:  # 最多显示 10 个
        traits = persona.get("traits", [])
        trait_count = len(traits)
        lines.append(f"用户 {uid}：{trait_count} 个特征")
        if traits:
            lines.append(f"  特征：{', '.join(traits[:5])}")

    return "\n".join(lines)


def get_all_personas():
    return dict(_load_data().get('personas', {}))
