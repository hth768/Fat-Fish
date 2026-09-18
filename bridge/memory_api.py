# -*- coding: utf-8 -*-
"""记忆中心 API：人物档案 / 重要事项 / 人格记忆 / 反思记忆 / 全文历史 / 知识库 / 会话记忆。

全部通过 qq_bot 既有模块的公开函数读写（数据文件在 qq_bot 内，本应用不改任何源码）。
"""
import json
import os
import re
import time
from quiet import degrade

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
    except Exception as e:
        degrade("bridge/memory_api.py:43 list_users", e, "降级：import long_term_memory")
    try:
        import important_notes
        for uid in (important_notes.load_notes() or {}):
            add(uid, "事项")
    except Exception as e:
        degrade("bridge/memory_api.py:49 list_users", e, "降级：import important_notes")
    try:
        import persona_memory
        for uid in ((persona_memory._load_data() or {}).get("personas") or {}):
            add(uid, "人格")
    except Exception as e:
        degrade("bridge/memory_api.py:55 list_users", e, "降级：import persona_memory")
    try:
        import reflection_memory
        for r in ((reflection_memory._load_data() or {}).get("reflections") or []):
            add(r.get("user_id"), "反思")
    except Exception as e:
        degrade("bridge/memory_api.py:61 list_users", e, "降级：import reflection_memory")

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
    except Exception as e:
        degrade("bridge/memory_api.py:172 memory_stats", e, "降级：import long_term_memory as ltm")
    try:
        import important_notes
        out["notes"] = sum(len(v or []) for v in (important_notes.load_notes() or {}).values())
    except Exception as e:
        degrade("bridge/memory_api.py:177 memory_stats", e, "降级：import important_notes")
    try:
        import persona_memory
        out["persona"] = len((persona_memory._load_data() or {}).get("personas") or {})
    except Exception as e:
        degrade("bridge/memory_api.py:182 memory_stats", e, "降级：import persona_memory")
    try:
        import reflection_memory
        out["reflection"] = len((reflection_memory._load_data() or {}).get("reflections") or [])
    except Exception as e:
        degrade("bridge/memory_api.py:187 memory_stats", e, "降级：import reflection_memory")
    try:
        import knowledge_store
        out["knowledge"] = knowledge_store.count()
    except Exception as e:
        degrade("bridge/memory_api.py:192 memory_stats", e, "降级：import knowledge_store")
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


# ---------------- 记忆文件导入 / 导出 ----------------

def _norm_fact(f):
    return re.sub(r"\s+", "", str(f)).lower()


def export_memory() -> dict:
    """导出全部记忆维度，供备份 / 迁移。"""
    out = {
        "version": 1,
        "exported_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "profiles": {}, "notes": {},
        "persona": {"personas": {}, "global_style": ""},
        "reflection": {"reflections": [], "interaction_rules": [], "stats": {}},
        "knowledge": [], "history": {}, "sessions": {},
    }
    try:
        import long_term_memory as ltm
        out["profiles"] = ltm.load_profiles() or {}
    except Exception as e:
        degrade("bridge/memory_api.py:286 export_memory", e, "降级：import long_term_memory as ltm")
    try:
        import important_notes
        out["notes"] = important_notes.load_notes() or {}
    except Exception as e:
        degrade("bridge/memory_api.py:291 export_memory", e, "降级：import important_notes")
    try:
        import persona_memory
        d = persona_memory._load_data() or {}
        out["persona"] = {
            "personas": d.get("personas", {}),
            "global_style": d.get("global_style", ""),
        }
    except Exception as e:
        degrade("bridge/memory_api.py:300 export_memory", e, "降级：import persona_memory")
    try:
        import reflection_memory
        d = reflection_memory._load_data() or {}
        out["reflection"] = {
            "reflections": d.get("reflections", []),
            "interaction_rules": d.get("interaction_rules", []),
            "stats": d.get("stats", {}),
        }
    except Exception as e:
        degrade("bridge/memory_api.py:310 export_memory", e, "降级：import reflection_memory")
    try:
        import knowledge_store as ks
        out["knowledge"] = ks._load() or []
    except Exception as e:
        degrade("bridge/memory_api.py:315 export_memory", e, "降级：import knowledge_store as ks")
    try:
        import long_term_memory as ltm
        hdir = ltm._history_dir()
        if os.path.isdir(hdir):
            for fn in os.listdir(hdir):
                if fn.endswith(".jsonl"):
                    uid = os.path.splitext(fn)[0]
                    try:
                        with open(os.path.join(hdir, fn), "r", encoding="utf-8") as f:
                            out["history"][uid] = [json.loads(l) for l in f if l.strip()]
                    except Exception as e:
                        degrade("bridge/memory_api.py:327 export_memory", e, "降级：with open(os.path.join(hdir, fn), 'r', encoding='u")
    except Exception as e:
        degrade("bridge/memory_api.py:329 export_memory", e, "降级：import long_term_memory as ltm")
    try:
        out["sessions"] = _read_json(os.path.join(os.getcwd(), _MEMORY_FILE), {}) or {}
    except Exception as e:
        degrade("bridge/memory_api.py:334 export_memory", e, "降级：out['sessions'] = _read_json(os.path.join(os.getcw")
    return out


