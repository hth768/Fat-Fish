"""
多智能体运行时（AgentRuntime）。

设计（按需求）：
- 每个智能体 = 一个独立的 AgentCore 实例（互不影响）。
- 惰性启动：智能体**不随程序自启**，只有用户「启用/选中对话/命中绑定」时才拉起其核心。
- 平台消息按 agents/<id>/agent.json 的 bindings 路由到对应智能体的核心。

本模块是核心与平台/UI 之间的调度中枢：
    runtime.submit(platform, channel_type, channel_id, msg, reply)
    runtime.ensure(agent_id) -> AgentCore   # 惰性启动
    runtime.match(platform, channel_type, channel_id) -> agent_id
"""
import asyncio
from typing import Dict, Optional

import agent_manager
import agent_ctx
from agent_core import AgentCore, get_core


class AgentRuntime:
    def __init__(self, default_agent_id: str = "feiyu"):
        self.default_agent_id = default_agent_id
        self.cores: Dict[str, AgentCore] = {}      # 已启动的智能体核心
        self._starting: Dict[str, asyncio.Task] = {}  # 进行中的启动任务（防并发重复启动）

    # ---- 路由 ----
    def match(self, platform: str, channel_type: str, channel_id: str) -> str:
        """依据 bindings 把 (平台, 频道) 匹配到智能体 id；无匹配返回默认智能体。"""
        hit = agent_manager.match_binding(platform, channel_type, channel_id)
        return hit or self.default_agent_id

    # ---- 惰性启停 ----
    async def ensure(self, agent_id: str) -> AgentCore:
        """确保某智能体核心已启动并返回；并发安全。"""
        if agent_id in self.cores:
            return self.cores[agent_id]
        if agent_id in self._starting:
            return await self._starting[agent_id]

        async def _start():
            core = get_core(agent_id=agent_id)
            if not core.running:
                try:
                    await core.start()
                except Exception as e:
                    print(f"[RUNTIME][WARN] 智能体 {agent_id} 启动失败: {e}")
                    raise
            self.cores[agent_id] = core
            self._starting.pop(agent_id, None)
            return core

        task = asyncio.ensure_future(_start())
        self._starting[agent_id] = task
        try:
            return await task
        finally:
            self._starting.pop(agent_id, None)

    async def stop(self, agent_id: str) -> None:
        """停止某智能体核心（释放资源）。"""
        core = self.cores.pop(agent_id, None)
        if core is not None and core.running:
            try:
                await core.stop()
            except Exception as e:
                print(f"[RUNTIME][WARN] 智能体 {agent_id} 停止异常: {e}")

    def is_running(self, agent_id: str) -> bool:
        return agent_id in self.cores

    # ---- 统一入口 ----
    async def submit(self, platform: str, channel_type: str, channel_id: str,
                     msg, reply) -> Optional[AgentCore]:
        """按绑定路由到对应智能体核心并提交消息；返回实际处理的核心。"""
        aid = self.match(platform, channel_type, channel_id)
        # 设置上下文，使核心内部的记忆/人设落入该智能体命名空间
        token = agent_ctx.set_agent(aid)
        try:
            core = await self.ensure(aid)
            await core.chat.handle_message(msg, reply)
            return core
        finally:
            agent_ctx.reset_agent(token)


# 全局运行时单例（供 bridge / 平台插件调用）
_runtime: Optional[AgentRuntime] = None


def get_runtime() -> AgentRuntime:
    global _runtime
    if _runtime is None:
        _runtime = AgentRuntime(default_agent_id=agent_manager.DEFAULT_AGENT_ID)
    return _runtime
