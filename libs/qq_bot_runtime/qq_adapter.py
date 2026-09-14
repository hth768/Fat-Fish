# -*- coding: utf-8 -*-
"""QQ 平台适配器：对接 OneBot v11（NapCat 反向 WebSocket）。

实现 message_bus.MessageSender 接口，把智能体的消息请求转换为
OneBot v11 的 send_msg 协议发送到 QQ。

这是"QQ 聊天"作为一个功能模块的实现。将来要换其他平台（Telegram/Web/本地），
只需要新增一个实现 MessageSender 的适配器，核心智能体代码不用改。
"""
import asyncio
import json
import time
from typing import Optional

from message_bus import MessageSender


class QQAdapter(MessageSender):
    """OneBot v11 适配器。

    持有 ws 连接，实现 send_private / send_group。
    """

    def __init__(self, ws=None):
        self._ws = ws
        self._send_lock = asyncio.Lock()

    # ---- ws 绑定 ----
    def set_ws(self, ws):
        """设置/更新 WebSocket 连接。"""
        self._ws = ws

    @property
    def is_connected(self) -> bool:
        return self._ws is not None

    # ---- 发送实现 ----
    def _build_payload(self, message_type: str, target_id, segments: list, echo: str) -> dict:
        """构造 OneBot v11 send_msg 请求。segments 为 OneBot 消息段列表（text/record 等）。"""
        return {
            "action": "send_msg",
            "params": {
                "message_type": message_type,
                "user_id" if message_type == "private" else "group_id": str(target_id),
                "message": segments,
            },
            "echo": echo,
        }

    async def _send_raw(self, payload: dict) -> bool:
        """发送原始 OneBot 消息。失败返回 False。"""
        if not self._ws:
            return False
        try:
            async with self._send_lock:
                await self._ws.send(json.dumps(payload, ensure_ascii=False))
            return True
        except Exception as e:
            print(f"[QQ-ADAPTER] 发送失败: {e}")
            return False

    @staticmethod
    def _text_segment(text: str) -> list:
        return [{"type": "text", "data": {"text": text}}]

    @staticmethod
    def _voice_segment(wav_path: str) -> list:
        return [{"type": "record", "data": {"file": wav_path}}]

    async def send_private(self, user_id, text: str) -> bool:
        """发私聊消息。"""
        payload = self._build_payload(
            "private", user_id, self._text_segment(text),
            f"feiyu-private-{int(time.time() * 1000)}")
        return await self._send_raw(payload)

    async def send_group(self, group_id, text: str) -> bool:
        """发群聊消息。"""
        payload = self._build_payload(
            "group", group_id, self._text_segment(text),
            f"feiyu-group-{int(time.time() * 1000)}")
        return await self._send_raw(payload)

    async def send_voice_private(self, user_id, wav_path: str) -> bool:
        """发私聊语音消息（record 段，wav 本地路径）。"""
        payload = self._build_payload(
            "private", user_id, self._voice_segment(wav_path),
            f"feiyu-voice-private-{int(time.time() * 1000)}")
        return await self._send_raw(payload)

    async def send_voice_group(self, group_id, wav_path: str) -> bool:
        """发群聊语音消息（record 段，wav 本地路径）。"""
        payload = self._build_payload(
            "group", group_id, self._voice_segment(wav_path),
            f"feiyu-voice-group-{int(time.time() * 1000)}")
        return await self._send_raw(payload)


# ---------------------------------------------------------------------------
# 便捷：在核心模块里通过 message_bus.get_sender() 拿到的就是这个适配器实例
# ---------------------------------------------------------------------------

_default_adapter: Optional[QQAdapter] = None


def get_qq_adapter() -> QQAdapter:
    """获取 QQ 适配器单例。"""
    global _default_adapter
    if _default_adapter is None:
        _default_adapter = QQAdapter()
    return _default_adapter