def import_memory(obj, mode="replace") -> dict:
    """导入记忆导出 JSON。mode: replace 覆盖整维度 / merge 合并去重。"""
    if not isinstance(obj, dict):
        return {"ok": False, "error": "导入数据需为 JSON 对象"}
    result = {"ok": True, "imported": {}}
    replace = (mode == "replace")

    if "profiles" in obj and isinstance(obj["profiles"], dict):
        import long_term_memory as ltm
        profiles = ltm.load_profiles() or {}
        for uid, entry in (obj["profiles"] or {}).items():
            uid = str(uid); entry = entry or {}
            facts = [str(f) for f in (entry.get("facts") or []) if str(f).strip()]
            if replace or uid not in profiles:
                profiles[uid] = {"facts": list(facts), "updated": time.time()}
            else:
                existing = {_norm_fact(f) for f in profiles[uid].get("facts", [])}
                merged = list(profiles[uid].get("facts", []))
                for f in facts:
                    if _norm_fact(f) not in existing:
                        merged.append(f); existing.add(_norm_fact(f))
                profiles[uid] = {"facts": merged, "updated": time.time()}
        ltm.save_profiles(profiles)
        result["imported"]["profiles"] = len(profiles)

    if "notes" in obj and isinstance(obj["notes"], dict):
        import important_notes as notes_mod
        notes = notes_mod.load_notes() or {}
        for uid, lst in (obj["notes"] or {}).items():
            uid = str(uid)
            if not isinstance(lst, list):
                continue
            if replace or uid not in notes:
                notes[uid] = []
            for n in lst:
                text = (n.get("text") if isinstance(n, dict) else str(n)).strip()
                if text:
                    notes_mod.add_note(uid, text, (n.get("category", "") if isinstance(n, dict) else ""))
        result["imported"]["notes"] = sum(len(v) for v in notes_mod.load_notes().values())

    if "persona" in obj and isinstance(obj["persona"], dict):
        import persona_memory
        data = persona_memory._load_data()
        src = obj["persona"] or {}
        personas = src.get("personas", {}) if isinstance(src, dict) else {}
        if replace:
            data["personas"] = dict(personas)
        else:
            data.setdefault("personas", {})
            for uid, p in personas.items():
                data["personas"][str(uid)] = p
        if "global_style" in src:
            data["global_style"] = src["global_style"]
        persona_memory._save_data()
        result["imported"]["persona"] = len(data["personas"])

    if "reflection" in obj and isinstance(obj["reflection"], dict):
        import reflection_memory
        data = reflection_memory._load_data()
        src = obj["reflection"] or {}
        if replace:
            data["reflections"] = list(src.get("reflections", []))
            data["interaction_rules"] = list(src.get("interaction_rules", []))
            data["stats"] = src.get("stats", {})
        else:
            existing_ids = {r.get("id") for r in data.get("reflections", [])}
            for r in src.get("reflections", []):
                if r.get("id") not in existing_ids:
                    data.setdefault("reflections", []).append(r)
            for rule in src.get("interaction_rules", []):
                if rule not in data.get("interaction_rules", []):
                    data.setdefault("interaction_rules", []).append(rule)
        reflection_memory._save_data()
        result["imported"]["reflection"] = len(data["reflections"])

    if "knowledge" in obj and isinstance(obj["knowledge"], list):
        import knowledge_store as ks
        if replace:
            ks._save(list(obj["knowledge"]))
        else:
            for e in obj["knowledge"]:
                topic = (e.get("topic") or "").strip()
                facts = [str(f).strip() for f in (e.get("facts") or []) if str(f).strip()]
                if topic and facts:
                    ks.add_entry(topic, facts, e.get("keywords"), e.get("sources"))
        result["imported"]["knowledge"] = ks.count()

    if "history" in obj and isinstance(obj["history"], dict):
        import long_term_memory as ltm
        hdir = ltm._history_dir()
        os.makedirs(hdir, exist_ok=True)
        for uid, lines in obj["history"].items():
            if not isinstance(lines, list):
                continue
            fp = os.path.join(hdir, str(uid) + ".jsonl")
            existing = set()
            if os.path.isfile(fp):
                with open(fp, "r", encoding="utf-8") as f:
                    for l in f:
                        l = l.strip()
                        if l:
                            existing.add(l)
            with open(fp, "a", encoding="utf-8") as f:
                for item in lines:
                    line = json.dumps(item, ensure_ascii=False)
                    if line not in existing:
                        f.write(line + "\n"); existing.add(line)
        result["imported"]["history"] = len(obj["history"])

    if "sessions" in obj and isinstance(obj["sessions"], dict):
        path = os.path.join(os.getcwd(), _MEMORY_FILE)
        cur = _read_json(path, {}) or {}
        if replace:
            cur = dict(obj["sessions"])
        else:
            for k, v in obj["sessions"].items():
                cur[k] = v
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(cur, f, ensure_ascii=False, indent=2)
            result["imported"]["sessions"] = 1
        except Exception as e:
            result.setdefault("warnings", []).append("sessions: " + repr(e))

    return result


