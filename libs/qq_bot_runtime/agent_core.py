# -*- coding: utf-8 -*-
"""智能体核心：整个项目的"主体"，不依赖任何聊天平台。

架构：
                    ┌────────────────────────────────┐
                    │           AgentCore            │
                    │  chat: ChatService（聊天大脑）   │
                    │  plugins: PluginManager         │
                    │  bus: 事件总线                   │
                    └───────┬───────────────┬────────┘
              InboundMessage│               │MessageSender / 事件
        ┌───────────────────▼──┐      ┌─────▼──────────────────┐
        │ 平台插件（可插拔）      │      │ 功能插件（可插拔）        │
        │  - QQPlugin (NapCat) │      │  - 余额监控              │
        │  - ConsolePlugin     │      │  - 主动说话              │
        │  - (未来)B站/直播/...  │      │  - (未来)定时任务/...    │
        └──────────────────────┘      └────────────────────────┘

QQ 只是核心当前接入的一个平台插件。接入新平台只需实现 PlatformPlugin，
核心与聊天大脑（chat_service）零改动。功能扩展同理实现 FeaturePlugin。
「大脑」同理：实现 AgentBrain（brain_base.py）并注册进 AgentCore.brains
（大脑注册表，统一生命周期/状态/事件通道），新增 agent 无需改核心逻辑。
"""
import asyncio
import time
from typing import Dict, List, Optional

import config
import agent_ctx

from brain_base import AgentBrain, BrainManager, brain_event
from message_bus import AgentEventBus, get_event_bus
from plugin_base import FeaturePlugin, PlatformPlugin, PluginManager
from quiet import degrade
from tts_vox import VoxTTSPlugin  # 本地 VoxCPM2 TTS sidecar 管理（轻依赖，无模型加载）


class BalanceMonitorPlugin(FeaturePlugin):
    """DeepSeek 余额监控。"""

    name = "balance_monitor"

    def __init__(self, core):
        super().__init__(core)
        self._task = None

    async def start(self):
        if not getattr(config, "ENABLE_BALANCE_MONITOR", False):
            return
        from scheduler import balance_monitor_loop
        self._task = asyncio.get_event_loop().create_task(balance_monitor_loop())
        print("[CORE] 余额监控已启动")
        await super().start()

    async def stop(self):
        if self._task:
            self._task.cancel()
            self._task = None
        await super().stop()


class ProactiveSpeakerPlugin(FeaturePlugin):
    """主动说话调度器（主动私聊/群聊接话/游戏分享）。"""

    name = "proactive_speaker"

    def __init__(self, core):
        super().__init__(core)
        self._keepalive = None

    async def start(self):
        if not getattr(config, "ENABLE_PROACTIVE_SPEAKER", False):
            return
        from proactive_speaker import get_speaker
        speaker = get_speaker()
        # 发送通道由平台插件注册到 message_bus（无平台时 speaker 自然不发）
        if speaker.start(None):
            self._keepalive = asyncio.get_event_loop().create_task(self._hold())
            print("[CORE] 主动说话调度器已启动")
            await super().start()

    async def _hold(self):
        try:
            while True:
                await asyncio.sleep(3600)
        except asyncio.CancelledError as e:
            degrade("libs/qq_bot_runtime/agent_core.py:86 ProactiveSpeakerPlugin._hold", e, "降级：while True")

    async def stop(self):
        if self._keepalive:
            self._keepalive.cancel()
            self._keepalive = None
        try:
            from proactive_speaker import get_speaker
            get_speaker().stop()
        except Exception as e:
            degrade("agent_core.ProactiveSpeakerPlugin.stop", e, "停主动发言器失败")
        await super().stop()


# ======================================================================
# 内建大脑（注册进 AgentCore.brains 大脑注册表）
# 包装层保持薄：真正的循环/会话/工具都在各自模块里，这里只做统一生命周期
# 与状态接入；懒 import，避免 import 期把 MC/聊天大模块拖进核心加载链。
# ======================================================================

