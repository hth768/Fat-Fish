# -*- coding: utf-8 -*-
"""核心大脑框架：AgentBrain 接口 + BrainManager 注册表 + 统一大脑事件。

把「会自己思考/回应」的智能体单元（大脑）变成核心的一等公民：
- 聊天大脑（消息式，收到消息即回应，无自主循环）
- 自主循环大脑（回合制，start 后自己在循环里观察→决策→执行）
- 外部大脑（独立进程驱动，核心只注册它的状态/事件通道）

每个大脑实现 AgentBrain 并注册进 AgentCore.brains 后，自动获得：
统一生命周期、统一状态查询（AgentCore.status / /大脑）、
统一事件通道（brain.event，求助/播报/状态，平台与功能插件可订阅）。

未来接入新大脑三步走（详见 ARCHITECTURE.md「核心大脑（大脑注册表）」）：

    from brain_base import AgentBrain

    class MyBrain(AgentBrain):
        name = "my_agent"
        title = "我的新智能体"
        kind = "autonomous"          # chat | autonomous | external
        description = "..."
        async def start(self): ...   # 需要随核心自启时设 auto_start_on_core = True
        async def stop(self): ...
        def status(self): return {**super().status(), ...}

    # agent_core.register_builtin_brains() 里加一行：
    #     m.register(MyBrain(self))

统一大脑事件（发到核心事件总线）：
    await brain_event(core, "mc_mod", "help", "遇到问题了，主人帮帮我")
    kind: help   = 求助/求教（核心会兜底私聊主人，见 agent_core 转发策略）
          notice = 重要事件播报（掉线/低血/里程碑等，播报方各自限速）
          state  = 状态变化（可触发平台主动同步展示）
"""
import time
from typing import Dict, List, Optional

from plugin_base import Plugin, PluginManager

# ---- 统一大脑事件 ----
BRAIN_EVENT_TYPE = "brain.event"     # 总线事件名
EVENT_HELP = "help"                  # 求助/求教
EVENT_NOTICE = "notice"              # 重要事件播报
EVENT_STATE = "state"                # 状态变化


async def brain_event(core, source: str, kind: str, text: str):
    """把某个大脑的事件发布到统一通道 brain.event。

    data = {"source": 大脑 name, "kind": kind, "text": text, "ts": 时间戳}
    订阅方：核心内的兜底转发（help）、平台/功能插件按需呈现。
    """
    await core.bus.emit(BRAIN_EVENT_TYPE, {
        "source": str(source),
        "kind": str(kind),
        "text": str(text),
        "ts": time.time(),
    })


class AgentBrain(Plugin):
    """大脑基类：一个会自己思考/回应的智能体单元。

    复用插件生命周期契约（core 引用 / started / start / stop / status），
    语义上比功能插件更靠近「智能体主体」。会话上下文各自私有，
    共享的是核心服务：记忆/知识/事件/状态通道。
    """

    title = ""                       # 展示名（如 "聊天大脑"）
    kind = "autonomous"              # "chat" 消息式 | "autonomous" 自主循环 | "external" 独立进程
    description = ""                 # 一句话说明（/大脑 展示用）
    auto_start_on_core = True        # True: 随核心启动自动 start；False: 注册但不自动拉起

    def status(self) -> Dict:
        """大脑状态（供 /大脑、AgentCore.status 展示），子类可扩展字段。"""
        return {
            **super().status(),
            "title": self.title,
            "kind": self.kind,
            "description": self.description,
        }


class BrainManager(PluginManager):
    """大脑注册表与生命周期管理（核心大脑入口，与插件注册表同构）。

    用法：
        core.brains.get("mc_mod").start()   # 按需启动某个大脑
        core.brains.all()                   # 全部大脑（含状态）
        core.brains.start_all()             # 只启动 auto_start_on_core=True 的大脑
    """

    def brains(self, kind: Optional[str] = None) -> List[AgentBrain]:
        """按形态过滤大脑：kind ∈ "chat" / "autonomous" / "external"，None=全部。"""
        out = [b for b in self.all() if not kind or b.kind == kind]
        return out

    def statuses(self) -> List[Dict]:
        return [b.status() for b in self.all()]

    async def start_all(self):
        """随核心启动：只拉起 auto_start_on_core=True 的大脑（其余注册待命）。"""
        for b in self.all():
            try:
                if not b.auto_start_on_core:
                    print(f"[BRAINS] 大脑已注册、不随核心自启（按需启动）: {b.name}")
                    continue
                await b.start()
                if b.started:
                    print(f"[BRAINS] 大脑已启动: {b.name}")
                else:
                    print(f"[BRAINS] 大脑未启动: {b.name}")
            except Exception as e:
                print(f"[BRAINS] 大脑启动失败 {b.name}: {e}")

    async def stop_all(self):
        """随核心关闭：停掉全部注册的大脑（含手动启动的）。"""
        for b in reversed(self.all()):
            try:
                await b.stop()
            except Exception as e:
                print(f"[BRAINS] 大脑停止失败 {b.name}: {e}")
