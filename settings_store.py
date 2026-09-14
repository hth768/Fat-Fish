# -*- coding: utf-8 -*-
"""应用配置层：覆盖式配置中心（不改动 qq_bot 任何源文件）。

原理：
- qq_bot/config.py 是源文件，本应用不写它。
- 所有在 App 界面里改的配置，落到 f:/feiyu_app/data/app_settings.json（覆盖层）。
- 启动时与保存时，把覆盖层的键 setattr 到已导入的 config 模块上，运行时立即生效。
- 供应商相关键改完后调用 ai_provider.reload_provider_config() 热重载路由。

CONFIG_SCHEMA 同时是「配置页」的 UI 数据源：分组 + 类型 + 标签。
"""
import json
import os
import threading

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(APP_DIR, "data")
SETTINGS_FILE = os.path.join(DATA_DIR, "app_settings.json")

_lock = threading.Lock()


# ----------------------------------------------------------------------
# 覆盖层读写
# ----------------------------------------------------------------------
def load_overlay() -> dict:
    """读覆盖层 JSON。不存在返回 {}。"""
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_overlay(overlay: dict):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = SETTINGS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(overlay, f, ensure_ascii=False, indent=2)
    os.replace(tmp, SETTINGS_FILE)


def set_values(values: dict) -> dict:
    """把一批键值合并进覆盖层并落盘。返回最终覆盖层。"""
    with _lock:
        overlay = load_overlay()
        for k, v in (values or {}).items():
            overlay[k] = v
        save_overlay(overlay)
    return overlay


def get_value(key, default=None):
    overlay = load_overlay()
    if key in overlay:
        return overlay[key]
    return default


# ----------------------------------------------------------------------
# 首次运行的默认覆盖层：App 模式下外部平台/自主功能默认全关，
# 核心只保留 智能体核心 + chat + 记忆 + 总结，其余功能在「插件」页按需打开。
# ----------------------------------------------------------------------
DEFAULT_SWITCH_OFF = [
    # 平台插件（默认不开，App 自己就是 web 平台）
    "ENABLE_QQ_PLUGIN",
    "ENABLE_CONSOLE_PLUGIN",
    "ENABLE_BILIBILI_PLUGIN",
    # 会产生外部行为的功能插件（发消息/自主行为）
    "ENABLE_BILIBILI_DM",
    "ENABLE_BILIBILI_LEARN_SCHEDULE",
    "ENABLE_BALANCE_MONITOR",
    "ENABLE_PROACTIVE_SPEAKER",
    "ENABLE_TTS_SERVER",
    # 自主大脑
    "ENABLE_MC_AGENT",
    "ENABLE_MC_WATCH",
    "ENABLE_PC_CONTROL",
    "ENABLE_PVZ_BRAIN",
    "ENABLE_REALTIME_VOICE",
]


def ensure_default_overlay():
    """首次运行时生成默认覆盖层（已存在则不动用户数据）。"""
    overlay = load_overlay()
    if not overlay:
        overlay = {k: False for k in DEFAULT_SWITCH_OFF}
        overlay.setdefault("app", {
            "port": 8900,
            "auto_open_browser": True,
            "theme": "dark",
        })
        save_overlay(overlay)
    return overlay


def get_app_settings() -> dict:
    overlay = load_overlay()
    app = overlay.get("app")
    return app if isinstance(app, dict) else {}


def set_app_settings(patch: dict):
    with _lock:
        overlay = load_overlay()
        app = overlay.get("app") if isinstance(overlay.get("app"), dict) else {}
        app.update(patch or {})
        overlay["app"] = app
        save_overlay(overlay)
    return get_app_settings()


# ----------------------------------------------------------------------
# 覆盖层 -> 运行时 config 模块（立即生效）
# ----------------------------------------------------------------------
def apply_overlay() -> int:
    """把覆盖层所有标量键 setattr 到 config 模块。返回应用条数。

    必须在任何重模块 import 之前调用（app.py 引导阶段）。
    """
    import config as qq_config
    overlay = load_overlay()
    n = 0
    for k, v in overlay.items():
        if k == "app" or k.startswith("_"):
            continue
        if isinstance(v, (str, int, float, bool, list, dict)) or v is None:
            setattr(qq_config, k, v)
            n += 1
    return n


def apply_value(key, value):
    """单个键立即生效到运行时 config。"""
    import config as qq_config
    setattr(qq_config, key, value)


_SECRET_HINT = "****"


def mask_secret(v) -> str:
    s = "" if v is None else str(v)
    if not s:
        return ""
    if len(s) <= 8:
        return s[:2] + _SECRET_HINT
    return s[:4] + _SECRET_HINT + s[-4:]


def is_masked(v) -> bool:
    return isinstance(v, str) and _SECRET_HINT in v