class ChatBrain(AgentBrain):
    """聊天大脑（消息式）：ChatService 处理平台消息，随核心常开。"""

    name = "chat"
    title = "聊天大脑"
    kind = "chat"                      # 消息式：无自主循环，收到消息即处理
    description = "ChatService：处理 QQ/控制台等平台对话，聚合记忆/知识/媒体/MC 注入"
    auto_start_on_core = True

    # start/stop 无额外动作：由平台插件驱动，基类管理 started 标记

    def status(self) -> Dict:
        d = {**super().status()}
        d["platforms"] = [p.platform for p in self.core.plugins.platforms() if p.started]
        return d



class AgentCore:
    """智能体核心控制器。每个智能体对应一个独立 AgentCore 实例。"""

    def __init__(self, agent_id: str = "feiyu"):
        self.agent_id = agent_id
        self.running = False
        self.bus = get_event_bus()
        self.plugins = PluginManager(self)
        # 聊天大脑延迟初始化（避免 import 期副作用影响插件加载）
        self._chat = None
        # 知识库服务延迟初始化（插件经 core.knowledge 访问自主学习闭环）
        self._knowledge = None
        # 大脑注册表延迟初始化（首次访问自动注册内建大脑，见 register_builtin_brains）
        self._brains = None
        self._builtin_brains = False

    @property
    def chat(self):
        if self._chat is None:
            from chat_service import get_chat_service
            self._chat = get_chat_service(agent_id=self.agent_id)
        return self._chat

    @property
    def knowledge(self):
        """知识库服务（全插件共享）：查知识 / 学知识 / 记知识，详见 knowledge_service。"""
        if self._knowledge is None:
            from knowledge_service import get_knowledge
            self._knowledge = get_knowledge()
        return self._knowledge

    @property
    def brains(self) -> BrainManager:
        """核心大脑注册表：所有智能体大脑的统一入口（懒初始化，自动注册内建大脑）。"""
        if self._brains is None:
            self._brains = BrainManager(self)
            self.register_builtin_brains()
        return self._brains

    def register_builtin_brains(self):
        """注册内建大脑（幂等）。注册 ≠ 启动：auto_start_on_core 决定是否随核心自启。

        新增内建 agent：实现 AgentBrain（brain_base.py）后在此加一行注册；
        游戏类大脑（mc/pc/pvz）已随插件分发，由插件包 create_brain 注册，
        自动获得统一生命周期/状态查询/事件通道。详见 ARCHITECTURE.md。
        """
        if self._builtin_brains:
            return
        self._builtin_brains = True
        if self._brains is None:
            self._brains = BrainManager(self)
        m = self._brains
        m.register(ChatBrain(self))
        # mc_mod/mc_bot/pc/pvz 大脑已随插件分发（brain_* 插件包自带大脑类），
        # 由插件装载（pkg_manager.attach_to -> create_brain）时注册，此处不再内建。
    # ---- 插件注册 ----
    def register_builtin_plugins(self, platforms: Optional[List[str]] = None):
        """按配置注册内置插件。platforms 为 None 时按 config 决定。

        platforms: 指定启用的平台列表，如 ["qq"] / ["console"] / ["qq","console"]

        注册来源统一为 plugin_registry.SPECS（唯一事实来源）：清单给出「模块/类/开关」，
        这里只负责按开关实例化。新增插件=往清单加一条 + 类上声明 name/version，本函数无需改。
        """
        platforms = platforms if platforms is not None else self._platforms_from_config()

        try:
            import plugin_registry as reg
        except Exception as e:   # 注册表不可用则退化为原硬编码路径，保证核心仍能起
            print(f"[CORE][WARN] 插件注册表不可用({e})，降级为内置注册")
            self._register_builtin_plugins_fallback(platforms)
            return

        # 平台插件：以 platforms 参数为唯一依据（显式请求即注册，保持历史语义）。
        # 开关只用于 _platforms_from_config() 推导默认 platforms，不在此处二次过滤。
        requested = set(platforms)
        for spec in reg.by_kind("platform"):
            if spec.name in requested:
                self._instance_from_spec(spec, force=True)

        # 功能插件：由清单开关决定（插件内部仍保留自管开关，双重保险）
        for spec in reg.enabled_specs("feature"):
            self._instance_from_spec(spec)

    def _instance_from_spec(self, spec, force: bool = False):
        """按清单条目实例化并注册一个插件（import 失败降级为告警，不拖垮核心）。"""
        if not force and not spec.enabled():
            return None
        if self.plugins.get(spec.name) is not None:
            return self.plugins.get(spec.name)
        try:
            mod = __import__(spec.module, fromlist=[spec.cls or "*"])
            cls = getattr(mod, spec.cls)
            if spec.name == "web":   # WebPlugin 需要端口参数
                import os as _os
                port = int(_os.environ.get("WEB_PORT",
                                           getattr(config, "WEB_PLUGIN_PORT", 8800)))
                inst = cls(self, port=port)
            else:
                inst = cls(self)
            return self.plugins.register(inst)
        except Exception as e:
            print(f"[CORE][WARN] 插件 {spec.name} 注册失败: {e}")
            return None

    def _register_builtin_plugins_fallback(self, platforms: List[str]):
        """注册表不可用时的兜底注册路径（与历史硬编码行为一致）。"""
        if "qq" in platforms:
            from qq_plugin import QQPlugin
            self.plugins.register(QQPlugin(self))
        if "console" in platforms:
            from console_plugin import ConsolePlugin
            self.plugins.register(ConsolePlugin(self))
        if "bilibili" in platforms:
            from bilibili_plugin import BilibiliPlugin
            self.plugins.register(BilibiliPlugin(self))
        if "web" in platforms:
            import os as _os
            from web_plugin import WebPlugin
            _web_port = int(_os.environ.get("WEB_PORT", getattr(config, "WEB_PLUGIN_PORT", 8800)))
            self.plugins.register(WebPlugin(self, port=_web_port))

        self.plugins.register(BalanceMonitorPlugin(self))
        self.plugins.register(ProactiveSpeakerPlugin(self))
        self.plugins.register(VoxTTSPlugin(self))
        from bili_dm import BilibiliDmPlugin
        self.plugins.register(BilibiliDmPlugin(self))
        from bili_learn_scheduler import BilibiliLearnScheduler
        self.plugins.register(BilibiliLearnScheduler(self))

    @staticmethod
    def _platforms_from_config() -> List[str]:
        platforms = []
        if getattr(config, "ENABLE_QQ_PLUGIN", True):
            platforms.append("qq")
        if getattr(config, "ENABLE_CONSOLE_PLUGIN", False):
            platforms.append("console")
        if getattr(config, "ENABLE_BILIBILI_PLUGIN", False):
            platforms.append("bilibili")
        if getattr(config, "ENABLE_WEB_PLUGIN", False):
            platforms.append("web")
        return platforms

    # ---- 生命周期 ----
    async def start(self):
        """启动核心：拉起全部插件（平台 + 功能）。"""
        if self.running:
            return True
        self.running = True
        # 注册表自检：清单与已注册插件是否一致（版本/依赖/漂移），发现问题仅告警不阻塞
        try:
            self.plugins.validate_registry(log=True, platforms=self._platforms_from_config())
        except Exception as e:
            print(f"[CORE][WARN] 注册表校验异常: {e}")
        await self.plugins.start_all()
        # 大脑注册表随核心启动（只自动拉起 auto_start_on_core=True 的大脑）
        await self.brains.start_all()
        # 转发自主大脑求助请求：统一上 brain.event 总线，proactive 关闭时兜底私聊主人
        self.plugins.track_task(self._help_request_forwarder())
        print("[CORE] 智能体核心已启动，插件：",
              ", ".join(p.name for p in self.plugins.all()))
        return True

    async def shutdown(self):
        """停止核心与全部插件。"""
        self.running = False
        await self.plugins.cancel_tasks()
        # 大脑注册表统一停止（含手动启动的 mc_mod 大脑）
        await self.brains.stop_all()
        await self.plugins.stop_all()
        try:
            from mc_watcher import mc_watcher
            if mc_watcher.is_running():
                mc_watcher.stop()
        except Exception as e:
            degrade("agent_core.AgentCore.shutdown.mc_watcher", e, "停 MC 监听器失败")
        print("[CORE] 智能体核心已停止")

    # ---- 大脑求助统一通道（brain.event + 主人兜底私聊） ----
    def _proactive_speaker_on(self) -> bool:
        """主动说话调度器是否在跑：它在跑时由它负责求助私聊，核心不重复发。"""
        p = self.plugins.get("proactive_speaker")
        return bool(p and p.started)

    async def _dm_owner_help(self, reqs: List[str]):
        """把大脑求助兜底私聊主人（无平台 sender 或未配主人时静默丢弃）。"""
        from message_bus import get_sender
        sender = get_sender()
        owner = str(getattr(config, "PROACTIVE_PRIVATE_USER_ID", "") or "").strip()
        if not sender or not owner:
            return
        for m in reqs:
            try:
                await sender.send_private(owner, m)
                print(f"[CORE] 大脑求助兜底私聊主人: {str(m)[:40]}")
            except Exception as e:
                print(f"[CORE] 大脑求助私聊失败: {e}")

    async def _help_request_forwarder(self):
        """把自主大脑的求助转发到统一通道：brain.event 总线 + proactive 关闭时兜底私聊主人。"""
        last_dm = 0.0
        while True:
            try:
                await asyncio.sleep(10)
                if not self.running:
                    break
                try:
                    from mc_agent import get_agent
                    reqs = get_agent().pop_help_requests()
                except Exception:
                    reqs = []
                if not reqs:
                    continue
                for m in reqs:
                    await brain_event(self, "mc_mod", "help", m)
                if self._proactive_speaker_on():
                    continue            # 主动说话调度器会私聊主人，避免重复
                now = time.time()
                if now - last_dm < 30.0:   # 简单频控，防刷屏
                    continue
                last_dm = now
                await self._dm_owner_help(reqs)
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"[CORE] 求助转发失败: {e}")

    # ---- 兼容旧接口 ----
    def start_blocking_compat(self) -> bool:
        """同步环境的兼容启动（旧 run_agent 调用方式）。"""
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self.start())
            return True
        except RuntimeError:
            asyncio.run(self.start())
            return True

    # ---- 手动触发闲聊 ----
    async def trigger_casual(self) -> str:
        from proactive_speaker import get_speaker
        return await get_speaker().trigger_casual()

    def status(self) -> Dict:
        """核心状态（插件列表 + 大脑注册表 + 统一注册表清单/校验）。"""
        st = {
            "running": self.running,
            "plugins": [p.status() for p in self.plugins.all()],
            "brains": [b.status() for b in self.brains.all()],
        }
        # 统一注册表：完整清单（含未启用的）+ 校验结果，供 /插件 与 Web 面板展示
        try:
            import plugin_registry as reg
            st["registry"] = reg.to_dict()
            st["registry_report"] = reg.validate()
        except Exception as e:
            st["registry"] = []
            st["registry_report"] = {"ok": False, "errors": [f"注册表不可用: {e}"], "warnings": []}
        return st


# 按智能体分桶的单例：{ agent_id: AgentCore }
_core_instances: dict = {}


def get_core(agent_id: str = None) -> AgentCore:
    aid = agent_id or agent_ctx.current_agent() or "feiyu"
    if aid not in _core_instances:
        _core_instances[aid] = AgentCore(agent_id=aid)
    return _core_instances[aid]
