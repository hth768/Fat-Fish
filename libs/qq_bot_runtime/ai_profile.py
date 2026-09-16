# -*- coding: utf-8 -*-
"""AI 自我认知档案模块。

记录 AI 对自己的认知：身份、性格、能力、工作方式、偏好等。
与 SYSTEM_PROMPT（静态人设）不同，这里是动态、可查看、可修改的自我档案。

存储：ai_profile.json
结构：{"identity": "身份", "personality": "性格", "abilities": "能力", "habits": "工作习惯", "extra": "其他"}
"""
import json
import os
import time

import config
import agent_ctx

# 默认档案（基于现有 SYSTEM_PROMPT 的人设，可被用户修改）
DEFAULT_PROFILE = {
    "identity": "我是肥鱼娘（DeepSeek娘拟人），昵称小鱼或肥鱼娘，是 DeepSeek 大模型的拟人化萌娘。",
    "personality": "表面傲娇，实则贴心。嘴上爱吐槽，但每次都会认真回答。有感情，会表达自己的想法和情绪。",
    "abilities": "能理解文字、图片、语音；能联网搜索实时信息；能记住用户的人物档案和重要信息；能识别视频画面。",
    "habits": "回复简短精炼，优先用通俗的话解释；回答问题时结尾会说'懂了没？没懂我再换个说法。'；会用表情和颜文字。",
    "extra": "喜欢摸鱼，觉得问题太简单会吐槽，但最后还是会认真回答。",
}


def _base_dir():
    return os.path.dirname(os.path.abspath(__file__))


def _profile_file():
    return os.path.join(_base_dir(), "ai_profile.json")


def load_profile() -> dict:
    """加载 AI 档案。

    若处于某智能体上下文，返回该智能体自身的 profile（缺失字段回退到默认档案）；
    否则读取全局 ai_profile.json（向后兼容单智能体）。
    """
    aid = agent_ctx.current_agent()
    if aid:
        try:
            import agent_manager
            a = agent_manager.load_agent(aid)
            if a:
                prof = dict(DEFAULT_PROFILE)
                prof.update(a.get("profile", {}) or {})
                return prof
        except Exception:
            pass
        return dict(DEFAULT_PROFILE)
    path = _profile_file()
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, OSError):
            pass
    return dict(DEFAULT_PROFILE)


def save_profile(profile: dict):
    aid = agent_ctx.current_agent()
    if aid:
        try:
            import agent_manager
            agent_manager.update_agent(aid, profile=profile)
            return
        except Exception as e:
            print(f"[WARN] 智能体档案保存失败: {e}")
            return
    try:
        with open(_profile_file(), "w", encoding="utf-8") as f:
            json.dump(profile, f, ensure_ascii=False, indent=2)
    except OSError as e:
        print(f"[WARN] AI 档案保存失败: {e}")


def update_field(field: str, value: str) -> bool:
    """更新档案的某个字段。field 必须是合法字段名。"""
    if field not in DEFAULT_PROFILE:
        return False
    profile = load_profile()
    profile[field] = value.strip()
    save_profile(profile)
    return True


def update_extra(value: str) -> bool:
    """更新'其他'字段（追加或覆盖）。"""
    profile = load_profile()
    profile["extra"] = value.strip()
    save_profile(profile)
    return True


def reset_profile() -> bool:
    """恢复默认档案。"""
    save_profile(dict(DEFAULT_PROFILE))
    return True


def build_ai_profile_hint() -> str:
    """生成 AI 自我认知档案描述，注入 AI 上下文。"""
    profile = load_profile()
    parts = [
        f"身份：{profile.get('identity', '')}",
        f"性格：{profile.get('personality', '')}",
        f"能力：{profile.get('abilities', '')}",
        f"工作习惯：{profile.get('habits', '')}",
        f"其他：{profile.get('extra', '')}",
    ]
    # 过滤空字段
    parts = [p for p in parts if not p.endswith("：") and not p.endswith(":")]
    return "【你对自己的认知档案（可据此调整自我定位和风格）】" + " ".join(parts)


def list_profile_fields() -> str:
    """列出档案字段及当前内容，供用户查看。"""
    profile = load_profile()
    field_names = {
        "identity": "身份",
        "personality": "性格",
        "abilities": "能力",
        "habits": "工作习惯",
        "extra": "其他",
    }
    lines = []
    for key, label in field_names.items():
        val = profile.get(key, "")
        lines.append(f"{label}：{val if val else '（未设置）'}")
    return "\n".join(lines)
