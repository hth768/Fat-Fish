# -*- coding: utf-8 -*-
"""长期记忆：人物档案 + 全文历史 + 语义检索。

- 人物档案（profile）：永久记住用户的关键信息，自动去重、纠错
- 全文历史（history）：所有对话原文保存，带时间戳
- 语义检索（retrieval）：从历史里找与问题相关的内容
"""
import json
import os
import re
import time

import config
import fact_store
import file_lock
import time_indexed_memory
import agent_ctx
from quiet import degrade


def _base_dir():
    return os.path.dirname(os.path.abspath(__file__))


def _ns_base():
    d = agent_ctx.ns_dir() or _base_dir()
    os.makedirs(d, exist_ok=True)
    return d


def _profile_file():
    return os.path.join(_ns_base(), "user_profiles.json")


def _history_file():
    return os.path.join(_ns_base(), "chat_history.json")


def _history_dir():
    """历史记录分文件存储目录（每个用户一个 jsonl 文件，追加写）。"""
    d = os.path.join(_ns_base(), "chat_history")
    os.makedirs(d, exist_ok=True)
    return d


def _user_history_path(user_id):
    """某个用户的历史文件路径：chat_history/<user_id>.jsonl

    跨平台用户 id 可能带冒号（如 B 站 bili:123），Windows 文件名不允许，
    统一替换成下划线（只影响文件名，JSON 记忆键仍用原始 id）。
    """
    safe = re.sub(r'[\\/:*?"<>|]', "_", str(user_id))
    return os.path.join(_history_dir(), f"{safe}.jsonl")


# ---------------- 人物档案 ----------------
def load_profiles() -> dict:
    """加载所有用户档案 {user_id: {facts: [...], updated: timestamp}}"""
    path = _profile_file()
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_profiles(profiles: dict):
    try:
        with open(_profile_file(), "w", encoding="utf-8") as f:
            json.dump(profiles, f, ensure_ascii=False, indent=2)
    except OSError as e:
        print(f"[WARN] 人物档案保存失败: {e}")


# ---------------- 人物档案：按事实记录时间戳 ----------------
# facts 本身会被 merge_profile_facts 整体重排，顺序不代表新旧。为了「按时间就近取称呼」，
# 单独用一份 {uid: {fact_text: 首次出现时间戳}} 记录，供情绪等模块判断哪条事实最新。
def _profile_times_file():
    return os.path.join(_ns_base(), "user_profile_times.json")


def _load_times() -> dict:
    path = _profile_times_file()
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_times(times: dict):
    try:
        with open(_profile_times_file(), "w", encoding="utf-8") as f:
            json.dump(times, f, ensure_ascii=False)
    except OSError as e:
        print(f"[WARN] 档案时间戳保存失败: {e}")


def get_fact_times(user_id) -> dict:
    """某用户各事实的「首次出现时间戳」{fact_text: ts}（缺失的视为最旧）。"""
    return _load_times().get(str(user_id), {})


def _touch_fact_times(uid, facts, base_ts=None):
    """把 facts 里没见过的事实登记当前时间（已存在的保留原时间戳）。调用方需已持锁。

    base_ts：给"新出现"事实打的时间；默认 now()。replace_profile 里用 max(旧updated, now)
    保证新称呼的时间戳严格晚于任何旧事实，避免同秒歧义。
    """
    now = time.time() if base_ts is None else max(base_ts, time.time())
    times = _load_times()
    umap = times.setdefault(uid, {})
    for fact in facts:
        fact = str(fact).strip()
        if fact and fact not in umap:
            umap[fact] = now
    _save_times(times)


def get_profile(user_id) -> dict:
    """获取某个用户的档案。
    
    v2.0: 优先从 fact_store 读取，回退到 JSON。
    """
    uid = str(user_id)
    
    # 优先从 fact_store 读取
    try:
        store = fact_store.get_fact_store()
        facts = store.get_facts(uid)
        if facts:
            # 转换为旧格式
            return {
                "facts": [f["text"] for f in facts],
                "updated": max((f["updated_at"] for f in facts), default=time.time())
            }
    except Exception as e:
        print(f"[LTM] fact_store 读取失败，回退到 JSON: {e}")
    
    # 回退到 JSON
    profiles = load_profiles()
    return profiles.get(uid, {})


