# -*- coding: utf-8 -*-
"""Web 平台插件（浏览器前端交互界面）。

让浏览器成为「肥鱼娘 / DeepSeek娘」的一个聊天平台（与 console_plugin 同级），
所有斜杠命令都会通过真实核心（ChatService）生效。同时承担：

- 静态前端托管（webui/ 下的 index.html / styles.css / app.js）
- SSE 实时事件流（回复、主动消息、状态）
- 只读数据 API（记忆 / 情绪 / 知识库 / 身份 / 大脑状态 / MC / PVZ / B站 / 语音 …）
- 控制 API（发消息、执行命令、MC 提示等）

运行方式：
    1) 独立运行（自带最小核心，不连 QQ/NapCat）：
        venv/Scripts/python.exe web_plugin.py --port 8800
        venv/Scripts/python.exe web_plugin.py --no-core   # 仅前端 + 只读数据
    2) 作为 bot 平台插件（在 main.py 里加 --web）：
        venv/Scripts/python.exe main.py --web
"""
from __future__ import annotations

import argparse
import asyncio
import functools
import json
import mimetypes
import os
import sys
import queue
import threading
import time
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from quiet import degrade

try:
    from plugin_base import PlatformPlugin
except Exception:  # 独立脚本且 plugin_base 不可用时降级（仅影响类继承，不影响运行）
    class PlatformPlugin:  # type: ignore
        def __init__(self, core=None):
            self.core = core
            self._started = False

# ----------------------------------------------------------------------------
# 路径
# ----------------------------------------------------------------------------
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_WEBUI_DIR = os.path.join(_BASE_DIR, "webui")
_DATA_DIR = os.path.join(_BASE_DIR, "data")

OWNER_ID = "web_owner"
OWNER_NAME = "主人"


def _read_json(path, default=None):
    try:
        if not os.path.exists(path):
            return default
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _root_json(name, default=None):
    return _read_json(os.path.join(_BASE_DIR, name), default)


def _data_json(name, default=None):
    return _read_json(os.path.join(_DATA_DIR, name), default)


# 延迟导入项目模块（--no-core 模式下尽量不触发重依赖）
def _import_config():
    import config
    return config


def _safe_config():
    """容错版 _import_config：导入失败返回 None（--no-core 前端仍可访问）。"""
    try:
        return _import_config()
    except Exception:
        return None


# ----------------------------------------------------------------------------
# 回复目标 / 发送者（Web 平台）
# ----------------------------------------------------------------------------
def _build_reply_and_sender():
    """构造依赖项目核心类的 WebReplyTarget / WebSender（带降级）。"""
    try:
        from message_bus import ReplyTarget, MessageSender, InboundMessage
    except Exception as e:  # pragma: no cover
        raise ImportError(f"无法导入 message_bus: {e}")

    class WebReplyTarget(ReplyTarget):
        def __init__(self, plugin, session, msg=None, **kw):
            super().__init__(msg)
            self._plugin = plugin
            self.session = session

        async def reply(self, text, **kw):
            self._plugin.push(self.session, {"type": "message", "role": "assistant", "text": str(text)})
            return True

        async def reply_text(self, text, **kw):
            return await self.reply(text, **kw)

        async def reply_image(self, path, **kw):
            rel = self._plugin.rel_path(path)
            if rel:
                self._plugin.push(self.session, {"type": "image", "url": "/api/file?f=" + rel})
            return True

        async def reply_audio(self, path, **kw):
            rel = self._plugin.rel_path(path)
            if rel:
                self._plugin.push(self.session, {"type": "audio", "url": "/api/file?f=" + rel})
            return True

        async def tts(self, text, **kw):
            self._plugin.push(self.session, {"type": "tts", "text": str(text)})
            return True

    class WebSender(MessageSender):
        def __init__(self, plugin, name=None, **kw):
            super().__init__()
            self._plugin = plugin
            self.name = name

        async def send_private(self, user_id, text, **kw):
            # 主动/系统私聊消息 → 推送给所有 Web 会话（视作猫娘主动发言）
            self._plugin.push(None, {
                "type": "message", "role": "assistant", "proactive": True,
                "sender": kw.get("name") or self.name or OWNER_NAME,
                "text": str(text),
            })

        async def send_group(self, group_id, text, **kw):
            await self.send_private(group_id, text, **kw)

    return WebReplyTarget, WebSender, InboundMessage


