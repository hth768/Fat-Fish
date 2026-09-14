# -*- coding: utf-8 -*-
"""统一消息抽象层。

让核心智能体（mc_agent / proactive_speaker / scheduler 等）与具体聊天平台解耦。
核心模块只依赖 MessageSender 接口，不直接操作 ws / OneBot 协议。

架构：
┌─────────────────────────────┐
│ 核心智能体（无 QQ 依赖）       │
│  -> 通过 sender 发消息         │
└────────────┬────────────────┘
             │ MessageSender
┌────────────▼────────────────┐
│ 平台适配器（qq_adapter 等）    │
│  -> 实现具体平台协议           │
└─────────────────────────────┘

MessageSender 定义核心模块需要的发送能力。
各平台适配器实现这些接口方法。
"""
import asyncio
import time
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


class MessageSender:
    """消息发送接口。核心模块依赖此接口，不关心底层平台。

    子类（平台适配器）必须实现以下方法。
    """

    def send_private(self, user_id, text: str):
        """发私聊消息。必须在子类实现。"""
        raise NotImplementedError

    def send_group(self, group_id, text: str):
        """发群聊消息。必须在子类实现。"""
        raise NotImplementedError

    def send_text(self, target: str, text: str):
        """通用发文本。target 格式如 'private:123456' 或 'group:123456'。"""
        if target.startswith("private:"):
            return self.send_private(target[len("private:"):], text)
        elif target.startswith("group:"):
            return self.send_group(target[len("group:"):], text)
        raise ValueError(f"无法识别的发送目标: {target}")

    def send_voice_private(self, user_id, wav_path: str):
        """发私聊语音消息（wav 文件路径）。

        语音是可选能力：不支持的平台继承默认实现（告警并返回 False），
        支持语音的平台（如 qq_adapter）覆盖此方法。
        """
        print(f"[SENDER] 当前平台不支持主动语音发送，丢弃: {wav_path}")
        return False

    def send_voice_group(self, group_id, wav_path: str):
        """发群聊语音消息（wav 文件路径）。默认行为同 send_voice_private。"""
        print(f"[SENDER] 当前平台不支持主动语音发送，丢弃: {wav_path}")
        return False

    def send_voice(self, target: str, wav_path: str):
        """通用发语音（主动说话模块用）。

        合成语音文件可复用 chat_service.text_to_voice_wav(text)，
        再把 wav 路径按 target 发出去：sender.send_voice("private:123", wav)。
        """
        if target.startswith("private:"):
            return self.send_voice_private(target[len("private:"):], wav_path)
        elif target.startswith("group:"):
            return self.send_voice_group(target[len("group:"):], wav_path)
        raise ValueError(f"无法识别的发送目标: {target}")


# ---------------------------------------------------------------------------
# 平台无关的消息模型：平台插件负责把自家协议转换成这两个类型
# ---------------------------------------------------------------------------
@dataclass
class InboundMessage:
    """一条收到的消息（平台无关）。

    平台插件把自家协议（OneBot/B站弹幕/直播评论/控制台输入...）转换成此结构，
    交给 ChatService 处理；其中媒体内容以"引用(ref)"形式携带，
    由平台通过 ReplyTarget.fetch_* 按需取回（避免为不回复的消息白白下载）。
    """
    platform: str = ""                 # "qq" / "console" / "bilibili" ...
    channel_type: str = ""             # "private" | "group"
    channel_id: str = ""               # 群号或私聊对方 id
    user_id: str = ""                  # 发送者 id（平台内命名空间，建议跨平台时自行加前缀）
    user_name: str = ""                # 发送者昵称（可选）
    message_id: str = ""
    text: str = ""                     # 纯文本内容
    mentioned: bool = False            # 群聊中是否 @了机器人（私聊恒 True）
    image_refs: List[Any] = field(default_factory=list)   # 图片引用列表
    audio_wav: bytes = b""             # 已解码为 wav 的语音（无语音为空；语音解码是平台职责）
    has_video: bool = False
    video_ref: Any = None              # 视频引用
    quoted_text: str = ""              # 被引用/回复消息的文本（平台已解析）
    quoted_image_refs: List[Any] = field(default_factory=list)
    quoted_sender: str = ""
    quoted_self: bool = False          # 引用的消息是否是机器人自己发的（群聊触发门槛用）
    raw: Dict = field(default_factory=dict)               # 平台原始事件（逃生舱口）


class ReplyTarget:
    """一次会话的回复上下文：ChatService 通过它回复消息，平台插件实现具体发送。

    每条收到的消息对应一个 ReplyTarget 实例。平台插件必须实现 reply()，
    其余按平台能力选实现（不支持语音的平台可保持默认行为）。
    """

    def __init__(self, msg: InboundMessage):
        self.msg = msg
        self.platform = msg.platform

    @property
    def channel_type(self) -> str:
        return self.msg.channel_type

    @property
    def channel_id(self) -> str:
        return self.msg.channel_id

    @property
    def user_id(self) -> str:
        return self.msg.user_id

    def send_target(self) -> str:
        """主动发送用的统一目标格式：'private:123' / 'group:456'。"""
        return f"{self.msg.channel_type}:{self.msg.channel_id}"

    async def reply(self, text: str):
        """回复文本。平台负责解析表情标记、分句、限速等呈现细节。"""
        raise NotImplementedError

    async def reply_voice(self, wav_path: str):
        """发送语音文件。平台不支持时可静默忽略或降级为文本。"""
        raise NotImplementedError

    async def fetch_image(self, ref) -> bytes:
        """取回图片引用的二进制内容。平台不支持时抛异常。"""
        raise NotImplementedError

    async def fetch_video(self, ref) -> str:
        """取回视频引用的本地文件路径。平台不支持时抛异常。"""
        raise NotImplementedError


# ---------------------------------------------------------------------------
# 简单事件总线：让智能体能主动"发事件"，平台适配器决定如何呈现
# ---------------------------------------------------------------------------
class AgentEventBus:
    """智能体事件总线。

    核心智能体可以向总线发布事件（游戏发现、求助、状态变化等），
    平台适配器订阅这些事件并决定如何发送给用户。
    """

    def __init__(self):
        self._handlers: Dict[str, List] = {}

    def on(self, event_type: str, handler):
        """订阅事件。handler 是 async 函数，接收 (event_data: dict)。"""
        self._handlers.setdefault(event_type, []).append(handler)

    async def emit(self, event_type: str, data: dict = None):
        """发布事件。调用所有订阅者。"""
        handlers = self._handlers.get(event_type, [])
        for h in handlers:
            try:
                if asyncio.iscoroutinefunction(h):
                    await h(data or {})
                else:
                    h(data or {})
            except Exception as e:
                print(f"[EVENT-BUS] 事件处理器异常 ({event_type}): {e}")


# ---------------------------------------------------------------------------
# 全局事件总线单例
# ---------------------------------------------------------------------------
_global_bus: Optional[AgentEventBus] = None


def get_event_bus() -> AgentEventBus:
    global _global_bus
    if _global_bus is None:
        _global_bus = AgentEventBus()
    return _global_bus


# ---------------------------------------------------------------------------
# 全局 sender 持有者：核心模块通过 get_sender() 获取当前平台的 sender
# ---------------------------------------------------------------------------
_global_sender: Optional[MessageSender] = None


def set_sender(sender: MessageSender):
    """由平台适配器在初始化时注入全局 sender。"""
    global _global_sender
    _global_sender = sender


def get_sender() -> Optional[MessageSender]:
    """核心模块获取当前平台的 sender。未注入时返回 None。"""
    return _global_sender
