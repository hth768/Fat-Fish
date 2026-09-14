# -*- coding: utf-8 -*-
"""AI 自我知识管理系统。

让 AI 能自己【查看】【学习】【更改】它积累的所有知识：
- 游戏技巧（mc_tips）
- 资源地图/探索记忆（mc_explored）
- 生存流程/配方（mc_survival）
- 地标（mc_explored.landmarks）
- 百科知识（knowledge_store）：联网搜索学到的通用知识，见 facts 类型

核心接口（供 agent 决策时调用）：
- view_knowledge(kind, keyword)     查看某类知识
- add_knowledge(kind, content)      学习/添加知识
- remove_knowledge(kind, keyword)   删除过时知识

kind 可选：tips（技巧）/ resources（资源地图）/ landmarks（地标）/ recipes（配方）/ stage（生存阶段）/ facts（百科知识）/ pvz（PvZ 技巧）
"""
from typing import Dict, List, Optional


def _valid_kind(kind: str) -> bool:
    return kind in ("tips", "resources", "landmarks", "recipes", "stage", "facts", "pvz")


def view_knowledge(kind: str, keyword: str = "") -> Dict:
    """查看某类知识。返回结果 dict。"""
    kind = kind.strip().lower()
    if not _valid_kind(kind):
        return {"ok": False, "error": f"未知知识类型:{kind}（可选 tips/resources/landmarks/recipes/stage/facts）"}

    try:
        if kind == "tips":
            from mc_tips import list_tips
            tips = list_tips()
            if not tips:
                return {"ok": True, "content": "技巧库是空的，可以学习新的游戏技巧"}
            if keyword:
                kw = keyword.lower()
                matched = [t for t in tips if kw in t.get("content", "").lower()]
                if not matched:
                    return {"ok": True, "content": f"没找到含「{keyword}」的技巧"}
                text = "\n".join(f"{i}. {t.get('content','')}" for i, t in enumerate(matched, 1))
            else:
                text = "\n".join(f"{i}. {t.get('content','')}（{'主人教的' if t.get('source')=='user' else '自己学的'}）"
                                 for i, t in enumerate(tips, 1))
            return {"ok": True, "content": f"我记住了 {len(tips)} 条技巧：\n{text}", "count": len(tips)}

        elif kind == "resources":
            from mc_explored import list_resources
            resources = list_resources()
            if not resources:
                return {"ok": True, "content": "资源地图是空的，探索时发现矿石/村庄会自动记录"}
            text = "\n".join(f"{r.get('name')} @X={r.get('x')} Z={r.get('z')}（{r.get('extra','')}）" for r in resources)
            return {"ok": True, "content": f"我记住了 {len(resources)} 个资源点：\n{text}", "count": len(resources)}

        elif kind == "landmarks":
            from mc_explored import list_landmarks
            lms = list_landmarks()
            if not lms:
                return {"ok": True, "content": "还没记住任何地标"}
            text = "\n".join(f"{lm.get('name')} @X={lm.get('x')} Y={lm.get('y')} Z={lm.get('z')}" for lm in lms)
            return {"ok": True, "content": f"我记住了 {len(lms)} 个地标：\n{text}", "count": len(lms)}

        elif kind == "recipes":
            from mc_survival import RECIPES
            if not RECIPES:
                return {"ok": True, "content": "还没有配方知识"}
            if keyword:
                kw = keyword.lower()
                matched = {k: v for k, v in RECIPES.items() if kw in k.lower()}
                if not matched:
                    return {"ok": True, "content": f"没找到含「{keyword}」的配方"}
                text = "\n".join(f"- {k}: {v}" for k, v in matched.items())
            else:
                text = "\n".join(f"- {k}: {v}" for k, v in RECIPES.items())
            return {"ok": True, "content": f"我掌握了 {len(RECIPES)} 个配方：\n{text}", "count": len(RECIPES)}

        elif kind == "stage":
            from mc_survival import get_survival_guidance, get_stage_progress_text
            parts = []
            g = get_survival_guidance()
            if g:
                parts.append(g)
            p = get_stage_progress_text()
            if p:
                parts.append(p)
            return {"ok": True, "content": "\n\n".join(parts) if parts else "暂无生存阶段信息"}

        elif kind == "facts":
            from knowledge_store import list_for_view
            text = list_for_view(keyword)
            if not text:
                return {"ok": True, "content": "知识库还是空的，我遇到不懂的问题会自己搜索并记下来"}
            return {"ok": True, "content": text}

        elif kind == "pvz":
            from pvz_tips import list_tips
            tips = list_tips()
            if not tips:
                return {"ok": True, "content": "PvZ 技巧库是空的（她玩几局就会自己总结了）"}
            if keyword:
                kw = keyword.lower()
                matched = [t for t in tips if kw in t.get("content", "").lower()]
                if not matched:
                    return {"ok": True, "content": f"没找到含「{keyword}」的 PvZ 技巧"}
                text = "\n".join(f"{i}. {t.get('content','')}" for i, t in enumerate(matched, 1))
            else:
                text = "\n".join(f"{i}. {t.get('content','')}（{'主人教的' if t.get('source')=='user' else '自己学的'}）"
                                 for i, t in enumerate(tips, 1))
            return {"ok": True, "content": f"我记住了 {len(tips)} 条 PvZ 技巧：\n{text}", "count": len(tips)}

    except Exception as e:
        return {"ok": False, "error": f"查看知识失败:{e}"}

    return {"ok": False, "error": "未知知识类型"}


