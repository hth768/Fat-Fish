# -*- coding: utf-8 -*-
"""记忆中心 API：人物档案 / 重要事项 / 人格记忆 / 反思记忆 / 全文历史 / 知识库 / 会话记忆。

全部通过 qq_bot 既有模块的公开函数读写（数据文件在 qq_bot 内，本应用不改任何源码）。
"""
import json
import os
import time

_MEMORY_FILE = "memory_data.json"


def _read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


# ----------------------------------------------------------------------
# 用户列表（跨记忆维度聚合）
# ----------------------------------------------------------------------
def list_users() -> list:
    ids = {}

    def add(uid, source):
        uid = str(uid)
        if not uid or uid == "None":
            return
        ids.setdefault(uid, set()).add(source)

    try:
        import long_term_memory
        for uid in (long_term_memory.load_profiles() or {}):
            add(uid, "档案")
        hdir = long_term_memory._history_dir()
        if os.path.isdir(hdir):
            for fn in os.listdir(hdir):
                if fn.endswith(".jsonl"):
                    add(os.path.splitext(fn)[0], "历史")
    except Exception:
        pass
    try:
        import important_notes
        for uid in (important_notes.load_notes() or {}):
            add(uid, "事项")
    except Exception:
        pass
    try:
        import persona_memory
        for uid in ((persona_memory._load_data() or {}).get("personas") or {}):
            add(uid, "人格")
    except Exception:
        pass
    try:
        import reflection_memory
        for r in ((reflection_memory._load_data() or {}).get("reflections") or []):
            add(r.get("user_id"), "反思")
    except Exception:
        pass

    try:
        import config as qq_config
        overrides = dict(getattr(qq_config, "NAME_OVERRIDES", {}) or {})
    except Exception:
        overrides = {}

    out = []
    for uid, sources in ids.items():
        out.append({"id": uid, "name": overrides.get(uid, ""), "sources": sorted(sources)})
    out.sort(key=lambda u: (u["id"] != "app_owner", u["id"]))
    return out


# ----------------------------------------------------------------------
# 各维度读取
# ----------------------------------------------------------------------
def get_profiles(uid=""):
    import long_term_memory
    profiles = long_term_memory.load_profiles() or {}
    if uid:
        profiles = {uid: profiles.get(uid, {"facts": [], "updated": 0})}
    return {"items": profiles}


def get_notes(uid="", q=""):
    import important_notes
    notes = important_notes.load_notes() or {}
    if uid:
        notes = {uid: notes.get(uid, [])}
    if q:
        ql = q.lower()
        notes = {u: [n for n in lst if ql in json.dumps(n, ensure_ascii=False).lower()]
                 for u, lst in notes.items()}
        notes = {u: lst for u, lst in notes.items() if lst}
    return {"items": notes}


def get_persona(uid=""):
    import persona_memory
    data = persona_memory._load_data() or {}
    personas = dict(data.get("personas") or {})
    if uid:
        personas = {uid: personas.get(uid, {})}
    return {"items": personas, "global_style": data.get("global_style", "")}


def get_reflection(uid="", q=""):
    import reflection_memory
    data = reflection_memory._load_data() or {}
    reflections = list(data.get("reflections") or [])
    if uid:
        reflections = [r for r in reflections if str(r.get("user_id", "")) == uid]
    if q:
        ql = q.lower()
        reflections = [r for r in reflections if ql in json.dumps(r, ensure_ascii=False).lower()]
    return {"items": reflections[-200:],
            "rules": list(data.get("interaction_rules") or [])[-50:],
            "stats": data.get("stats", {})}


def get_knowledge(q="", limit=100):
    try:
        import knowledge_store as ks
        total = ks.count()
        if q:
            return {"items": ks.search_entries(q, limit=limit), "count": total, "searched": True}
        return {"items": (ks._load() or [])[-limit:], "count": total}
    except Exception as e:
        return {"items": [], "error": repr(e)}


def get_history(uid="", q="", limit=100):
    if not uid:
        return {"items": [], "hint": "请先选择用户"}
    import long_term_memory
    if q:
        items = long_term_memory.search_history(uid, q, limit=limit)
    else:
        items = long_term_memory.get_user_history(uid, limit=limit)
    return {"items": items}


