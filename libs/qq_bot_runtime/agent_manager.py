"""
智能体（Agent）注册表。

肥鱼单机版支持同时存在多个智能体，互不影响：
- 每个智能体 = agents/<agent_id>/agent.json（名称 / 人设 / 系统提示 / 模型 /
  启用的 brains·插件 / 平台绑定 / 是否自启）
- 每个智能体的记忆完全隔离，存放在 agents/<agent_id>/memory/（见后续阶段）

本模块只负责智能体定义的 CRUD 与默认智能体迁移，不改动既有人格/记忆逻辑。
记忆命名空间化与聊天路由在后续阶段接入。
"""
import os
import re
import json
import time
import shutil
from typing import Optional, Dict, Any, List
from quiet import degrade

_BASE = os.path.dirname(os.path.abspath(__file__))
AGENTS_DIR = os.path.join(_BASE, "agents")
DEFAULT_AGENT_ID = "feiyu"

# agent.json 字段说明（_phase 1 基础集，后续阶段可扩展）
# - id            : 唯一标识（等于目录名，snake_case）
# - name          : 展示名
# - emoji         : 图标（可选）
# - system_prompt : 系统提示词覆盖；为空时由 chat_service 依据 profile 合成
# - profile       : 人格画像（身份/性格/能力/习惯/补充），结构同 ai_profile
# - model         : {"provider": str|null, "model": str|null} 模型覆盖（null=用全局）
# - enabled_brains : 启用的 brains 白名单（空=引擎默认）
# - enabled_plugins: 启用的插件白名单（空=引擎默认）
# - bindings      : 平台→频道绑定 {"platform": ["private:123", "group:456"]}
# - auto_start    : 是否随程序自启/激活（默认 False）
# - memory_isolation: 记忆隔离策略，当前仅 "full"
# - created/updated: 时间戳

MEMORY_ISOLATION = "full"
REQUIRED_FIELDS = ("id", "name")


def _agent_dir(aid: str) -> str:
    return os.path.join(AGENTS_DIR, aid)


def _agent_file(aid: str) -> str:
    return os.path.join(_agent_dir(aid), "agent.json")


def _slugify(name: str) -> str:
    s = re.sub(r"[^\w一-鿿]+", "_", name.strip().lower())
    s = s.strip("_")
    return s or ("agent_" + time.strftime("%Y%m%d%H%M%S"))


def _atomic_write(path: str, data: Dict[str, Any]) -> None:
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def list_agents() -> List[Dict[str, Any]]:
    """返回所有智能体定义列表（按 created 升序）。"""
    if not os.path.isdir(AGENTS_DIR):
        return []
    out = []
    for name in os.listdir(AGENTS_DIR):
        fp = _agent_file(name)
        if os.path.isfile(fp):
            try:
                with open(fp, "r", encoding="utf-8") as f:
                    out.append(json.load(f))
            except Exception as e:
                degrade("libs/qq_bot_runtime/agent_manager.py:75 list_agents", e, "降级：with open(fp, 'r', encoding='utf-8') as f")
                continue
    out.sort(key=lambda d: d.get("created", 0))
    return out


def load_agent(aid: str) -> Optional[Dict[str, Any]]:
    fp = _agent_file(aid)
    if not os.path.isfile(fp):
        return None
    with open(fp, "r", encoding="utf-8") as f:
        return json.load(f)


def save_agent(defn: Dict[str, Any]) -> Dict[str, Any]:
    """校验并保存一个智能体定义；返回保存后的完整定义。"""
    for k in REQUIRED_FIELDS:
        if not defn.get(k):
            raise ValueError(f"agent 缺少必填字段: {k}")
    aid = defn["id"]
    existing = load_agent(aid)
    now = time.time()
    if existing is None:
        defn.setdefault("created", now)
    defn["updated"] = now
    defn.setdefault("emoji", "🐟")
    defn.setdefault("system_prompt", "")
    defn.setdefault("profile", {})
    defn.setdefault("model", {"provider": None, "model": None})
    defn.setdefault("enabled_brains", [])
    defn.setdefault("enabled_plugins", [])
    defn.setdefault("bindings", {})
    defn.setdefault("auto_start", False)
    defn["memory_isolation"] = MEMORY_ISOLATION
    _atomic_write(_agent_file(aid), defn)
    return defn


def create_agent(name: str, agent_id: Optional[str] = None, **overrides) -> str:
    """新建智能体，返回其 id。"""
    aid = agent_id or _slugify(name)
    if load_agent(aid) is not None:
        raise ValueError(f"agent_id 已存在: {aid}")
    defn: Dict[str, Any] = {"id": aid, "name": name}
    defn.update(overrides)
    save_agent(defn)
    return aid


def update_agent(aid: str, **updates) -> Dict[str, Any]:
    cur = load_agent(aid)
    if cur is None:
        raise ValueError(f"agent 不存在: {aid}")
    cur.update(updates)
    cur["id"] = aid  # id 不可改
    return save_agent(cur)


def delete_agent(aid: str) -> None:
    """删除智能体（含其记忆目录，完全隔离故可整体移除）。"""
    d = _agent_dir(aid)
    if os.path.isdir(d):
        shutil.rmtree(d)


def get_active_agents() -> List[Dict[str, Any]]:
    """返回 auto_start=True 的智能体。"""
    return [a for a in list_agents() if a.get("auto_start")]


def match_binding(platform: str, channel_type: str, channel_id: str) -> Optional[str]:
    """依据 bindings 把 (平台, 频道) 匹配到某智能体 id；无匹配返回 None。

    绑定值形如 "private:123" / "group:456"；"*" 表示匹配该平台全部频道。
    """
    target = f"{channel_type}:{channel_id}"
    for a in list_agents():
        binds = a.get("bindings", {}).get(platform)
        if not binds:
            continue
        if "*" in binds or target in binds:
            return a["id"]
    return None


def ensure_default(profile: Optional[Dict[str, Any]] = None) -> str:
    """若没有任何智能体，则创建默认 feiyu（肥鱼娘）智能体。

    profile 缺省时从 ai_profile.load_profile() 取（回退到代码内置 DEFAULT_PROFILE）。
    不删除或改动既有 ai_profile.json，保证向后兼容，直到后续阶段切换 chat_service。
    """
    if list_agents():
        return DEFAULT_AGENT_ID
    if profile is None:
        try:
            import ai_profile
            profile = ai_profile.load_profile()
        except Exception:
            profile = {}
    save_agent({
        "id": DEFAULT_AGENT_ID,
        "name": "肥鱼娘",
        "emoji": "🐟",
        "system_prompt": "",
        "profile": profile or {},
        "auto_start": True,
    })
    return DEFAULT_AGENT_ID


def resolve_profile(aid: str) -> Dict[str, Any]:
    """取某智能体的人格画像（缺失字段回退到全局 ai_profile 默认）。"""
    a = load_agent(aid) or load_agent(DEFAULT_AGENT_ID) or {}
    prof = dict(a.get("profile", {}))
    try:
        import ai_profile
        base = ai_profile.load_profile()
        for k, v in base.items():
            prof.setdefault(k, v)
    except Exception as e:
        degrade("libs/qq_bot_runtime/agent_manager.py:194 resolve_profile", e, "降级：import ai_profile")
    return prof


if __name__ == "__main__":
    ensure_default()
    for a in list_agents():
        print(a["id"], a.get("name"), "auto_start=" + str(a.get("auto_start")))
