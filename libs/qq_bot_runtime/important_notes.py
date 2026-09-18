# -*- coding: utf-8 -*-
"""重要信息存储模块。

让 AI 能长期记住用户明确要求保存的重要信息（如生日、密码提示、重要日期、约定等）。
与人物档案不同，这里是用户主动指定要记住的"笔记"，可增删查。

存储：important_notes.json
结构：{user_id: [{"text": 信息, "time": 时间戳, "category": 分类}, ...]}
"""
import json
import os
import time

import config
import file_lock
import agent_ctx


def _base_dir():
    return os.path.dirname(os.path.abspath(__file__))


def _ns_base():
    # 按当前 bot 命名空间隔离；默认 feiyu 回落到引擎目录（不迁移历史数据）
    return agent_ctx.agent_storage_dir(_base_dir())


def _notes_file():
    return os.path.join(_ns_base(), "important_notes.json")


def load_notes() -> dict:
    """加载所有重要信息 {user_id: [note, ...]}"""
    path = _notes_file()
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_notes(notes: dict):
    try:
        with open(_notes_file(), "w", encoding="utf-8") as f:
            json.dump(notes, f, ensure_ascii=False, indent=2)
    except OSError as e:
        print(f"[WARN] 重要信息保存失败: {e}")


def get_user_notes(user_id) -> list:
    """获取某个用户的重要信息列表。"""
    notes = load_notes()
    return notes.get(str(user_id), [])


def add_note(user_id, text: str, category: str = "") -> bool:
    """新增一条重要信息。重复（完全相同的文本）时不重复添加，返回是否新增。

    QQ 核心进程与 MC bot 进程都会写本文件，load-modify-save 全程加锁防丢更新。
    """
    with file_lock.file_lock(_notes_file()):
        notes = load_notes()
        uid = str(user_id)
        if uid not in notes:
            notes[uid] = []
        text = text.strip()
        if not text:
            return False
        # 去重：忽略已存在完全相同的文本
        for existing in notes[uid]:
            if existing.get("text") == text:
                return False
        notes[uid].append({
            "text": text,
            "category": category.strip(),
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        })
        # 限制单用户笔记数量，防止无限增长
        max_notes = getattr(config, "IMPORTANT_NOTES_MAX", 200)
        if len(notes[uid]) > max_notes:
            notes[uid] = notes[uid][-max_notes:]
        save_notes(notes)
        return True


def delete_note(user_id, index=None, keyword=None) -> int:
    """删除重要信息。index 是 1 起序号，或按关键词匹配。返回删除条数。"""
    with file_lock.file_lock(_notes_file()):
        notes = load_notes()
        uid = str(user_id)
        if uid not in notes:
            return 0
        user_notes = notes[uid]

        if index is not None:
            # 1 起序号
            if 1 <= index <= len(user_notes):
                del user_notes[index - 1]
                save_notes(notes)
                return 1
            return 0

        if keyword:
            before = len(user_notes)
            notes[uid] = [n for n in user_notes if keyword not in n.get("text", "")]
            removed = before - len(notes[uid])
            if removed > 0:
                save_notes(notes)
            return removed

        return 0


def clear_notes(user_id) -> int:
    """清空某个用户的全部重要信息。返回删除条数。"""
    with file_lock.file_lock(_notes_file()):
        notes = load_notes()
        uid = str(user_id)
        if uid not in notes:
            return 0
        count = len(notes[uid])
        del notes[uid]
        save_notes(notes)
        return count


def build_notes_hint(user_id) -> str:
    """生成重要信息描述，注入给 AI 对话上下文。"""
    notes = get_user_notes(user_id)
    if not notes:
        return ""
    parts = []
    for n in notes:
        text = n.get("text", "")
        cat = n.get("category", "")
        if cat:
            parts.append(f"[{cat}] {text}")
        else:
            parts.append(text)
    return "【你帮用户长期记住的重要信息】" + "；".join(parts)


def search_notes(user_id, keyword: str) -> list:
    """按关键词搜索某个用户的重要信息。"""
    notes = get_user_notes(user_id)
    return [n for n in notes if keyword in n.get("text", "")]