def add_knowledge(kind: str, content: str) -> Dict:
    """学习/添加知识。返回结果 dict。"""
    kind = kind.strip().lower()
    content = content.strip()
    if not content:
        return {"ok": False, "error": "内容为空"}
    if not _valid_kind(kind):
        return {"ok": False, "error": f"未知知识类型:{kind}（目前只能添加 tips 技巧、landmarks 地标、facts 百科知识）"}

    try:
        if kind == "tips":
            from mc_tips import add_tip
            result = add_tip(content, source="learned")
            if result.get("ok"):
                return {"ok": True, "content": f"我学到了新技巧：{content}", "learned": True}
            else:
                return {"ok": False, "error": result.get("error", "添加失败")}

        elif kind == "pvz":
            from pvz_tips import add_tip as add_pvz_tip
            result = add_pvz_tip(content, source="learned")
            if result.get("ok"):
                return {"ok": True, "content": f"我学到了新的 PvZ 技巧：{content}", "learned": True}
            else:
                return {"ok": False, "error": result.get("error", "添加失败")}

        elif kind == "facts":
            from knowledge_store import add_fact
            result = add_fact(content)
            if result.get("ok"):
                return {"ok": True, "content": f"我记下了这条知识：{content}", "learned": True}
            else:
                return {"ok": False, "error": result.get("error", "记录失败")}

        elif kind == "landmarks":
            from mc_explored import add_landmark
            from mc_watcher import mc_watcher
            state = mc_watcher.fetch_state()
            p = (state or {}).get("player") or {}
            x, z = p.get("x"), p.get("z")
            if x is None or z is None:
                return {"ok": False, "error": "读不到当前坐标，无法记录地标"}
            result = add_landmark(content, x, p.get("y", 0), z, "landmark")
            if result.get("ok"):
                return {"ok": True, "content": f"我记下了地标：{content} @({x},{z})", "learned": True}
            else:
                return {"ok": False, "error": result.get("error", "记录失败")}

        return {"ok": False, "error": f"不能直接添加{kind}，建议用 tips 或 landmarks"}
    except Exception as e:
        return {"ok": False, "error": f"学习失败:{e}"}


def remove_knowledge(kind: str, keyword: str) -> Dict:
    """删除过时知识。返回结果 dict。"""
    kind = kind.strip().lower()
    keyword = keyword.strip()
    if not keyword:
        return {"ok": False, "error": "需要提供要删除的关键词"}
    if not _valid_kind(kind):
        return {"ok": False, "error": f"未知知识类型:{kind}"}

    try:
        if kind == "tips":
            from mc_tips import delete_tip
            result = delete_tip(keyword)
            if result.get("ok"):
                return {"ok": True, "content": f"删除了 {len(result.get('deleted', []))} 条含「{keyword}」的技巧"}
            else:
                return {"ok": False, "error": result.get("error", "删除失败")}

        elif kind == "landmarks":
            from mc_explored import delete_landmark
            result = delete_landmark(keyword)
            if result.get("ok"):
                return {"ok": True, "content": f"删除了 {len(result.get('deleted', []))} 个含「{keyword}」的地标"}
            else:
                return {"ok": False, "error": result.get("error", "删除失败")}

        elif kind == "facts":
            from knowledge_store import delete_by_keyword
            result = delete_by_keyword(keyword)
            if result.get("ok"):
                return {"ok": True, "content": f"删除了 {len(result.get('deleted', []))} 条含「{keyword}」的知识"}
            else:
                return {"ok": False, "error": result.get("error", "删除失败")}

        elif kind == "pvz":
            from pvz_tips import delete_tip as delete_pvz_tip
            result = delete_pvz_tip(keyword)
            if result.get("ok"):
                return {"ok": True, "content": f"删除了 {len(result.get('deleted', []))} 条含「{keyword}」的 PvZ 技巧"}
            else:
                return {"ok": False, "error": result.get("error", "删除失败")}

        return {"ok": False, "error": f"{kind} 暂不支持直接删除"}
    except Exception as e:
        return {"ok": False, "error": f"删除失败:{e}"}


# 知识类型的中文名（用于 AI 理解）
KIND_CN = {
    "tips": "游戏技巧", "resources": "资源地图", "landmarks": "地标",
    "recipes": "合成配方", "stage": "生存阶段", "facts": "百科知识",
    "pvz": "PvZ 技巧",
}