def update_profile(user_id, facts: list):
    """更新用户档案（合并新事实，去重，限制条数）。

    v2.0: 使用 fact_store 的原子提交和 SHA-256 去重。
    同时保留旧 JSON 格式以兼容。
    """
    uid = str(user_id)
    
    # 使用新的 fact_store（SQLite FTS5 + 原子提交）
    try:
        store = fact_store.get_fact_store()
        store.add_facts(uid, facts)
    except Exception as e:
        print(f"[LTM] fact_store 更新失败，回退到 JSON: {e}")
    
    # 同时更新旧 JSON 格式（向后兼容）
    with file_lock.file_lock(_profile_file()):
        profiles = load_profiles()
        if uid not in profiles:
            profiles[uid] = {"facts": [], "updated": time.time()}
        existing = profiles[uid].get("facts", [])
        for fact in facts:
            fact = fact.strip()
            if fact and fact not in existing:
                existing.append(fact)
        if len(existing) > 50:
            existing = existing[-50:]
        profiles[uid]["facts"] = existing
        profiles[uid]["updated"] = time.time()
        save_profiles(profiles)
        _touch_fact_times(uid, existing)


def replace_profile(user_id, facts: list):
    """用智能合并后的新事实列表替换（用于纠错后的更新）。
    
    v2.0: 使用 fact_store 的原子提交。
    """
    uid = str(user_id)
    
    # 使用 fact_store 原子提交
    try:
        store = fact_store.get_fact_store()
        # 先删除旧事实
        old_facts = store.get_facts(uid)
        for old_fact in old_facts:
            store.delete_fact(uid, old_fact['text'])
        # 再添加新事实
        if facts:
            store.add_facts(uid, facts)
    except Exception as e:
        print(f"[LTM] fact_store 替换失败，回退到 JSON: {e}")
    
    # 同时更新旧 JSON 格式（向后兼容）
    with file_lock.file_lock(_profile_file()):
        profiles = load_profiles()
        prev_updated = (profiles.get(uid) or {}).get("updated", 0) or time.time()
        profiles[uid] = {"facts": facts, "updated": time.time()}
        save_profiles(profiles)
        # 新出现的事实（如改了称呼）时间戳严格晚于历史，供"就近取"判定
        _touch_fact_times(uid, facts, base_ts=max(prev_updated, time.time()))


def build_profile_hint(user_id) -> str:
    """生成人物档案描述，注入给 AI。

    若档案里能解析出"当前生效称呼"（见 emotion.resolve_display_name），
    在 facts 之前压一行权威称呼规则，明确告诉 AI 该怎么叫 TA——避免档案里
    堆了多条互相矛盾的历史称呼（改名/旧外号/"主人"等）时，模型自己乱挑一个，
    出现"对话称呼与记忆文件不符"。QQ 与 MC 两条注入链路都经此函数，同源生效。
    """
    profile = get_profile(user_id)
    facts = profile.get("facts", [])
    if not facts:
        return ""
    head = ""
    try:
        import emotion  # 函数内导入：emotion 也会反向 import 本模块，避免顶层循环
        nm = emotion.resolve_display_name(user_id)
        if nm:
            head = (f"【称呼规则（最高优先级）】你称呼 TA 时统一用「{nm}」。"
                    f"这是当前生效的称呼，覆盖下面档案里任何其它旧叫法/外号；"
                    f"除非 TA 本次明确要求换叫法，否则不要使用别的称呼。\n")
    except Exception as e:
        print(f"[LTM] 称呼规则解析失败（忽略）: {e}")
    return head + "【你对该用户的了解】" + "；".join(facts)


# ---------------- 全文历史 ----------------
def _migrate_old_history():
    """把旧的单一 chat_history.json 迁移到分文件 jsonl 格式（只执行一次）。"""
    old = _history_file()
    if not os.path.exists(old):
        return
    try:
        with open(old, "r", encoding="utf-8") as f:
            records = json.load(f)
        if isinstance(records, list):
            from collections import defaultdict
            grouped = defaultdict(list)
            for r in records:
                grouped[str(r.get("user_id"))].append(r)
            for uid, recs in grouped.items():
                with open(_user_history_path(uid), "a", encoding="utf-8") as f:
                    for r in recs:
                        f.write(json.dumps(r, ensure_ascii=False) + "\n")
        # 迁移后重命名旧文件，避免重复迁移
        os.rename(old, old + ".migrated")
        print(f"[INFO] 旧历史已迁移到分文件格式，共 {len(records) if isinstance(records, list) else 0} 条")
    except Exception as e:
        print(f"[WARN] 历史迁移失败: {e}")


