# -*- coding: utf-8 -*-
"""通用知识库：AI 遇到知识盲区时自行搜索，把学到的新知识沉淀在这里。

与 self_knowledge.py 的分工：这里存「联网学到的通用知识」（facts 类型），
游戏领域的知识仍在 mc_tips / mc_explored / mc_survival。

闭环：
- 沉淀（学）：联网搜索结束后，chat_service 用一次廉价 LLM 调用提炼通用知识，
  调 add_entry() 写入（带学习日期、来源、关键词），自动去重、限制总量
- 复用（用）：回答问题前调 build_recall_context()，命中就直接用记录回答，
  省一次搜索；记录带学习日期，提示 AI 注意时效

存储：JSON 文件（config.KNOWLEDGE_FILE），threading.Lock 防并发写坏。
"""
import json
import os
import re
import threading
import time
import uuid
from typing import Dict, List, Optional

import config
import agent_ctx

# 文件锁，避免并发写入
_lock = threading.Lock()


def _kb_path() -> str:
    """知识库文件路径（按当前智能体命名空间隔离；默认 bot 回落引擎目录，保持兼容）。"""
    p = getattr(config, "KNOWLEDGE_FILE", "")
    if not p:
        base = agent_ctx.agent_storage_dir(os.path.dirname(os.path.abspath(__file__)))
        p = os.path.join(base, "knowledge_base.json")
    return p


def _load() -> List[Dict]:
    path = _kb_path()
    try:
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    return data
    except Exception as e:
        print(f"[KB] 知识库加载失败: {e}")
    return []