# ----------------------------------------------------------------------------
# Web 平台插件
# ----------------------------------------------------------------------------
class WebPlugin(PlatformPlugin):
    """平台插件：托管前端 + SSE + 数据/控制 API。"""

    name = "web"
    platform = "web"
    capabilities = {"group": True, "voice": True, "image": True, "voice_input": False, "video_input": False}

    def __init__(self, core=None, port: int = 8800):
        super().__init__(core)
        self.port = port
        self.loop = None
        self._http = None
        self._http_thread = None
        self._ready = threading.Event()

        # SSE 订阅：session -> set(queue.Queue)；_ALL 全局广播
        self._subs = {}
        self._all = set()
        self._lock = threading.Lock()
        self._log = []  # 事件回放缓冲（最多 200 条）

        self.WebReplyTarget = None
        self.WebSender = None
        self.InboundMessage = None

    # ---- 生命周期 ----
    async def start(self):
        await super().start()
        try:
            self.WebReplyTarget, self.WebSender, self.InboundMessage = _build_reply_and_sender()
        except Exception as e:
            print(f"[WEB] 警告：无法构造 Web 回复类（聊天不可用）: {e}")
        self.loop = asyncio.get_running_loop()
        self._start_http()
        self._ready.set()
        print(f"[WEB] 前端已启动: http://127.0.0.1:{self.port}  (核心: {'已连接' if self.core else '未连接'})")
        self._open_browser()

    async def stop(self):
        if self._http:
            self._http.shutdown()

    def _open_browser(self):
        """启动成功后尝试打开默认浏览器（失败不影响服务）。"""
        import webbrowser
        try:
            webbrowser.open(f"http://127.0.0.1:{self.port}")
        except Exception as e:
            degrade("libs/qq_bot_runtime/web_plugin.py:197 WebPlugin._open_browser", e, "降级：webbrowser.open(f'http://127.0.0.1:{self.port}')")

    def serve_no_core(self):
        """仅前端 + 只读数据模式（无聊天核心）。"""
        self._start_http()
        self._ready.set()
        print(f"[WEB] 前端（只读模式）已启动: http://127.0.0.1:{self.port}")
        self._open_browser()
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt as e:
            degrade("libs/qq_bot_runtime/web_plugin.py:209 WebPlugin.serve_no_core", e, "降级：while True")

    # ---- SSE 事件推送 ----
    def push(self, session, event):
        event = dict(event)
        event["ts"] = time.time()
        with self._lock:
            self._log.append(event)
            if len(self._log) > 200:
                self._log = self._log[-200:]
            targets = list(self._all)
            if session and session in self._subs:
                targets += list(self._subs[session])
        for q in targets:
            try:
                q.put_nowait(event)
            except Exception as e:
                degrade("libs/qq_bot_runtime/web_plugin.py:226 WebPlugin.push", e, "降级：q.put_nowait(event)")

    def subscribe(self, session):
        q = queue.Queue()
        with self._lock:
            self._all.add(q)
            if session:
                self._subs.setdefault(session, set()).add(q)
        return q

    def unsubscribe(self, session, q):
        with self._lock:
            self._all.discard(q)
            if session and session in self._subs:
                self._subs[session].discard(q)

    def recent(self, n=50):
        with self._lock:
            return list(self._log[-n:])

    # ---- 路径安全 ----
    def rel_path(self, path):
        try:
            p = os.path.abspath(path)
            if p.startswith(_BASE_DIR):
                return os.path.relpath(p, _BASE_DIR).replace("\\", "/")
        except Exception as e:
            degrade("libs/qq_bot_runtime/web_plugin.py:253 WebPlugin.rel_path", e, "降级：p = os.path.abspath(path)")
        return None

    # ---- 在事件循环里调度协程（供 HTTP 线程调用）----
    def _schedule(self, coro, timeout=180):
        if self.loop is None:
            raise RuntimeError("核心事件循环未就绪")
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout=timeout)

    # ---- 提交一条用户消息到聊天核心 ----
    def submit(self, text, session, user_id=OWNER_ID, name=OWNER_NAME, msg_type="private", group_id=None):
        if not self.core or not self.WebReplyTarget:
            return False, "核心未启动（请用 python web_plugin.py 或 main.py --web 启动带核心的版本）"
        channel = "group" if msg_type == "group" else "private"
        msg = self.InboundMessage(
            platform="web",
            channel_type=channel,
            channel_id=group_id or user_id,
            user_id=user_id,
            user_name=name,
            message_id=str(uuid.uuid4()),
            text=text,
            mentioned=True,
            raw={"session": session, "source": "web"},
        )
        reply = self.WebReplyTarget(self, session, msg=msg)

        async def _run():
            self.push(session, {"type": "status", "state": "thinking"})
            try:
                await self.core.chat.handle_message(msg, reply)
            except Exception as e:  # noqa
                self.push(session, {"type": "error", "text": repr(e)})
            finally:
                self.push(session, {"type": "status", "state": "idle"})

        self._schedule(_run(), timeout=180)
        return True, None

    # ---- 核心状态快照 ----
    def state(self):
        st = {
            "plugin": "web",
            "core": bool(self.core),
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "status": None,
            "brains": {},
        }
        if self.core and hasattr(self.core, "status"):
            try:
                st["status"] = self.core.status()
            except Exception as e:
                degrade("libs/qq_bot_runtime/web_plugin.py:305 WebPlugin.state", e, "降级：st['status'] = self.core.status()")
        return st

    # ---- 插件 / 服务聚合（供 Web「插件管理」面板）----
    def _api_plugins(self):
        """聚合：平台插件 + 功能插件 + sidecar 服务 + 功能开关，供网页统一管理。"""
        out = {"launcher": False, "platforms": [], "features": [], "sidecars": [], "toggles": []}
        # 平台 / 功能插件（来自核心已注册实例）
        if self.core and hasattr(self.core, "status"):
            try:
                st = self.core.status()
                out["platforms"] = st.get("plugins", [])
                out["features"] = st.get("feature_plugins", [])
            except Exception as e:
                degrade("libs/qq_bot_runtime/web_plugin.py:319 WebPlugin._api_plugins", e, "降级：st = self.core.status()")
        # sidecar 服务（来自 launcher 单例；launcher 与 bot 同进程，可统一管理）
        try:
            import launcher
            L = launcher.ACTIVE_LAUNCHER
        except Exception:
            L = None
        if L is not None:
            out["launcher"] = True
            for spec in L.specs:
                proc = L.procs.get(spec.name)
                out["sidecars"].append({
                    "name": spec.name,
                    "script": spec.script,
                    "configured": True,
                    "running": proc is not None and proc.poll() is None,
                    "pid": proc.pid if proc else None,
                    "ready": bool(L.is_ready(spec)),
                    "port": spec.port,
                    "required": spec.required,
                    "python": spec.python or "（主 venv）",
                })
        # 功能开关（config 里的 ENABLE_* 主要特性）
        try:
            cfg = _import_config()
        except Exception:
            cfg = None
        if cfg:
            for k in ("ENABLE_WEB_PLUGIN", "ENABLE_QQ_PLUGIN", "ENABLE_CONSOLE_PLUGIN",
                      "ENABLE_BILIBILI_PLUGIN", "ENABLE_MEMORY_SERVER", "ENABLE_MONITOR",
                      "ENABLE_TTS_SERVER", "ENABLE_PROACTIVE_SPEAKER",
                      "ENABLE_KNOWLEDGE_LEARN", "ENABLE_KNOWLEDGE_RECALL",
                      "ENABLE_REFLECTION", "ENABLE_PERSONA", "ENABLE_VECTOR_MEMORY",
                      "ENABLE_SCREEN_AWARENESS", "ENABLE_LIVE_VISION",
                      "ENABLE_LOCAL_VL", "ENABLE_REALTIME_VOICE", "ENABLE_BALANCE_MONITOR",
                      "ENABLE_MC_AGENT", "ENABLE_PC_CONTROL"):
                v = getattr(cfg, k, None)
                if isinstance(v, bool):
                    out["toggles"].append({"key": k, "value": v})
        return out

    def _api_memory_browser(self):
        return _api_memory_browser()

    def _api_memory_browser_action(self, body):
        return _api_memory_browser_action(body)

    def _api_control_plugins(self, body):
        """控制：重启 sidecar / 启停非 web 插件。"""
        action = body.get("action")
        target = body.get("target")
        if action == "restart_sidecar":
            try:
                import launcher
                L = launcher.ACTIVE_LAUNCHER
            except Exception:
                L = None
            if not L:
                return {"ok": False, "error": "launcher 未运行（请用 python launcher.py 启动）"}
            if target not in {s.name for s in L.specs}:
                return {"ok": False, "error": f"未知 sidecar: {target}"}
            ok = L.restart(target)
            return {"ok": ok, "action": action, "target": target}
        if action in ("start_plugin", "stop_plugin"):
            if not self.core:
                return {"ok": False, "error": "核心未连接"}
            if target == "web":
                return {"ok": False, "error": "Web 控制台自身不可停止"}
            p = next((c for c in self.core.plugins.all() if c.name == target), None)
            if p is None:
                return {"ok": False, "error": f"未知插件: {target}"}
            try:
                if action == "stop_plugin" and getattr(p, "running", False):
                    self._schedule(p.stop())
                elif action == "start_plugin" and not getattr(p, "running", False):
                    self._schedule(p.start())
                return {"ok": True, "action": action, "target": target,
                        "running": getattr(p, "running", None)}
            except Exception as e:
                return {"ok": False, "error": repr(e)}
        return {"ok": False, "error": f"未知 action: {action}"}

    def _api_telemetry(self):
        """遥测统计：优先取遥测 sidecar 聚合；无 sidecar 时退回本地 telemetry.snapshot。"""
        try:
            import launcher
            L = launcher.ACTIVE_LAUNCHER
        except Exception:
            L = None
        sidecar = None
        if L is not None:
            for s in L.specs:
                if s.name == "telemetry":
                    sidecar = s
                    break
        if sidecar is not None:
            from urllib.request import urlopen, URLError
            import json as _json
            url = f"http://{sidecar.host}:{sidecar.port}/stats"
            try:
                with urlopen(url, timeout=2) as r:
                    d = _json.loads(r.read().decode("utf-8"))
                    return {"source": "sidecar",
                            "hmac_enforced": d.get("hmac_enforced", False),
                            "stats": d.get("stats", {})}
            except (URLError, OSError, Exception) as e:
                degrade("libs/qq_bot_runtime/web_plugin.py:425 WebPlugin._api_telemetry", e, "降级：with urlopen(url, timeout=2) as r")
        try:
            import telemetry
            return {"source": "local", "stats": {
                "providers": telemetry.snapshot("providers"),
                "vision": telemetry.snapshot("vision"),
            }}
        except Exception:
            return {"source": "none", "stats": {}}

    # ---- HTTP 服务 ----
    def _start_http(self):
        self._http = _QuietHTTPServer(("0.0.0.0", self.port), _Handler)
        self._http._plugin = self  # 供 _Handler 访问
        t = threading.Thread(target=self._http.serve_forever, daemon=True)
        t.start()
        self._http_thread = t


