# -*- coding: utf-8 -*-
"""表情包收集与复用管理。

- 用户发的图片：下载保存到 emojis/ 目录，用 GLM 识别情绪，记录到 emoji_meta.json
- AI 回复时通过 [表情包:文件名] 引用这些图片
"""
import hashlib
import json
import os
import random

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

    meta[name] = {"emotion": emotion, "desc": desc, "md5": digest,
                  "usage": 0, "last_used": 0.0}
    save_meta(meta)
    return name


def record_emoji_used(name: str):
    """记录某表情包被使用一次（StickerStore usage 标注）。

    累计使用次数 + 最近使用时间戳，供 build_emoji_hint 展示与轻度降权，
    避免热门图被反复只用、冷门图永远沉底（对齐新版 StickerStore 的 usage 标注）。
    """
    if not name:
        return
    meta = load_meta()
    info = meta.get(name)
    if not info:
        return
    info["usage"] = int(info.get("usage", 0)) + 1
    info["last_used"] = __import__("time").time()
    meta[name] = info
    save_meta(meta)


def build_emoji_hint(max_count: int = 60, exclude: list = None) -> str:
    """生成表情包清单描述，注入给 AI。

    exclude: 最近用过的文件名列表，命中会在规则里提示「换一张别的」，避免连续重复。
    从【全部】表情包随机抽样 max_count 张注入（而非永远只取前 max_count 张，否则模型只能看到最早收集那批）。
    注入前按 usage/last_used 升序排序：冷门、久未用的图排在前面，给它们出场机会。
    每张图附带 emotion + usage 标注（StickerStore 式），帮助模型按情绪与冷热选用。
    """
    meta = load_meta()
    if not meta:
        return ""
    exclude = set(exclude or [])
    items = list(meta.items())
    # 关键修复：从全部库随机抽样，而不是 items[:max_count]（只会暴露最早一批）
    if len(items) > max_count:
        items = random.sample(items, max_count)
    # 冷门优先：用得最少、最久没用的排前面
    items.sort(key=lambda kv: (int(kv[1].get("usage", 0)), float(kv[1].get("last_used", 0.0))))
    lines = []
    for name, info in items:
        emotion = info.get("emotion", "")
        desc = info.get("desc", "")
        usage = int(info.get("usage", 0))
        usage_tag = f"，用过{usage}次" if usage else ""
        if name in exclude:
            label = f"{name}（情绪:{emotion}{usage_tag}" + (f"，{desc}" if desc else "") + "）【最近用过，换一张别的】"
        else:
            label = f"{name}（情绪:{emotion}{usage_tag}" + (f"，{desc}" if desc else "") + "）"
        lines.append(label)
    avoid_txt = ""
    if exclude:
        avoid_txt = (f"\n另外：以下表情包最近刚用过，本次请换一张不同的"
                     f"（同情绪通常还有其他图可用）：{', '.join(sorted(exclude))}")
    hint = (
        "【可用的表情包图片】你可以用 [表情包:文件名] 引用以下图片来表达情绪：\n"
        + "\n".join(lines)
        + "\n【使用规则】只有当你的情绪和图片标注的情绪确实匹配时才使用表情包，"
        "不确定时不要用；每条消息最多用一个表情包；不要反复用同一两张，"
        "尽量多换不同文件名，冷门同情绪图也该有机会出场。"
        + avoid_txt
    )
    return hint
