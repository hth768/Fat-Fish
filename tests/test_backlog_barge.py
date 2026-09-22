"""会话调度器测试：积压消息合并回复 + 插嘴打断。

通过替换真实管线 ``ChatService._handle_message`` 为可控协程，
专注验证调度层（串行队列 / 合并窗口 / barge_in 取消当前生成）的行为，
不依赖 LLM / 记忆等重依赖。
"""
import asyncio
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

# 让仓库根进入 import 路径（以便 import libs.qq_bot_runtime.chat_service）
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
# chat_service 内部 `import config` 等绝对导入依赖引擎运行时目录
ENGINE = os.path.join(ROOT, "libs", "qq_bot_runtime")
if ENGINE not in sys.path:
    sys.path.insert(0, ENGINE)

import libs.qq_bot_runtime.chat_service as cs_mod
from libs.qq_bot_runtime.message_bus import InboundMessage, ReplyTarget
from libs.qq_bot_runtime.chat_service import ChatService


class _RecReply(ReplyTarget):
    """记录所有回复的假 ReplyTarget。"""

    def __init__(self):
        self.replies = []

    async def reply(self, text, **kw):
        self.replies.append(text)


def _msg(text, **kw):
    return InboundMessage(
        platform="app", channel_type="private",
        channel_id="u1", user_id="u1", text=text, **kw)


class BacklogBargeTest(unittest.TestCase):

    def setUp(self):
        # 隔离重依赖，仅构造对象骨架
        with patch.object(cs_mod, "get_llm", return_value=MagicMock()), \
             patch.object(cs_mod, "get_vision", return_value=MagicMock()), \
             patch.object(cs_mod, "SessionManagerAdapter", return_value=MagicMock()):
            self.svc = ChatService(agent_id="test")
        self.reply = _RecReply()
        self.calls = []      # 被处理的消息 text（按处理顺序）
        self.cancelled = []  # 被插嘴取消的消息 text

    def _install_handler(self, delay=0.0):
        """用可控 handler 替换 _handle_message；delay>0 时进入 await（可被取消）。"""
        calls = self.calls
        cancelled = self.cancelled

        async def handler(msg, reply):
            calls.append(msg.text)
            if delay:
                try:
                    await asyncio.sleep(delay)
                except asyncio.CancelledError:
                    cancelled.append(msg.text)
                    raise
        self.svc._handle_message = handler
        return handler

    def test_backlog_merge(self):
        """连发多条普通消息，默认合并为 1 次处理且保留全部内容（顺序）。"""
        self._install_handler()
        async def run():
            await asyncio.gather(
                self.svc.handle_message(_msg("甲"), self.reply),
                self.svc.handle_message(_msg("乙"), self.reply),
                self.svc.handle_message(_msg("丙"), self.reply),
            )
        asyncio.run(run())
        self.assertEqual(len(self.calls), 1, "应合并为一次处理")
        merged = self.calls[0]
        self.assertIn("甲", merged)
        self.assertIn("乙", merged)
        self.assertIn("丙", merged)

    def test_backlog_serial_no_merge(self):
        """关闭合并时，连发消息逐条串行处理，且顺序即 FIFO。"""
        self._install_handler()
        async def run():
            await asyncio.gather(
                self.svc.handle_message(_msg("一"), self.reply),
                self.svc.handle_message(_msg("二"), self.reply),
                self.svc.handle_message(_msg("三"), self.reply),
            )
        with patch.object(cs_mod.config, "BACKLOG_MERGE", False, create=True):
            asyncio.run(run())
        self.assertEqual(self.calls, ["一", "二", "三"])

    def test_barge_cancels_current(self):
        """coalesce=0 让当前消息立即开始生成；插嘴应取消它并优先处理插嘴。"""
        self._install_handler(delay=5.0)
        async def run():
            t1 = asyncio.ensure_future(self.svc.handle_message(_msg("慢任务"), self.reply))
            await asyncio.sleep(0.2)  # 等慢任务进入 await（current 已建立）
            t2 = asyncio.ensure_future(
                self.svc.handle_message(_msg("插嘴！", barge_in=True), self.reply))
            await asyncio.gather(t1, t2)
        with patch.object(cs_mod.config, "BACKLOG_COALESCE_MS", 0, create=True):
            asyncio.run(run())
        self.assertIn("慢任务", self.cancelled, "当前生成应被插嘴取消")
        self.assertIn("插嘴！", self.calls, "插嘴消息应被处理")

    def test_barge_command_normalize(self):
        """`/插嘴 内容` 应归一为 barge_in 消息，并剥离命令前缀、打断慢任务。"""
        self._install_handler(delay=5.0)
        async def run():
            t1 = asyncio.ensure_future(self.svc.handle_message(_msg("慢任务"), self.reply))
            await asyncio.sleep(0.2)
            t2 = asyncio.ensure_future(self.svc.handle_message(_msg("/插嘴 关灯"), self.reply))
            await asyncio.gather(t1, t2)
        with patch.object(cs_mod.config, "BACKLOG_COALESCE_MS", 0, create=True):
            asyncio.run(run())
        self.assertIn("慢任务", self.cancelled)
        self.assertIn("关灯", self.calls)
        self.assertNotIn("/插嘴 关灯", self.calls)


if __name__ == "__main__":
    unittest.main()