def _save(entries: List[Dict]):
    path = _kb_path()
    try:
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(entries, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[KB] 知识库保存失败: {e}")


def _norm(text: str) -> str:
    """归一化：小写、去空白。用于主题合并与事实去重。"""
    return re.sub(r"\s+", "", (text or "")).lower()


def _entry_blob(e: Dict) -> str:
    """条目的关键词串（主题 + keywords），供检索匹配。"""
    return " ".join([e.get("topic", "")] + e.get("keywords", [])).lower()


def _entry_matches(e: Dict, keyword: str) -> bool:
    """判断条目是否含某关键词（查主题 / keywords / 事实内容）。"""
    kw = (keyword or "").strip().lower()
    if not kw:
        return False
    if kw in e.get("topic", "").lower():
        return True
    if any(kw in k.lower() for k in e.get("keywords", [])):
        return True
    return any(kw in f.lower() for f in e.get("facts", []))


def _query_tokens(query: str) -> List[str]:
    """提取问题里的英文/数字词（>=2 字符）。"""
    return [t for t in re.findall(r"[a-z0-9][a-z0-9.\-_/#]*", (query or "").lower()) if len(t) >= 2]


# ---------------- 学习（写） ----------------
def add_entry(topic: str, facts: List[str], keywords: List[str] = None,
              sources: List[str] = None) -> Dict:
    """添加/合并一条知识。同主题合并、事实全库去重，超出总量上限淘汰最旧条目。

    返回 {"ok": True, "topic": ..., "new_facts": 新增事实数, "total": 总条目数}。
    """
    topic = (topic or "").strip()
    facts = [f.strip() for f in (facts or []) if f and f.strip()]
    keywords = [k.strip() for k in (keywords or []) if k and k.strip()]
    sources = [s.strip() for s in (sources or []) if s and s.strip()]
    if not topic or not facts:
        return {"ok": False, "error": "主题或事实为空"}

    now = time.strftime("%Y-%m-%d %H:%M:%S")
    with _lock:
        entries = _load()
        # 事实级全库去重：完全相同的事实（忽略大小写/空白）不重复记
        known_facts = {_norm(f) for e in entries for f in e.get("facts", [])}
        new_facts = [f for f in facts if _norm(f) not in known_facts]

        # 同主题（归一化后相同）并入已有条目，否则新建
        target = next((e for e in entries if _norm(e.get("topic")) == _norm(topic)), None)
        merged = target is not None
        if target is None:
            target = {
                "id": str(uuid.uuid4())[:8],
                "topic": topic,
                "keywords": [],
                "facts": [],
                "sources": [],
                "learned_at": time.strftime("%Y-%m-%d"),
                "updated": now,
                "hits": 0,
            }
            entries.append(target)

        if new_facts:
            target["facts"] = (target.get("facts", []) + new_facts)[-20:]
            target["updated"] = now
        # 关键词/来源去重合并（关键词过多会稀释检索精度，设上限）
        exist_kw = {k.lower() for k in target.get("keywords", [])}
        for k in keywords:
            if k.lower() not in exist_kw:
                target.setdefault("keywords", []).append(k)
                exist_kw.add(k.lower())
        target["keywords"] = target.get("keywords", [])[:12]
        exist_src = set(target.get("sources", []))
        for s in sources:
            if s not in exist_src:
                target.setdefault("sources", []).append(s)
                exist_src.add(s)
        target["sources"] = target.get("sources", [])[:8]

        # 总量上限：超出时淘汰最旧（按 updated）的条目
        max_entries = int(getattr(config, "KNOWLEDGE_MAX_ENTRIES", 200) or 200)
        if len(entries) > max_entries:
            entries.sort(key=lambda e: e.get("updated", ""))
            entries = entries[-max_entries:]
        _save(entries)
    return {"ok": True, "topic": topic, "new_facts": len(new_facts),
            "merged": merged, "total": len(entries)}


def add_fact(content: str) -> Dict:
    """把单条知识文本记入库（供 self_knowledge「教知识」入口，无 LLM 参与）。

    主题取内容前 30 字，关键词取内容里的英文/数字词；已有完全相同的事实则拒绝。
    """
    content = (content or "").strip()
    if not content:
        return {"ok": False, "error": "内容为空"}
    with _lock:
        for e in _load():
            if any(_norm(f) == _norm(content) for f in e.get("facts", [])):
                return {"ok": False, "error": "这条知识已经记过了"}
    tokens = sorted(set(re.findall(r"[A-Za-z0-9][A-Za-z0-9.\-_/#]*", content)))[:6]
    return add_entry(content[:30].strip(), [content], tokens, [])


# ---------------- 复用（读） ----------------
def search_entries(query: str, limit: int = 2) -> List[Dict]:
    """按问题查知识库，返回最相关的最多 limit 条（附 _score 降序）。

    匹配规则（满足其一即命中）：
    - 条目的主题/关键词（词长>=3，避免「版本」「数据库」这类宽泛词误命中）出现在问题里
    - 问题里所有英文/数字词（>=2 字符）都出现在条目的关键词串里
    """
    query = (query or "").strip().lower()
    if not query:
        return []
    q_tokens = _query_tokens(query)
    scored = []
    with _lock:
        entries = _load()
    for e in entries:
        topic = e.get("topic", "").strip().lower()
        score = 0
        for kw in [e.get("topic", "")] + e.get("keywords", []):
            kw = kw.strip().lower()
            if len(kw) >= 3 and kw in query:
                score = max(score, 3 if kw == topic else 2)
        if score == 0 and q_tokens and all(t in _entry_blob(e) for t in q_tokens):
            score = 1
        if score > 0:
            e = dict(e)
            e["_score"] = score
            scored.append(e)
    scored.sort(key=lambda x: (-x["_score"], -x.get("hits", 0)))
    return scored[:limit]


def mark_used(entry_ids: List[str]):
    """召回命中后累加 hits（用于统计和淘汰参考）。"""
    try:
        with _lock:
            entries = _load()
            ids = set(i for i in entry_ids if i)
            changed = False
            for e in entries:
                if e.get("id") in ids:
                    e["hits"] = e.get("hits", 0) + 1
                    changed = True
            if changed:
                _save(entries)
    except Exception as e:
        print(f"[KB] 命中统计失败: {e}")


def build_recall_context(query: str, limit: int = 2) -> str:
    """生成注入给 AI 的知识库上下文。无命中返回空串。"""
    hits = search_entries(query, limit)
    if not hits:
        return ""
    mark_used([e.get("id", "") for e in hits])
    parts = []
    for e in hits:
        lines = "\n".join(f"{i}. {f}" for i, f in enumerate(e.get("facts", []), 1))
        parts.append(f"◆ {e.get('topic')}（学习日期 {e.get('learned_at', '?')}）\n{lines}")
    return (
        "【你的知识库命中】下面是你以前联网搜索后自己记录的知识，可以优先据此回答：\n"
        + "\n".join(parts)
        + "\n（这些记录可能过时：与问题无关的部分忽略；如果用户问的是最新动态，"
        "请注明这是你某天记录的内容；用户坚持要最新信息时，让他用「/搜索 问题」强制重新联网查证。）"
    )


# ---------------- 查看 / 删除（self_knowledge 接口用） ----------------
def list_for_view(keyword: str = "") -> str:
    """知识库概览文本。无内容返回空串。"""
    all_entries = _load()
    kw = (keyword or "").strip().lower()
    if kw:
        entries = [e for e in all_entries if _entry_matches(e, kw)]
        if not entries:
            return f"没找到含「{keyword}」的知识"
    else:
        entries = all_entries
    if not entries:
        return ""
    lines = [f"知识库共 {len(all_entries)} 个主题："]
    for i, e in enumerate(entries, 1):
        lines.append(f"{i}. {e.get('topic')}（{len(e.get('facts', []))} 条事实，学于 {e.get('learned_at', '?')}）")
        for f in e.get("facts", [])[:5]:
            lines.append(f"   - {f}")
    return "\n".join(lines)


def delete_by_keyword(keyword: str) -> Dict:
    """按关键词删除知识（匹配主题/关键词/事实，整条删除）。"""
    keyword = (keyword or "").strip()
    if not keyword:
        return {"ok": False, "error": "需要提供要删除的关键词"}
    with _lock:
        entries = _load()
        keep, deleted = [], []
        for e in entries:
            if _entry_matches(e, keyword):
                deleted.append(e.get("topic", ""))
            else:
                keep.append(e)
        if not deleted:
            return {"ok": False, "error": f"没有找到含「{keyword}」的知识"}
        _save(keep)
    return {"ok": True, "deleted": deleted}


def count() -> int:
    """知识主题总数。"""
    return len(_load())


# ---------------- 搜索结果提炼（chat_service 调用） ----------------
def parse_distill_output(text: str) -> Optional[Dict]:
    """解析 LLM 提炼搜索知识的输出。无值得记录的内容返回 None。

    期望输出为严格 JSON：{"topic", "keywords", "facts", "sources"}，
    或一个「无」字。容错：从文本里抠出第一个 {...} 再解析。
    """
    text = (text or "").strip()
    if not text or text.startswith("无"):
        return None
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    topic = str(data.get("topic", "")).strip()
    facts = [str(f).strip() for f in (data.get("facts") or []) if str(f).strip()]
    if not topic or not facts:
        return None
    return {
        "topic": topic[:60],
        "keywords": [str(k).strip() for k in (data.get("keywords") or []) if str(k).strip()][:8],
        "facts": [f[:500] for f in facts][:8],
        "sources": [str(s).strip() for s in (data.get("sources") or []) if str(s).strip()][:8],
    }