def get_sessions():
    data = _read_json(os.path.join(os.getcwd(), _MEMORY_FILE), {}) or {}
    store = data.get("store") or {}
    summary = data.get("summary") or {}
    topic = data.get("topic") or {}
    items = []
    keys = set(store) | set(summary)
    for key in keys:
        turns = list(store.get(key) or [])
        items.append({"key": key, "turns": len(turns), "summary": summary.get(key, ""),
                      "topic": topic.get(key, ""), "recent": turns[-6:]})
    items.sort(key=lambda i: (i["key"] != "app_owner", i["key"]))
    return {"items": items[:80]}


def memory_stats():
    out = {"profiles": 0, "notes": 0, "persona": 0, "reflection": 0,
           "knowledge": 0, "history_users": 0, "facts": 0}
    try:
        import long_term_memory as ltm
        profiles = ltm.load_profiles() or {}
        out["profiles"] = len(profiles)
        out["facts"] = sum(len((p or {}).get("facts", [])) for p in profiles.values())
        hdir = ltm._history_dir()
        out["history_users"] = (len([f for f in os.listdir(hdir) if f.endswith(".jsonl")])
                                if os.path.isdir(hdir) else 0)
    except Exception:
        pass
    try:
        import important_notes
        out["notes"] = sum(len(v or []) for v in (important_notes.load_notes() or {}).values())
    except Exception:
        pass
    try:
        import persona_memory
        out["persona"] = len((persona_memory._load_data() or {}).get("personas") or {})
    except Exception:
        pass
    try:
        import reflection_memory
        out["reflection"] = len((reflection_memory._load_data() or {}).get("reflections") or [])
    except Exception:
        pass
    try:
        import knowledge_store
        out["knowledge"] = knowledge_store.count()
    except Exception:
        pass
    return out


# ----------------------------------------------------------------------
# 写操作
# ----------------------------------------------------------------------
def memory_action(kind, body):
    op = body.get("op", "")
    uid = str(body.get("uid", "") or "")
    if kind == "profiles":
        import long_term_memory as ltm
        if op == "save":
            facts = [str(f).strip() for f in (body.get("facts") or []) if str(f).strip()]
            profiles = ltm.load_profiles() or {}
            entry = profiles.get(uid) or {}
            entry["facts"] = facts
            entry["updated"] = time.time()
            profiles[uid] = entry
            ltm.save_profiles(profiles)
            return {"ok": True}
        if op == "delete":
            profiles = ltm.load_profiles() or {}
            profiles.pop(uid, None)
            ltm.save_profiles(profiles)
            return {"ok": True}

    if kind == "notes":
        import important_notes as notes_mod
        if op == "add":
            ok = notes_mod.add_note(uid, body.get("text", ""), body.get("category", ""))
            return {"ok": bool(ok)}
        if op == "delete":
            n = notes_mod.delete_note(uid, index=body.get("index"), keyword=body.get("keyword"))
            return {"ok": n > 0, "removed": n}
        if op == "clear":
            n = notes_mod.clear_notes(uid)
            return {"ok": True, "removed": n}

    if kind == "persona" and op == "clear":
        import persona_memory
        n = persona_memory.clear_persona(uid)
        return {"ok": True, "removed": n}

    if kind == "reflection" and op == "clear":
        import reflection_memory
        n = reflection_memory.clear_reflections(uid)
        return {"ok": True, "removed": n}

    if kind == "knowledge":
        import knowledge_store as ks
        if op == "add":
            topic = str(body.get("topic", "")).strip()
            facts = [str(f).strip() for f in (body.get("facts") or []) if str(f).strip()]
            if not topic or not facts:
                return {"ok": False, "error": "主题与事实不能为空"}
            r = ks.add_entry(topic, facts, body.get("keywords"), body.get("sources"))
            return {"ok": True, "result": r}
        if op == "delete":
            kw = str(body.get("keyword", "")).strip()
            if not kw:
                return {"ok": False, "error": "缺少关键词"}
            r = ks.delete_by_keyword(kw)
            return {"ok": bool(r.get("ok", r.get("removed", 0) > 0)), "result": r}
        if op == "add_fact":
            content = str(body.get("content", "")).strip()
            if not content:
                return {"ok": False, "error": "内容为空"}
            r = ks.add_fact(content)
            return {"ok": True, "result": r}

    return {"ok": False, "error": f"不支持的操作: {kind}/{op}"}