def import_chatlog(text, uid="app_owner", target="knowledge", mode="auto") -> dict:
    """解析聊天记录文本（txt/csv/md）写入记忆。

    target: knowledge（默认）/ profile（人物档案）/ notes（重要事项）。
    启发式：按句切分，去重后写入目标记忆维度。
    """
    if not text or not text.strip():
        return {"ok": False, "error": "聊天记录为空"}
    uid = str(uid or "app_owner")
    seen = set(); clean = []
    for raw in text.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        for p in re.split(r"[。！？!?；;\n]", raw):
            p = p.strip()
            if len(p) >= 4 and _norm_fact(p) not in seen:
                seen.add(_norm_fact(p)); clean.append(p)
    if not clean:
        return {"ok": False, "error": "未解析到可记忆的内容"}

    if target == "notes":
        import important_notes as notes_mod
        added = 0
        for f in clean:
            if notes_mod.add_note(uid, f, "聊天记录导入"):
                added += 1
    elif target == "profile":
        import long_term_memory as ltm
        profiles = ltm.load_profiles() or {}
        entry = profiles.get(uid) or {"facts": [], "updated": 0}
        existing = {_norm_fact(x) for x in entry.get("facts", [])}
        for f in clean:
            if _norm_fact(f) not in existing:
                entry.setdefault("facts", []).append(f); existing.add(_norm_fact(f))
        entry["updated"] = time.time()
        profiles[uid] = entry
        ltm.save_profiles(profiles)
        added = len(entry["facts"])
    else:  # knowledge
        import knowledge_store as ks
        topic = clean[0][:30]
        r = ks.add_entry(topic, clean, ["聊天记录导入"], [])
        added = r.get("new_facts", 0)

    return {"ok": True, "target": target, "parsed": len(clean), "added": added}