# ----------------------------------------------------------------------
# 配置 Schema（配置页 UI 数据源）
# type: bool / int / float / str / text / secret / json
# ----------------------------------------------------------------------
CONFIG_SCHEMA = [
    {
        "section": "人设与语气",
        "hint": "系统提示词决定她是谁。改完立即生效（下一轮对话生效）。",
        "items": [
            {"key": "SYSTEM_PROMPT", "type": "text", "label": "系统人设提示词"},
        ],
    },
    {
        "section": "AI 供应商",
        "hint": "修改后自动热重载供应商路由（ai_provider），无需重启。密钥只显示掩码，留掩码原样表示不修改。",
        "items": [
            {"key": "DEEPSEEK_API_KEY", "type": "secret", "label": "DeepSeek API Key"},
            {"key": "DEEPSEEK_BASE_URL", "type": "str", "label": "DeepSeek Base URL"},
            {"key": "DEEPSEEK_MODEL", "type": "str", "label": "日常对话模型"},
            {"key": "DEEPSEEK_REASONER_MODEL", "type": "str", "label": "推理模型"},
            {"key": "GLM_API_KEY", "type": "secret", "label": "智谱 GLM API Key"},
            {"key": "GLM_BASE_URL", "type": "str", "label": "GLM Base URL"},
            {"key": "VISION_MODEL", "type": "str", "label": "视觉模型"},
            {"key": "AUTO_REASONING", "type": "bool", "label": "自动切换推理模型"},
        ],
    },
    {
        "section": "记忆系统",
        "hint": "短期记忆 / 摘要记忆 / 话题检测 / 人物档案 / 全文历史。",
        "items": [
            {"key": "ENABLE_MEMORY", "type": "bool", "label": "多轮对话记忆"},
            {"key": "MAX_HISTORY", "type": "int", "label": "短期记忆条数上限"},
            {"key": "ENABLE_SUMMARY", "type": "bool", "label": "自动压缩摘要记忆"},
            {"key": "SUMMARY_TRIGGER_RATIO", "type": "float", "label": "压缩触发比例"},
            {"key": "ENABLE_TOPIC_CHECK", "type": "bool", "label": "话题切换检测"},
            {"key": "ENABLE_PROFILE", "type": "bool", "label": "人物档案记忆"},
            {"key": "ENABLE_FULL_HISTORY", "type": "bool", "label": "全文历史"},
            {"key": "ENABLE_HISTORY_RETRIEVAL", "type": "bool", "label": "主动历史检索"},
            {"key": "ENABLE_AUTO_IMPORTANT_NOTES", "type": "bool", "label": "自动记重要事项"},
            {"key": "ENABLE_AI_PROFILE", "type": "bool", "label": "AI 自我档案"},
            {"key": "EMOTION_ENABLED", "type": "bool", "label": "情绪系统"},
            {"key": "EMOTION_DECAY_HOURS", "type": "float", "label": "情绪半衰期（小时）"},
        ],
    },
    {
        "section": "知识与联网",
        "hint": "自主知识库学习与实时信息联网搜索。",
        "items": [
            {"key": "AUTO_WEB_SEARCH", "type": "bool", "label": "自动联网搜索"},
            {"key": "ENABLE_KNOWLEDGE_LEARN", "type": "bool", "label": "知识库自动学习"},
            {"key": "ENABLE_KNOWLEDGE_RECALL", "type": "bool", "label": "知识库召回"},
            {"key": "KNOWLEDGE_MAX_ENTRIES", "type": "int", "label": "知识库主题上限"},
        ],
    },
    {
        "section": "聊天行为",
        "hint": "回复节奏与触发方式。",
        "items": [
            {"key": "SPLIT_REPLY_BY_SENTENCE", "type": "bool", "label": "按句拆分回复"},
            {"key": "SENTENCES_PER_MESSAGE", "type": "int", "label": "每条消息句子数"},
            {"key": "AGGREGATE_PRIVATE_MESSAGES", "type": "bool", "label": "私聊消息聚合"},
            {"key": "AGGREGATE_WAIT_SECONDS", "type": "int", "label": "聚合等待秒数"},
            {"key": "ONLY_MENTION_OR_PRIVATE", "type": "bool", "label": "仅@或私聊时回复"},
        ],
    },
]

# 供应商相关键：保存后需要热重载 ai_provider
PROVIDER_KEYS = {
    "DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "DEEPSEEK_MODEL",
    "DEEPSEEK_REASONER_MODEL", "GLM_API_KEY", "GLM_BASE_URL",
    "VISION_MODEL", "AI_PROVIDERS", "AI_CAPABILITY_ROUTING",
}


def schema_dict() -> dict:
    return {"sections": CONFIG_SCHEMA}