def append_history(user_id, role: str, content: str, message_type: str = ""):
    """追加一条对话历史（带时间戳）。
    
    v2.0: 使用 time_indexed_memory (SQLite) 存储，支持高效时间范围查询。
    """
    if not config.ENABLE_FULL_HISTORY:
        return
    
    # 使用新的 time_indexed_memory
    try:
        tim = time_indexed_memory.get_time_indexed_memory()
        tim.append_history(user_id, role, content, message_type)
    except Exception as e:
        print(f"[LTM] time_indexed_memory 写入失败: {e}")


def merge_alias_history(alias_user_id, canonical_user_id) -> bool:
    """把别名（如游戏名）的历史文件并入同一个人（canonical，如 QQ 号）的文件。

    跨平台绑定激活时调用：chat_history/<alias>.jsonl 的行改写 user_id 后追加进
    <canonical>.jsonl，随后 alias 文件改名为 .merged（幂等标记，只合并一次）。

    Returns:
        True 表示本次执行了合并；False 表示无事可做（已合并过 / 无文件 / 同名）。
    """
    alias = str(alias_user_id)
    canon = str(canonical_user_id)
    if not alias or not canon or alias == canon:
        return False
    alias_path = _user_history_path(alias)
    merged_path = alias_path + ".merged"
    with file_lock.file_lock(alias_path):
        if os.path.exists(merged_path) or not os.path.exists(alias_path):
            return False  # 已合并过，或别名侧本来就没有历史
        try:
            with open(alias_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
        except OSError as e:
            print(f"[WARN] 历史合并读取失败({alias}): {e}")
            return False
        records = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
                r["user_id"] = canon
                records.append(json.dumps(r, ensure_ascii=False))
            except json.JSONDecodeError as e:
                degrade("long_term_memory.merge_alias_history", e, "跳过损坏的别名历史记录")
                continue
        if not records:
            try:
                os.rename(alias_path, merged_path)
            except OSError as e:
                degrade("libs/qq_bot_runtime/long_term_memory.py:319 merge_alias_history", e, "降级：os.rename(alias_path, merged_path)")
            return False
        # 追加进 canonical 文件（与 append_history 同一把锁）
        canon_path = _user_history_path(canon)
        with file_lock.file_lock(canon_path):
            try:
                with open(canon_path, "a", encoding="utf-8") as f:
                    for line in records:
                        f.write(line + "\n")
            except OSError as e:
                print(f"[WARN] 历史合并写入失败({canon}): {e}")
                return False
        try:
            os.rename(alias_path, merged_path)
        except OSError as e:
            # 数据已并入但改名失败（如被占用）：下次调用会再查，避免重复并入
            print(f"[WARN] 历史合并改名失败({alias}): {e}")
            return False
        print(f"[INFO] 历史合并: {alias} → {canon}，共 {len(records)} 条")
        return True


def _read_user_history_lines(user_id, limit: int) -> list:
    """读取某个用户最近的 limit 条历史记录（从文件尾部按字节预算读，不全量加载）。"""
    path = _user_history_path(user_id)
    if not os.path.exists(path):
        return []
    try:
        size = os.path.getsize(path)
        # 尾部字节预算：每条记录平均 ~256B，放大 2 倍防超长记录漏读
        budget = max(64 * 1024, limit * 512)
        with open(path, "rb") as f:
            if size > budget:
                f.seek(size - budget)
                f.readline()  # 跳过被截断的半行
            data = f.read()
        lines = data.decode("utf-8", errors="replace").splitlines()
    except OSError:
        return []
    records = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError as e:
            degrade("libs/qq_bot_runtime/long_term_memory.py:366 _read_user_history_lines", e, "降级：records.append(json.loads(line))")
            continue
    return records[-limit:]


def get_user_history(user_id, limit: int = 200) -> list:
    """获取某个用户最近的历史记录（取最近 limit 条）。
    
    v2.0: 使用 time_indexed_memory (SQLite) 读取。
    """
    try:
        tim = time_indexed_memory.get_time_indexed_memory()
        return tim.get_recent_history(user_id, limit)
    except Exception as e:
        print(f"[LTM] time_indexed_memory 读取失败: {e}")
        return []


def search_history(user_id, keyword: str, limit: int = 5) -> list:
    """按关键词检索某个用户的历史对话。
    
    v2.0: 使用 FTS5 全文索引搜索。
    """
    try:
        tim = time_indexed_memory.get_time_indexed_memory()
        return tim.search_history(user_id, keyword, limit)
    except Exception as e:
        print(f"[LTM] time_indexed_memory 搜索失败: {e}")
        return []
