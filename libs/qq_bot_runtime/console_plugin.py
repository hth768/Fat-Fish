# -*- coding: utf-8 -*-
"""控制台平台插件：在终端里直接和智能体聊天，不依赖 QQ。

这是新平台接入的最小参考实现（约 100 行）——对照它就知道接入
B 站 / 直播弹幕 / Telegram 需要实现什么：
  1. start()：建立到平台的连接（这里是 stdin 读取循环）；
  2. 把平台消息转成 InboundMessage；
  3. 实现 ReplyTarget.reply()（这里是 print）；
  4. 实现 MessageSender（供主动说话等核心模块主动推送）。

用法：python main.py --console   或在 config.py 设 ENABLE_CONSOLE_PLUGIN = True
"""
import asyncio
import sys

from message_bus import InboundMessage, MessageSender, ReplyTarget
from plugin_base import PlatformPlugin


class ConsoleSender(MessageSender):
    """把主动发送（余额提醒/主动说话等）打印到终端。"""

    async def send_private(self, user_id, text: str) -> bool:
        print(f"\n[智能体 → 你] {text}\n你> ", end="", flush=True)
        return True

    async def send_group(self, group_id, text: str) -> bool:
        print(f"\n[智能体 → 群{group_id}] {text}\n你> ", end="", flush=True)
        return True


class ConsoleReplyTarget(ReplyTarget):
    """终端回复：直接打印。"""

    async def reply(self, text: str):
        print(f"\n智能体> {text}\n你> ", end="", flush=True)

    async def reply_voice(self, wav_path: str):
        print(f"\n智能体> [语音回复已生成: {wav_path}]\n你> ", end="", flush=True)

    async def fetch_image(self, ref) -> bytes:
        # 控制台读不到图片，读本地文件兜底（ref 就是文件路径）
        path = str(ref or "")
        if path and path != "console:image" and __import__("os").path.exists(path):
            with open(path, "rb") as f:
                return f.read()
        raise RuntimeError("控制台平台不支持接收图片")

    async def fetch_video(self, ref) -> str:
        raise RuntimeError("控制台平台不支持接收视频")


class ConsolePlugin(PlatformPlugin):
    """终端聊天平台插件。"""

    name = "console"
    platform = "console"
    capabilities = {"group": False, "voice": False, "image": False,
                    "voice_input": False, "video_input": False}

    def __init__(self, core):
        super().__init__(core)
        self._task = None

    async def start(self):
        from message_bus import set_sender
        set_sender(ConsoleSender())
        self._task = asyncio.get_event_loop().create_task(self._input_loop())
        print("=" * 54)
        print(" 控制台聊天已启动（直接输入文字和智能体对话）")
        print("  - /帮助 退出 /quit   其余斜杠命令与 QQ 端一致")
        print("=" * 54)
        print("你> ", end="", flush=True)
        await super().start()

    async def stop(self):
        if self._task:
            self._task.cancel()
            self._task = None
        await super().stop()

    async def _input_loop(self):
        """stdin 读取循环：一行一条消息。"""
        loop = asyncio.get_event_loop()
        while self.started:
            try:
                line = await loop.run_in_executor(None, sys.stdin.readline)
            except Exception:
                break
            if not line:
                # EOF（Ctrl+Z / Ctrl+D）
                await asyncio.sleep(0.2)
                continue
            text = line.strip()
            if not text:
                print("你> ", end="", flush=True)
                continue
            if text in {"/quit", "/exit", "/退出", "quit", "exit"}:
                print("[CONSOLE] 收到退出指令")
                asyncio.get_event_loop().create_task(self.core.shutdown())
                break
            msg = InboundMessage(
                platform=self.platform,
                channel_type="private",
                channel_id="console_user",
                user_id="console_user",
                user_name="终端用户",
                message_id=f"console-{asyncio.get_event_loop().time()}",
                text=text,
                mentioned=True,
                raw={"line": text},
            )
            reply = ConsoleReplyTarget(self, msg)
            try:
                await self.core.chat.handle_message(msg, reply)
            except Exception as e:
                print(f"\n[CONSOLE] 处理失败: {e}\n你> ", end="", flush=True)
