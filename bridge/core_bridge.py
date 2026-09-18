# -*- coding: utf-8 -*-
"""核心桥接：把 qq_bot 的 AgentCore 嵌进本应用（以库方式复用，零源码改动）。

职责：
- 生命周期：build / start / stop / restart AgentCore（平台插件默认不注册，功能插件按开关）
- 聊天：把 Web 消息包成 InboundMessage 交给 ChatService，回复经 SSE 推给前端
- SSE 事件中枢：subscribe / push（仿 web_plugin 的会话订阅模型）
- 状态快照：core.status() + 应用层信息
"""
import asyncio
import threading
import time
import uuid
from collections import deque
from typing import Dict, Optional

from .loop import LoopThread

OWNER_ID = "app_owner"
OWNER_NAME = "主人"


class CoreBridge:
    """AgentCore 的宿主与 Web 聊天适配层。"""

    def __init__(self, lt: LoopThread, port: int):
        self.lt = lt
        self.port = port
        self.core = None
        self._core_lock = threading.Lock()
        self._starting = False

        # SSE：session -> set(queue.Queue)；_all 为全局广播
        self._subs: Dict[str, set] = {}
        self._all: set = set()
        self._lock = threading.Lock()
        self._log = deque(maxlen=300)
        self._seq = 0  # 事件自增 id（前端去重用：SSE 回放/重连/初始拉取可能重叠）

        # 消息类（首次聊天时构造，避免无核心模式 import 失败）
        self._msg_classes = None
        # 核心构建钩子：包管理器在 register_builtin_plugins 之后、core.start 之前注册插件包
        self._build_hook = None

    def set_build_hook(self, fn):
        """fn(core)：核心构建后、启动前调用（插件包预注册入口）。"""
        self._build_hook = fn

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def _build_core(self):
        """构造 AgentCore（App 模式）：
        - 平台插件不注册（本应用自身即平台），包管理器按需装载平台包
        - 功能插件的内建注册临时抑制（开关置 False），由包管理器统一装载，避免双注册
        """
        import config as qq_config
        import plugin_registry as reg
        from agent_core import get_core
        core = get_core()
        # 官方 UI 事件通道：把本桥接挂到 core 上，插件（含第三方插件包）可以
        #   bridge = getattr(core, "app_bridge", None)
        #   if bridge: bridge.push(session, {"type": "message", "role": "assistant", "text": ...})
        # 在 App 里插件没有其它官方途径访问 UI（Plugin.__init__ 只拿到 core），
        # 挂在 core 上避免插件各自 hack sys.modules / 私接内部对象。
        # push 是线程安全的（内部加锁 + queue），可在任意线程/协程调用。
        try:
            core.app_bridge = self
        except Exception as e:
            print(f"[APP][WARN] 注入 app_bridge 失败: {e!r}")
        saved = {}
        for s in reg.by_kind("feature"):
            if s.switch:
                saved[s.switch] = getattr(qq_config, s.switch, s.default_on)
                setattr(qq_config, s.switch, False)
        try:
            core.register_builtin_plugins(platforms=[])
        finally:
            for k, v in saved.items():
                setattr(qq_config, k, v)
        # 构建钩子：已启用的插件包在此预注册（插件/大脑），随 core.start() 统一拉起
        if self._build_hook:
            try:
                self._build_hook(core)
            except Exception as e:
                print(f"[APP][WARN] 插件包预装载异常: {e!r}")
        return core

    async def _async_start(self):
        core = self._build_core()
        self.core = core
        await core.start()

    async def _async_stop(self):
        core = self.core
        self.core = None
        if core is None:
            return
        try:
            await core.shutdown()
        except Exception as e:
            self.push(None, {"type": "error", "text": f"核心停止异常: {e!r}"})

    def start(self, wait: bool = True) -> dict:
        with self._core_lock:
            if self.core is not None:
                return {"ok": True, "state": "running"}
            if self._starting:
                return {"ok": False, "error": "核心正在启动中"}
            self._starting = True
        try:
            if wait:
                self.lt.run_coro(self._async_start(), timeout=180)
                state = {"ok": True, "state": "running"}
            else:
                self.lt.schedule(self._async_start())
                state = {"ok": True, "state": "starting"}
        except Exception as e:
            state = {"ok": False, "error": repr(e)}
        finally:
            self._starting = False
        return state

    def stop(self, wait: bool = True) -> dict:
        try:
            if wait:
                self.lt.run_coro(self._async_stop(), timeout=60)
            else:
                self.lt.schedule(self._async_stop())
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": repr(e)}

    def restart(self) -> dict:
        self.stop(wait=True)
        time.sleep(0.5)
        return self.start(wait=True)

    def is_running(self) -> bool:
        return bool(self.core and getattr(self.core, "running", False))

    # ------------------------------------------------------------------
    # 状态快照
    # ------------------------------------------------------------------
    def status(self) -> dict:
        st = {
            "core": self.is_running(),
            "starting": self._starting,
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "port": self.port,
            "plugins": [],
            "brains": [],
            "registry_report": None,
            "counts": {},
        }
        if self.core is not None:
            try:
                snap = self.core.status()
                st["plugins"] = snap.get("plugins", [])
                st["brains"] = snap.get("brains", [])
                st["registry_report"] = snap.get("registry_report")
            except Exception as e:
                st["error"] = repr(e)
        try:
            import plugin_registry as reg
            st["registry"] = reg.to_dict()
        except Exception:
            st["registry"] = []
        st["counts"] = {
            "plugins_total": len(st.get("registry", [])),
            "plugins_running": len([p for p in st.get("plugins", []) if p.get("running")]),
        }
        return st

    # ------------------------------------------------------------------
    # SSE 事件中枢
    # ------------------------------------------------------------------
    def push(self, session, event: dict):
        event = dict(event)
        with self._lock:
            self._seq += 1
            event["id"] = self._seq
            event["ts"] = time.time()
            self._log.append(event)
            targets = list(self._all)
            if session and session in self._subs:
                targets += list(self._subs[session])
        for q in targets:
            try:
                q.put_nowait(event)
            except Exception:
                pass

    def subscribe(self, session: str):
        import queue
        q = queue.Queue()
        with self._lock:
            self._all.add(q)
            if session:
                self._subs.setdefault(session, set()).add(q)
        return q

    def unsubscribe(self, session: str, q):
        with self._lock:
            self._all.discard(q)
            if session and session in self._subs:
                self._subs[session].discard(q)

    def recent(self, n: int = 60) -> list:
        with self._lock:
            return list(self._log)[-n:]

    # ------------------------------------------------------------------
    # 聊天
    # ------------------------------------------------------------------
    def _ensure_msg_classes(self):
        if self._msg_classes is not None:
            return self._msg_classes
        from message_bus import InboundMessage, ReplyTarget, MessageSender

        bridge = self

        class AppReplyTarget(ReplyTarget):
            """App 聊天页的回复目标（文字界面）。

            capabilities 显式声明 voice=False：
            - 绕开 chat_service 的语音链路（wants_voice_reply LLM 判定 + TTS），
              避免未实现的 reply_voice 被走到（曾导致间歇性空白回复）
            - App 端若要语音，由前端另行用 TTS 事件播放
            """

            capabilities = {"voice": False, "voice_only": False}
            _EMOJI_RE = None  # 惰性编译

            def __init__(self, session, msg=None):
                super().__init__(msg)
                self._session = session

            @classmethod
            def _emoji_pattern(cls):
                if cls._EMOJI_RE is None:
                    import re
                    cls._EMOJI_RE = re.compile(r"\[表情包[:：]([^\]]+)\]")
                return cls._EMOJI_RE

            async def reply(self, text, **kw):
                text = str(text or "")
                # 解析表情包标记 [表情包:文件名] -> image 事件（前端内嵌渲染）
                imgs = []

                def _grab(mm):
                    imgs.append(mm.group(1).strip())
                    return ""

                if self._emoji_pattern().search(text):
                    text = self._emoji_pattern().sub(_grab, text).strip()
                if text:
                    bridge.push(self._session,
                                {"type": "message", "role": "assistant", "text": text})
                for fn in imgs:
                    path = self.resolve_emoji_path(fn)
                    if path:
                        bridge.push(self._session, {"type": "image", "path": path})
                # 文本和图都为空：发占位，避免前端出现"无回应"错觉
                if not text and not imgs:
                    bridge.push(self._session, {"type": "message", "role": "assistant",
                                                "text": "（嗯……我好像走神了，再问一次？）"})
                return True

            @staticmethod
            def resolve_emoji_path(fn):
                """表情文件名 -> 绝对路径（在运行时目录 emojis/ 下）。"""
                import os
                p = os.path.join(os.getcwd(), "emojis", fn)
                if os.path.isfile(p):
                    return p
                return None

            async def reply_text(self, text, **kw):
                return await self.reply(text, **kw)

            async def reply_voice(self, wav_path, **kw):
                # 理论上不会走到（capabilities.voice=False）；防御性推送
                bridge.push(self._session, {"type": "audio", "path": str(wav_path)})
                return True

            async def reply_image(self, path, **kw):
                bridge.push(self._session, {"type": "image", "path": str(path)})
                return True

            async def reply_audio(self, path, **kw):
                bridge.push(self._session, {"type": "audio", "path": str(path)})
                return True

            async def tts(self, text, **kw):
                bridge.push(self._session, {"type": "tts", "text": str(text)})
                return True

        class AppSender(MessageSender):
            async def send_private(self, user_id, text):
                bridge.push(None, {"type": "message", "role": "assistant",
                                   "text": str(text), "to": str(user_id)})
                return True

            async def send_group(self, group_id, text):
                bridge.push(None, {"type": "message", "role": "assistant",
                                   "text": str(text), "to_group": str(group_id)})
                return True

        self._msg_classes = (InboundMessage, AppReplyTarget, AppSender)
        return self._msg_classes

    def submit_chat(self, text: str, session: str = "web", user_id: str = OWNER_ID,
                    name: str = OWNER_NAME) -> dict:
        """提交一条用户消息到 ChatService（异步执行，回复走 SSE）。"""
        text = (text or "").strip()
        if not text:
            return {"ok": False, "error": "消息为空"}
        if not self.is_running():
            return {"ok": False, "error": "核心未运行，请先在仪表盘启动核心"}
        InboundMessage, AppReplyTarget, _ = self._ensure_msg_classes()
        msg = InboundMessage(
            platform="app",
            channel_type="private",
            channel_id=user_id,
            user_id=user_id,
            user_name=name,
            message_id=str(uuid.uuid4()),
            text=text,
            mentioned=True,
            raw={"session": session, "source": "app"},
        )
        reply = AppReplyTarget(session, msg=msg)
        core = self.core
        bridge = self

        async def _run():
            bridge.push(session, {"type": "status", "state": "thinking"})
            try:
                await core.chat.handle_message(msg, reply)
            except Exception as e:
                bridge.push(session, {"type": "error", "text": repr(e)})
            finally:
                bridge.push(session, {"type": "status", "state": "idle"})

        self.lt.schedule(_run())
        return {"ok": True}
