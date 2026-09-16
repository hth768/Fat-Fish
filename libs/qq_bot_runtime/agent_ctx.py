"""多智能体上下文：让记忆/存储按 agent_id 命名空间隔离。

设计：用 contextvars 携带当前智能体 id。记忆模块与存储后端在解析文件路径时读取
`current_agent()`；为空时回落到仓库根目录（向后兼容单智能体行为）。

调用方（chat_service / agent_runtime）在「处理某智能体的一条消息」边界处：
    token = agent_ctx.set_agent(aid)
    try:
        ... 处理 ...
    finally:
        agent_ctx.reset_agent(token)
即可保证该次处理内所有记忆读写都落在该智能体的命名空间。
"""
import os
import contextvars

_BASE = os.path.dirname(os.path.abspath(__file__))
AGENTS_DIR = os.path.join(_BASE, "agents")

_aid_ctx: contextvars.ContextVar = contextvars.ContextVar("feiyu_agent_id", default=None)


def set_agent(agent_id: str):
    """设置当前智能体上下文，返回 token 供 reset_agent 使用。"""
    return _aid_ctx.set(agent_id)


def reset_agent(token) -> None:
    _aid_ctx.reset(token)


def current_agent() -> "str | None":
    return _aid_ctx.get()


def ns_dir(agent_id: "str | None" = None) -> "str | None":
    """返回某智能体的记忆目录 agents/<id>/memory；无 agent 时返回 None。"""
    aid = agent_id or current_agent()
    if aid:
        return os.path.join(AGENTS_DIR, aid, "memory")
    return None
