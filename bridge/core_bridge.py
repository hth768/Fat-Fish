# -*- coding: utf-8 -*-
"""核心桥接：把 qq_bot 的 AgentCore 嵌进本应用（以库方式复用，零源码改动）。

职责：
- 生命周期：build / start / stop / restart AgentCore（平台插件默认不注册，功能插件按开关）
- 聊天：把 Web 消息包成 InboundMessage 交给 ChatService，回复经 SSE 推给前端
- SSE 事件中枢：subscribe / push（仿 web_plugin 的会话订阅模型）
- 状态快照：core.status() + 应用层信息
"""
import asyncio
import sys
import threading
import time
import traceback
import warnings
import uuid
from collections import deque
from typing import Dict, Optional

from .loop import LoopThread
from quiet import degrade

OWNER_ID = "app_owner"
OWNER_NAME = "主人"

_BRIDGE_SINGLETON = None


def set_bridge(b: "CoreBridge"):
    global _BRIDGE_SINGLETON
    _BRIDGE_SINGLETON = b


def get_bridge() -> "CoreBridge":
    """获取 CoreBridge 单例（App 启动时由 app.py 注入）。"""
    return _BRIDGE_SINGLETON


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

        # 多 bot 生命周期管理器（默认主 bot 为 "feiyu"，其余运行时动态增删）
        from .bot_manager import BotManager
        self.bot_manager = BotManager(self)

        # ===== 调试模式中枢 =====
        # 开启时采集：stdout 输出、未捕获 traceback、警告、quiet 降级/告警，并周期推送核心运行状态。
        self._debug = False
        self._debug_buf = deque(maxlen=3000)   # 环形缓冲，供前端重新打开时回灌
        self._debug_pending = []               # stdout tee 待刷新文本
        self._debug_lock = threading.Lock()
        self._debug_orig = {}                  # 安装 hook 前的原始对象，用于还原
        self._debug_threads = []

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
        # 登记进 BotManager，避免下方 autostart_all 对默认主 bot 二次 start()
        self.bot_manager.cores[core.agent_id] = core
        await core.start()

    async def _async_stop(self):
        core = self.core
        self.core = None
        if core is not None:
            # 同步清理 BotManager 登记，避免 stop 后 is_running/start 误判
            self.bot_manager.cores.pop(getattr(core, "agent_id", None), None)
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
                # 非阻塞启动其余 autostart bot（多 bot 可同时运行）
                self.bot_manager.autostart_all()
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
    def push(self, session, event: dict, record: bool = True):
        event = dict(event)
        with self._lock:
            self._seq += 1
            event["id"] = self._seq
            event["ts"] = time.time()
            if record:
                self._log.append(event)
            targets = list(self._all)
            if session and session in self._subs:
                targets += list(self._subs[session])
        for q in targets:
            try:
                q.put_nowait(event)
            except Exception as e:
                degrade("bridge/core_bridge.py:191 CoreBridge.push", e, "降级：q.put_nowait(event)")

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
    # 调试模式中枢
    # ------------------------------------------------------------------
    def debug_enabled(self) -> bool:
        return self._debug

    def get_debug_log(self) -> list:
        """返回环形缓冲中的调试日志快照（供前端打开控制台时回灌）。"""
        with self._debug_lock:
            return [dict(e) for e in self._debug_buf]

    def push_debug(self, level: str, text: str, where: str = ""):
        """向调试日志中枢追加一条记录并实时推送（仅调试模式开启时生效）。

        防刷屏：连续完全相同的条目（同级别/同文本/同来源，且间隔 <15s）
        折叠为最后一行的计数（×N），不再无限堆积——例如客户端断开引发的
        重复「SSE 写入失败」告警。
        """
        if not self._debug:
            return
        text = str(text)
        entry = {"level": level, "text": text, "where": where, "ts": time.time()}
        repeat = 0
        with self._debug_lock:
            last = self._debug_buf[-1] if self._debug_buf else None
            if (last is not None
                    and last.get("level") == level
                    and last.get("text") == text
                    and last.get("where") == where
                    and entry["ts"] - last.get("ts", 0) < 15):
                last["count"] = last.get("count", 1) + 1
                last["ts"] = entry["ts"]
                repeat = last["count"]
            else:
                self._debug_buf.append(entry)
        ev = {"type": "debug", "level": level, "text": text, "where": where}
        if repeat:
            ev["repeat"] = repeat
        # record=False：调试事件不进通用回放历史（_log），避免挤掉真实事件；
        # 前端回灌走 /api/debug/log（_debug_buf）。
        self.push(None, ev, record=False)

    def set_debug(self, enabled: bool):
        """开启/关闭调试模式（配置页开关联动；App 启动按设置预置）。"""
        enabled = bool(enabled)
        if enabled == self._debug:
            return
        self._debug = enabled
        if enabled:
            self._debug_install()
            self.push_debug("status", "[调试] 已开启调试模式，开始采集运行日志（stdout / traceback / warn / error）")
        else:
            self._debug_uninstall()
            self.push_debug("status", "[调试] 已关闭调试模式")

    # ---- 模块级 hook（供 sys.excepthook / threading.excepthook / warnings 使用） ----
    def _debug_hook_except(et, ev, tb):
        b = get_bridge()
        if b is not None:
            try:
                b.push_debug("traceback", "".join(traceback.format_exception(et, ev, tb)).rstrip(),
                             where="sys.excepthook")
            except Exception:
                pass

    def _debug_hook_thread_except(args):
        b = get_bridge()
        if b is not None:
            try:
                et, ev, tb = args.exc_type, args.exc_value, args.exc_traceback
                b.push_debug("traceback",
                             "".join(traceback.format_exception(et, ev, tb)).rstrip(),
                             where="thread:%s" % getattr(args, "thread", None))
            except Exception:
                pass

    def _debug_hook_warning(message, category, filename, lineno, file=None, line=None):
        b = get_bridge()
        if b is not None:
            try:
                name = getattr(category, "__name__", "Warning")
                b.push_debug("warn", "[%s] %s (%s:%d)" % (name, message, filename, lineno),
                             where="warnings")
            except Exception:
                pass

    def _debug_hook_quiet(level, where, exc, note):
        b = get_bridge()
        if b is None:
            return
        try:
            detail = ("%s: %s" % (type(exc).__name__, exc)) if exc is not None else ""
            text = (note or "").strip()
            if detail:
                text = (text + "  " + detail).strip() if text else detail
            # ATTENTION = 真问题 → error；DEGRADE = 刻意降级 → warn
            b.push_debug("error" if level == "ATTENTION" else "warn",
                         text or "(无详情)", where=where)
        except Exception:
            pass

    class _DebugStream:
        """包装 sys.stdout：原样写出，同时把文本喂给调试缓冲（批量刷新避免刷屏）。"""

        def __init__(self, orig, hub):
            self._orig = orig
            self._hub = hub

        def write(self, s):
            r = self._orig.write(s)
            if s:
                self._hub._debug_feed(s)
            return r

        def flush(self):
            return self._orig.flush()

        def writelines(self, lines):
            return self._orig.writelines(lines)

        def __getattr__(self, name):
            # 委托 encoding/fileno/closed 等给原对象
            return getattr(self._orig, name)

    def _debug_feed(self, s):
        with self._debug_lock:
            self._debug_pending.append(s)

    def _debug_flush_loop(self):
        while self._debug:
            time.sleep(0.25)
            with self._debug_lock:
                buf = self._debug_pending
                self._debug_pending = []
            if buf:
                text = "".join(buf).rstrip("\n")
                if text:
                    self.push_debug("log", text)

    def _debug_heartbeat_loop(self):
        while self._debug:
            time.sleep(5)
            try:
                st = self.status()
                running = st.get("core")
                plugins = len(st.get("plugins") or [])
                brains = len(st.get("brains") or [])
                summary = "运行状态：核心 %s | 插件 %d | 大脑 %d | 端口 %s | %s" % (
                    "运行中" if running else "未运行", plugins, brains,
                    self.port, st.get("time"))
                self.push_debug("status", summary)
            except Exception:
                pass

    def _debug_install(self):
        import sys as _sys
        import warnings as _warnings
        # 1) quiet 降级/告警 → 调试流
        try:
            from quiet import set_sink
            set_sink(CoreBridge._debug_hook_quiet)
        except Exception:
            pass
        # 2) 未捕获异常 hook
        try:
            self._debug_orig["excepthook"] = _sys.excepthook
            _sys.excepthook = CoreBridge._debug_hook_except
        except Exception:
            pass
        try:
            self._debug_orig["thread_excepthook"] = threading.excepthook
            threading.excepthook = CoreBridge._debug_hook_thread_except
        except Exception:
            pass
        # 3) 警告 hook
        try:
            self._debug_orig["showwarning"] = _warnings.showwarning
            _warnings.showwarning = CoreBridge._debug_hook_warning
        except Exception:
            pass
        # 4) stdout 输出流 tee（捕捉 print 出来的运行状态）
        try:
            if _sys.stdout is not None and not isinstance(_sys.stdout, CoreBridge._DebugStream):
                self._debug_orig["stdout"] = _sys.stdout
                _sys.stdout = CoreBridge._DebugStream(_sys.stdout, self)
        except Exception:
            pass
        # 5) 后台线程：批量刷新 stdout + 周期心跳
        t1 = threading.Thread(target=self._debug_flush_loop, name="feiyu-debug-flush", daemon=True)
        t2 = threading.Thread(target=self._debug_heartbeat_loop, name="feiyu-debug-heartbeat", daemon=True)
        t1.start()
        t2.start()
        self._debug_threads = [t1, t2]

    def _debug_uninstall(self):
        import sys as _sys
        import warnings as _warnings
        try:
            from quiet import set_sink
            set_sink(None)
        except Exception:
            pass
        o = self._debug_orig
        if o.get("excepthook") is not None:
            _sys.excepthook = o["excepthook"]
        if o.get("thread_excepthook") is not None:
            threading.excepthook = o["thread_excepthook"]
        if o.get("showwarning") is not None:
            _warnings.showwarning = o["showwarning"]
        if o.get("stdout") is not None and isinstance(_sys.stdout, CoreBridge._DebugStream):
            _sys.stdout = o["stdout"]
        self._debug_orig = {}

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

            async def fetch_image(self, ref) -> bytes:
                """取回图片二进制：App 端图片已落盘到 data/media，ref 即绝对路径。"""
                import os
                p = str(ref or "")
                if p and os.path.isfile(p):
                    with open(p, "rb") as f:
                        return f.read()
                raise RuntimeError("图片文件不存在: " + p)

            async def fetch_video(self, ref) -> str:
                """取回视频本地路径：App 端视频已落盘到 data/media，ref 即绝对路径。"""
                import os
                p = str(ref or "")
                if p and os.path.isfile(p):
                    return p
                raise RuntimeError("视频文件不存在: " + p)

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
                    name: str = OWNER_NAME, bot_id: str = None,
                    image_paths: list = None, video_path: str = None,
                    audio_wav: bytes = None) -> dict:
        """提交一条用户消息到指定 bot 的 ChatService（异步执行，回复走 SSE）。

        媒体经本地落盘路径传入：image_paths=图片绝对路径列表；video_path=视频路径；
        audio_wav=已解码为 wav 的语音字节（浏览器录音由 server 经 ffmpeg 转码后传入）。
        """
        text = (text or "").strip()
        image_paths = [p for p in (image_paths or []) if p]
        if not text and not image_paths and not video_path and not audio_wav:
            return {"ok": False, "error": "消息为空"}
        core = self.bot_manager.core_for(bot_id)
        if core is None or not getattr(core, "running", False):
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
            image_refs=image_paths,
            audio_wav=audio_wav or b"",
            has_video=bool(video_path),
            video_ref=video_path,
            mentioned=True,
            raw={"session": session, "source": "app", "bot_id": bot_id or ""},
        )
        reply = AppReplyTarget(session, msg=msg)
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
