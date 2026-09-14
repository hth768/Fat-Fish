# -*- coding: utf-8 -*-
"""表情包收集与复用管理。

- 用户发的图片：下载保存到 emojis/ 目录，用 GLM 识别情绪，记录到 emoji_meta.json
- AI 回复时通过 [表情包:文件名] 引用这些图片
"""
import hashlib
import json
import os

import config


def get_base_dir() -> str:
    """表情包目录的绝对路径。"""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), config.EMOJI_DIR)


def get_meta_path() -> str:
    """元数据文件路径。"""
    return os.path.join(get_base_dir(), "emoji_meta.json")


def load_meta() -> dict:
    """加载表情包元数据：{文件名: {"emotion": "...", "desc": "..."}}"""
    path = get_meta_path()
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_meta(meta: dict):
    """保存表情包元数据。"""
    os.makedirs(get_base_dir(), exist_ok=True)
    with open(get_meta_path(), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)


def next_filename(meta: dict, ext: str = ".jpg") -> str:
    """生成下一个可用的文件名。"""
    idx = len(meta) + 1
    while True:
        name = f"emoji_{idx:03d}{ext}"
        if name not in meta and not os.path.exists(os.path.join(get_base_dir(), name)):
            return name
        idx += 1


def add_emoji(image_bytes: bytes, emotion: str, desc: str = "", ext: str = ".jpg") -> str:
    """保存一张表情包图片，并记录情绪。返回文件名。

    按图片内容 MD5 去重：相同图片不会重复保存，返回已有的文件名。
    """
    meta = load_meta()
    digest = hashlib.md5(image_bytes).hexdigest()

    # 去重：检查是否已有相同内容的图片
    for name, info in meta.items():
        if info.get("md5") == digest:
            return name

    name = next_filename(meta, ext)
    os.makedirs(get_base_dir(), exist_ok=True)

    path = os.path.join(get_base_dir(), name)
    with open(path, "wb") as f:
        f.write(image_bytes)

    meta[name] = {"emotion": emotion, "desc": desc, "md5": digest}
    save_meta(meta)
    return name


def build_emoji_hint(max_count: int = 50) -> str:
    """生成表情包清单描述，注入给 AI。"""
    meta = load_meta()
    if not meta:
        return ""
    lines = []
    for name, info in list(meta.items())[:max_count]:
        emotion = info.get("emotion", "")
        desc = info.get("desc", "")
        label = f"{name}（情绪:{emotion}" + (f"，{desc}" if desc else "") + "）"
        lines.append(label)
    hint = (
        "【可用的表情包图片】你可以用 [表情包:文件名] 引用以下图片来表达情绪：\n"
        + "\n".join(lines)
        + "\n【使用规则】只有当你的情绪和图片标注的情绪确实匹配时才使用表情包，"
        "不确定时不要用；每条消息最多用一个表情包。"
    )
    return hint
