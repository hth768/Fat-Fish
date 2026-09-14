# -*- coding: utf-8 -*-
"""插件系统基类与管理器。

架构总览：
                       ┌────────────────────────────┐
                       │        AgentCore           │
                       │  chat_service（聊天大脑）    │
                       │  事件总线 / 后台服务          │
                       └──────────┬─────────────────┘
                                  │ InboundMessage / ReplyTarget
              ┌───────────────────┼────────────────────┐
        ┌───────▼──────┐    ┌───────▼──────┐    ┌────────▼───────┐
        │   QQ 插件     │    │  控制台插件   │    │  B站弹幕插件    │
        │ (NapCat/WS)  │    │  (终端输入)   │    │ (直播弹幕 wss)  │
        └──────────────┘    └──────────────┘    └────────────────┘

两类插件：
- PlatformPlugin（平台插件）：负责把某个聊天平台接入核心。
  职责：协议转换（自家事件 -> InboundMessage）、消息呈现（ReplyTarget 实现）、
  主动发送（MessageSender 实现）。参考 console_plugin.py（最简）和 qq_plugin.py（完整）。
- FeaturePlugin（功能插件）：核心后台能力的生命周期封装（余额监控、主动说话等）。

如何接入一个新平台（B 站 bilibili_plugin.py 是完整参考，console_plugin.py 最简）：
  1. 新建 xxx_plugin.py，继承 PlatformPlugin；
  2. start() 里建立到平台的连接，把收到的消息转成 InboundMessage；
  3. 实现 ReplyTarget（reply: 平台自己的回话方式）与 MessageSender；
  4. 在 AgentCore.register_builtin_plugins() 里按配置注册，或在 main.py 里注册。
核心聊天逻辑（chat_service）一行都不用改。
"""
import asyncio
from typing import Dict, List, Optional

from message_bus import InboundMessage, ReplyTarget


class Plugin:
    """插件基类：最小生命周期契约。"""

    name = "base"

    # 版本与依赖声明（plugin_registry 为唯一事实来源；此处镜像一份便于运行时自省）
    version = "1.0.0"
    requires: Dict[str, str] = {}          # 依赖名 -> 版本约束，如 {"bilibili": ">=1.0"}
    optional_requires: List[str] = []      # 软依赖：缺失只告警

    def __init__(self, core):
        self.core = core          # AgentCore 引用，插件通过它访问 chat / bus / 其它插件
        self._started = False

    async def start(self):
        """启动插件（连接平台、拉起后台任务等）。"""
        self._started = True

    async def stop(self):
        """停止插件并释放资源。"""
        self._started = False

    @property
    def started(self) -> bool:
        return self._started

    def status(self) -> Dict:
        """插件状态（供 /插件 等命令展示）。"""
        return {"name": self.name, "version": self.version,
                "running": self._started, "requires": dict(self.requires or {})}


class PlatformPlugin(Plugin):
    """平台插件基类：接入一个聊天平台。"""

    platform = "base"

    # 平台能力声明：chat_service 可据此降级（如无语音的平台不生成语音回复）
    capabilities = {
        "group": False,       # 是否支持群聊/多人群聊场景
        "voice": False,       # 是否支持发送语音
        "image": False,       # 是否支持发送图片（表情包）
        "voice_input": False, # 是否支持接收语音
        "video_input": False, # 是否支持接收视频
    }

    def status(self) -> Dict:
        return {**super().status(), "platform": self.platform, "capabilities": dict(self.capabilities)}


class FeaturePlugin(Plugin):
    """功能插件基类：核心后台能力（非聊天平台）。"""


class PluginManager:
    """插件注册表与生命周期管理。"""

    def __init__(self, core):
        self.core = core
        self._plugins: Dict[str, Plugin] = {}
        self._tasks: List = []

    # ---- 注册 ----
    def register(self, plugin: Plugin) -> Plugin:
        self._plugins[plugin.name] = plugin
        return plugin

    def get(self, name: str) -> Optional[Plugin]:
        return self._plugins.get(name)

    def all(self) -> List[Plugin]:
        return list(self._plugins.values())

    def platforms(self) -> List[PlatformPlugin]:
        return [p for p in self._plugins.values() if isinstance(p, PlatformPlugin)]

    # ---- 生命周期 ----
    async def start_all(self):
        for plugin in self._plugins.values():
            try:
                await plugin.start()
                if plugin.started:
                    print(f"[PLUGINS] 插件已启动: {plugin.name}")
                else:
                    print(f"[PLUGINS] 插件未启用，跳过: {plugin.name}")
            except Exception as e:
                print(f"[PLUGINS] 插件启动失败 {plugin.name}: {e}")

    async def stop_all(self):
        for plugin in reversed(list(self._plugins.values())):
            try:
                await plugin.stop()
            except Exception as e:
                print(f"[PLUGINS] 插件停止失败 {plugin.name}: {e}")

    def track_task(self, coro):
        """登记一个跟随核心生命周期的后台任务。"""
        task = asyncio.get_event_loop().create_task(coro)
        self._tasks.append(task)
        return task

    async def cancel_tasks(self):
        for t in self._tasks:
            t.cancel()
        self._tasks = []

    # ---- 注册表集成 ----
    def validate_registry(self, log: bool = True, platforms=None) -> Dict:
        """按统一注册表（plugin_registry）校验已注册插件，返回校验报告。

        校验「已注册实例」而非静态清单：能发现「清单要求 X 但实际没注册」这类漂移。

        platforms: 本次实际请求启用的平台列表（与 register_builtin_plugins 的入参一致）。
                   为 None 时不做「平台是否漏注册」判定——因为平台还受显式请求约束，
                   用 config 全局开关判断会把「本来就没请求的平台」误报成漂移。
        """
        try:
            import plugin_registry as reg
        except Exception as e:
            return {"ok": True, "errors": [], "warnings": [f"注册表不可用: {e}"]}

        report = reg.validate()
        errors = list(report.get("errors", []))
        warnings = list(report.get("warnings", []))
        registered = {p.name for p in self._plugins.values()}

        # 功能插件：清单开关启用却没注册 → 漂移（error）
        for spec in reg.enabled_specs("feature"):
            if spec.name not in registered:
                errors.append(
                    f"[ERR] {spec.name}: 清单标记启用（{spec.switch}）但未注册进 PluginManager")

        # 平台插件：只在「明确请求了该平台」时才判定漏注册
        if platforms is not None:
            requested = set(platforms)
            for spec in reg.by_kind("platform"):
                if spec.name in requested and spec.name not in registered:
                    errors.append(
                        f"[ERR] {spec.name}: 平台已请求启用但未注册进 PluginManager")

        # 已注册但不在清单 → 未登记的野插件（warning）
        known = {s.name for s in reg.SPECS}
        for name in registered - known:
            warnings.append(f"[WARN] {name}: 已注册但未在 plugin_registry 登记")

        report["ok"] = not errors
        report["errors"] = errors
        report["warnings"] = warnings
        report["registered"] = sorted(registered)
        if log:
            for line in errors:
                print(f"[REGISTRY] {line}")
            for line in warnings:
                print(f"[REGISTRY] {line}")
            if not errors and not warnings:
                print("[REGISTRY] 校验通过：清单与已注册插件一致")
        return report