def _wrap(coro_fn):
    return coro_fn()


# ----------------------------------------------------------------------------
# HTTP 处理器
# ----------------------------------------------------------------------------
class _Handler(BaseHTTPRequestHandler):
    server_version = "NekoWeb/1.0"

    def log_message(self, *args, **kwargs):  # 安静日志
        pass

    # ---- 工具 ----
    def _send_json(self, obj, code=200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _send_text(self, text, ctype="text/plain; charset=utf-8"):
        data = text.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _path_query(self):
        parsed = urlparse(self.path)
        return parsed.path, parse_qs(parsed.query)

    def do_GET(self):
        path, q = self._path_query()
        plugin = self.server._plugin if hasattr(self.server, "_plugin") else None

        if path in ("/", "/index.html"):
            return self._serve_static("index.html", "text/html; charset=utf-8")
        if path.startswith("/static/"):
            return self._serve_static(path[len("/static/"):], None)
        if path == "/api/state":
            return self._send_json(plugin.state() if plugin else {"core": False})
        if path == "/api/config":
            return self._send_json(self._api_config())
        if path == "/api/emotion":
            return self._send_json(self._api_emotion())
        if path == "/api/knowledge":
            return self._send_json(self._api_knowledge(q.get("q", [""])[0]))
        if path == "/api/memory":
            return self._send_json(self._api_memory())
        if path == "/api/identity":
            return self._send_json(self._api_identity())
        if path == "/api/brains":
            return self._send_json(self._api_brains())
        if path == "/api/mc":
            return self._send_json(self._api_mc())
        if path == "/api/pvz":
            return self._send_json(self._api_pvz())
        if path == "/api/bili":
            return self._send_json(self._api_bili())
        if path == "/api/voice":
            return self._send_json(self._api_voice())
        if path == "/api/system":
            return self._send_json(self._api_system())
        if path == "/api/events":
            return self._serve_sse(q.get("session", ["web"])[0])
        if path == "/api/file":
            return self._serve_file(q.get("f", [""])[0])
        if path == "/api/health":
            return self._send_json({"ok": True, "time": time.time()})
        if path == "/api/plugins":
            return self._send_json(plugin._api_plugins() if plugin else {"launcher": False})
        if path == "/api/telemetry":
            return self._send_json(self._api_telemetry())
        if path == '/api/memory_browser':
            return self._send_json(_api_memory_browser())
        return self._send_json({"error": "not found"}, 404)

    def do_POST(self):
        path, q = self._path_query()
        plugin = self.server._plugin if hasattr(self.server, "_plugin") else None
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            body = {}

        if path == "/api/chat":
            session = body.get("session") or "web"
            text = (body.get("text") or "").strip()
            if not text:
                return self._send_json({"ok": False, "error": "empty"}, 400)
            ok, err = plugin.submit(text, session,
                                    user_id=body.get("user_id", OWNER_ID),
                                    name=body.get("name", OWNER_NAME),
                                    msg_type=body.get("msg_type", "private"),
                                    group_id=body.get("group_id"))
            return self._send_json({"ok": ok, "error": err})

        if path == "/api/command":
            # 以主人身份执行一条斜杠命令（/mc自动、/电脑做、/pvz玩 …）
            text = (body.get("text") or "").strip()
            session = body.get("session") or "web"
            if not text:
                return self._send_json({"ok": False, "error": "empty"}, 400)
            ok, err = plugin.submit(text, session, user_id=OWNER_ID, name=OWNER_NAME)
            return self._send_json({"ok": ok, "error": err})

        if path == "/api/config/reload":
            # 运行时热重载 AI 供应商配置中心（改完 ai_providers.json 无需重启）
            return self._send_json(self._api_reload_config())

        if path == "/api/hint":
            text = (body.get("text") or "").strip()
            if not text:
                return self._send_json({"ok": False, "error": "empty"}, 400)
            # 写入 MC 提示队列（若核心存在）；否则落盘
            try:
                if plugin.core and hasattr(plugin.core, "mc"):
                    plugin._schedule(plugin.core.mc.add_hint(text))
                    return self._send_json({"ok": True})
            except Exception as e:
                return self._send_json({"ok": False, "error": repr(e)})
            return self._send_json({"ok": True, "note": "核心未连接，提示未发送"})

        if path == "/api/control":
            return self._send_json(self._api_control(body))

        if path == "/api/memory":
            return self._send_json(self._api_memory_action(body))

        if path == "/api/plugins/control":
            return self._send_json(plugin._api_control_plugins(body) if plugin else {"ok": False})

        if path == '/api/memory_browser':
            return self._send_json(_api_memory_browser_action(body))

        return self._send_json({"error": "not found"}, 404)

    # ---- 静态文件 ----
    def _serve_static(self, rel, ctype):
        full = os.path.normpath(os.path.join(_WEBUI_DIR, rel))
        if not full.startswith(_WEBUI_DIR) or not os.path.isfile(full):
            return self._send_json({"error": "not found"}, 404)
        if ctype is None:
            ctype, _ = mimetypes.guess_type(full)
            ctype = ctype or "application/octet-stream"
        with open(full, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # ---- 文件（图片/音频）----
    def _serve_file(self, rel):
        if not rel:
            return self._send_json({"error": "missing f"}, 400)
        full = os.path.normpath(os.path.join(_BASE_DIR, rel.replace("/", os.sep)))
        if not full.startswith(_BASE_DIR) or not os.path.isfile(full):
            return self._send_json({"error": "not found"}, 404)
        ctype, _ = mimetypes.guess_type(full)
        ctype = ctype or "application/octet-stream"
        with open(full, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # ---- SSE ----
    def _serve_sse(self, session):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        plugin = self.server._plugin
        q = plugin.subscribe(session)
        # 回放最近事件
        try:
            for ev in plugin.recent(60):
                self.wfile.write(("data: " + json.dumps(ev, ensure_ascii=False) + "\n\n").encode("utf-8"))
            self.wfile.flush()
        except Exception:
            plugin.unsubscribe(session, q)
            return
        try:
            while True:
                try:
                    ev = q.get(timeout=15)
                    self.wfile.write(("data: " + json.dumps(ev, ensure_ascii=False) + "\n\n").encode("utf-8"))
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError) as e:
            degrade("web_plugin._Handler._serve_sse", e, "客户端断开 SSE，停止推送")
        finally:
            plugin.unsubscribe(session, q)

    # ===================== 数据 API =====================
    def _api_config(self):
        try:
            cfg = _import_config()
        except Exception as e:
            return {"error": str(e)}
        secret_keys = {"DEEPSEEK_API_KEY", "DASHSCOPE_API_KEY", "OPENAI_API_KEY",
                       "DOUBAO_API_KEY", "DASHSCOPE_API_BASE", "DEEPSEEK_API_BASE",
                       "OPENAI_API_BASE", "DOUBAO_API_BASE"}
        out = {}
        for k in dir(cfg):
            if k.startswith("_"):
                continue
            v = getattr(cfg, k)
            if isinstance(v, (str, int, float, bool, dict, list)) or v is None:
                if k in secret_keys and v:
                    v = str(v)[:4] + "****" + str(v)[-4:]
                out[k] = v
        return out

    def _api_emotion(self):
        data = _root_json("emotion_data.json", {})
        if isinstance(data, dict) and data:
            return data
        # 尝试从核心读取实时心情
        return {"note": "无 emotion_data.json，使用核心实时状态", "raw": data}

    def _api_knowledge(self, q):
        items = _root_json("knowledge_base.json", [])
        if not isinstance(items, list):
            items = []
        if q:
            ql = q.lower()
            items = [i for i in items if ql in json.dumps(i, ensure_ascii=False).lower()]
        return {"count": len(_root_json("knowledge_base.json", []) or []), "items": items[:200]}

    def _api_memory(self):
        mem = _root_json("memory_data.json", {})
        notes = _root_json("important_notes.json", {})
        profiles = _root_json("user_profiles.json", {})
        reflections = _root_json("reflection_data.json", {})
        persona = _root_json("persona_data.json", {})
        # 取最近聊天片段
        recent = []
        if isinstance(mem, dict):
            store = mem.get("store", {})
            for key, turns in store.items():
                for t in (turns or [])[-8:]:
                    if isinstance(t, dict):
                        recent.append({"key": key, "role": t.get("role"), "text": t.get("content", t.get("text", ""))})
        return {
            "recent": recent[-40:],
            "notes": notes,
            "profiles_keys": list(profiles.keys()) if isinstance(profiles, dict) else [],
            "reflections": reflections,
            "persona": persona,
            "summary_keys": list(mem.get("summary", {}).keys()) if isinstance(mem, dict) else [],
        }

    def _api_identity(self):
        data = _root_json("identity_bindings.json", {})
        return {"data": data, "count": len(data) if isinstance(data, dict) else 0}

    def _api_brains(self):
        plugin = self.server._plugin if hasattr(self.server, "_plugin") else None
        brains = {}
        if plugin and plugin.core:
            for name in ("chat", "mc", "mc_mod", "pc", "pvz"):
                b = getattr(plugin.core, name, None)
                if b is not None:
                    st = getattr(b, "status", None)
                    brains[name] = st() if callable(st) else {"present": True}
        return {"brains": brains, "core": bool(plugin and plugin.core)}

    def _api_mc(self):
        live = _data_json("mc_bot_live.json", {}) or _data_json("mc_bot_live_tmp.json", {})
        state = _data_json("mc_game_state.json", {})
        skills = _data_json("mc_skills.json", [])
        hints = []
        hints_file = os.path.join(_DATA_DIR, "mc_hints.jsonl")
        if os.path.exists(hints_file):
            try:
                with open(hints_file, "r", encoding="utf-8") as f:
                    for line in f.read().splitlines()[-20:]:
                        if line.strip():
                            hints.append(json.loads(line))
            except Exception as e:
                degrade("libs/qq_bot_runtime/web_plugin.py:739 _Handler._api_mc", e, "降级：with open(hints_file, 'r', encoding='utf-8') as f")
        return {"live": live, "game_state": state, "skills": skills[:50] if isinstance(skills, list) else [],
                "recent_hints": hints}

    def _api_pvz(self):
        import glob
        shots = []
        for p in sorted(glob.glob(os.path.join(_DATA_DIR, "pvz_*.png")), key=os.path.getmtime, reverse=True)[:24]:
            shots.append({"name": os.path.basename(p),
                          "mtime": datetime.fromtimestamp(os.path.getmtime(p)).strftime("%m-%d %H:%M"),
                          "url": "/api/file?f=data/" + os.path.basename(p)})
        return {"shots": shots, "state_file": os.path.exists(os.path.join(_DATA_DIR, "pvz_state_now.png"))}

    def _api_bili(self):
        try:
            cfg = _import_config()
            return {
                "ENABLE_BILI_LIVE": getattr(cfg, "ENABLE_BILI_LIVE", None),
                "BILI_ROOM_ID": getattr(cfg, "BILI_ROOM_ID", None),
                "BILI_UID": getattr(cfg, "BILI_UID", None),
                "BILI_LIVE_TITLE": getattr(cfg, "BILI_LIVE_TITLE", None),
            }
        except Exception as e:
            return {"error": str(e)}

    def _api_voice(self):
        try:
            cfg = _import_config()
            return {
                "ENABLE_VOX": getattr(cfg, "ENABLE_VOX", None),
                "ENABLE_VOX_COSY": getattr(cfg, "ENABLE_VOX_COSY", None),
                "ENABLE_REALTIME_VOICE": getattr(cfg, "ENABLE_REALTIME_VOICE", None),
                "VOX_VOICE": getattr(cfg, "VOX_VOICE", None),
                "ENABLE_TTS_AFTER_REPLY": getattr(cfg, "ENABLE_TTS_AFTER_REPLY", None),
            }
        except Exception as e:
            return {"error": str(e)}

    def _api_system(self):
        plugin = self.server._plugin if hasattr(self.server, "_plugin") else None
        logs = []
        log_path = os.path.join(_BASE_DIR, "bot.log")
        if os.path.exists(log_path):
            try:
                with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
                    logs = f.read().splitlines()[-50:]
            except Exception as e:
                degrade("libs/qq_bot_runtime/web_plugin.py:786 _Handler._api_system", e, "降级：with open(log_path, 'r', encoding='utf-8', errors=")
        return {
            "core": bool(plugin and plugin.core),
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "uptime_log_lines": len(logs),
            "logs": logs,
        }

    def _api_control(self, body):
        plugin = self.server._plugin if hasattr(self.server, "_plugin") else None
        action = body.get("action")
        if action == "restart_brain":
            name = body.get("brain")
            if plugin and plugin.core and hasattr(plugin.core, name):
                b = getattr(plugin.core, name)
                if hasattr(b, "restart"):
                    try:
                        plugin._schedule(b.restart())
                        return {"ok": True}
                    except Exception as e:
                        return {"ok": False, "error": repr(e)}
            return {"ok": False, "error": "未知大脑或不可重启"}
        if action == "reset_emotion":
            try:
                if plugin and plugin.core and plugin.core.emotion:
                    plugin.core.emotion.reset()
                    return {"ok": True}
            except Exception as e:
                return {"ok": False, "error": repr(e)}
            return {"ok": False, "error": "情绪模块不可用"}
        return {"ok": False, "error": "unknown action"}

    # ---- 记忆操作（校对：清空反思/人格/记事）----
    def _api_memory_action(self, body):
        action = body.get("action")
        uid = body.get("user_id", "") or ""
        try:
            if action == "clear_reflection":
                import reflection_memory
                reflection_memory.clear_reflections(uid)
                return {"ok": True}
            if action == "clear_persona":
                import persona_memory
                persona_memory.clear_persona(uid)
                return {"ok": True}
            if action == "clear_notes":
                import important_notes
                important_notes.clear_notes(uid)
                return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": repr(e)}
        return {"ok": False, "error": f"unknown action: {action}"}

    # ---- 配置中心热重载（改完 ai_providers.json 无需重启）----
    def _api_reload_config(self):
        try:
            from ai_provider import reload_provider_config
            cfg = reload_provider_config()
            enabled = [n for n, p in cfg.get("providers", {}).items()
                       if p.get("api_key")]
            return {
                "ok": True,
                "providers_enabled": enabled,
                "capability_routing": cfg.get("capability_routing", {}),
                "vision_routing": cfg.get("vision_routing", {}),
            }
        except Exception as e:
            return {"ok": False, "error": repr(e)}


class _QuietHTTPServer(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        # 客户端（本机浏览器/前端）中断连接属正常现象：关标签页、SSE 长连接
        # 被断开、健康检查超时等都会触发。这只是让该条请求线程结束，不影响服务，
        # 默认 BaseServer.handle_error 会把整段 traceback 刷到日志，故对断连静默。
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionAbortedError, ConnectionResetError,
                             BrokenPipeError)):
            return
        super().handle_error(request, client_address)


# ----------------------------------------------------------------------------
# 独立运行入口（自带最小核心）
# ----------------------------------------------------------------------------
def build_web_core():
    """构造一个只含 web 平台 + 必要功能插件的最小核心（不连 QQ/NapCat）。

    等价于 `python main.py --web`，但不启动 QQ/控制台/B站。
    """
    from agent_core import get_core
    core = get_core()
    core.register_builtin_plugins(platforms=["web"])
    return core


async def _amain(core):
    await core.start()
    await asyncio.Future()  # 永久运行


def main():
    ap = argparse.ArgumentParser(description="肥鱼娘 Web 前端平台")
    ap.add_argument("--port", type=int, default=8800)
    ap.add_argument("--no-core", action="store_true", help="仅前端 + 只读数据，不启动聊天核心")
    args = ap.parse_args()
    os.environ["WEB_PORT"] = str(args.port)

    if args.no_core:
        WebPlugin(core=None, port=args.port).serve_no_core()
        return

    # 带核心：web 平台自己启动 HTTP 服务（在 core.start -> WebPlugin.start 中）
    try:
        core = build_web_core()
        asyncio.run(_amain(core))
    except KeyboardInterrupt:
        print("\n[WEB] 已停止")
    except Exception as e:
        print(f"\n[WEB] 核心启动失败：{e}")
        print("[WEB] 已自动回退到只读模式（仅前端 + 数据浏览，聊天不可用）。")
        print("[WEB] 如需排查核心问题，请把上方报错发给我，或运行: python web_plugin.py --no-core")
        try:
            WebPlugin(core=None, port=args.port).serve_no_core()
        except KeyboardInterrupt as e:
            degrade("libs/qq_bot_runtime/web_plugin.py:911 main", e, "降级：WebPlugin(core=None, port=args.port).serve_no_core")


if __name__ == "__main__":
    main()



def _api_memory_browser():
    out = {'reflections': [], 'rules': [], 'personas': [], 'notes': [], 'stats': {}, 'available': {}}
    try:
        import reflection_memory as rm
        out['available']['reflection'] = True
        for r in rm.get_reflections():
            out['reflections'].append({'ts': r.get('ts', 0), 'type': r.get('type', ''), 'user_id': r.get('user_id', ''), 'content': r.get('content', ''), 'evidence': r.get('evidence', ''), 'confidence': r.get('confidence'), 'actionable': r.get('actionable')})
        out['rules'] = [{'rule': x.get('rule', ''), 'confidence': x.get('confidence')} for x in rm.get_interaction_rules()]
        out['stats'] = rm.get_reflection_stats()
    except Exception as e:
        out['available']['reflection'] = False
        out['reflection_error'] = repr(e)
    try:
        import persona_memory as pm
        out['available']['persona'] = True
        for uid, p in pm.get_all_personas().items():
            out['personas'].append({'user_id': uid, 'traits': p.get('traits', []), 'style': p.get('style', ''), 'updated': p.get('updated', 0)})
    except Exception as e:
        out['available']['persona'] = False
        out['persona_error'] = repr(e)
    try:
        import important_notes as inm
        out['available']['notes'] = True
        for uid, lst in inm.load_notes().items():
            for i, n in enumerate(lst):
                out['notes'].append({'user_id': uid, 'index': i + 1, 'text': n.get('text', ''), 'category': n.get('category', ''), 'time': n.get('time', '')})
    except Exception as e:
        out['available']['notes'] = False
        out['notes_error'] = repr(e)
    return out


def _api_memory_browser_action(body):
    action = body.get('action')
    try:
        if action == 'clear_reflections':
            import reflection_memory as rm
            rm.clear_reflections()
            return {'ok': True}
        if action == 'delete_reflection':
            import reflection_memory as rm
            return {'ok': True, 'removed': rm.delete_reflection(ts=body.get('ts'), content=body.get('content'))}
        if action == 'delete_rule':
            import reflection_memory as rm
            return {'ok': True, 'removed': rm.delete_interaction_rule(rule=body.get('rule'))}
        if action == 'clear_rules':
            import reflection_memory as rm
            rm.clear_interaction_rules()
            return {'ok': True}
        if action == 'clear_personas':
            import persona_memory as pm
            pm.clear_persona()
            return {'ok': True}
        if action == 'delete_persona':
            import persona_memory as pm
            pm.delete_persona(body.get('user_id', ''))
            return {'ok': True}
        if action == 'clear_notes':
            import important_notes as inm
            return {'ok': True, 'removed': inm.clear_notes(body.get('user_id', ''))}
        if action == 'delete_note':
            import important_notes as inm
            return {'ok': True, 'removed': inm.delete_note(body.get('user_id', ''), index=body.get('index'))}
    except Exception as e:
        return {'ok': False, 'error': repr(e)}
    return {'ok': False, 'error': 'unknown action: ' + str(action)}
