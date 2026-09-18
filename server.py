# -*- coding: utf-8 -*-
"""App HTTP 服务器：静态前端托管 + REST API + SSE 实时事件流（仅本机监听）。"""
import json
import mimetypes
import os
import socket
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

APP_DIR = os.path.dirname(os.path.abspath(__file__))
WEBUI_DIR = os.path.join(APP_DIR, "webui")
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

from bridge import appearance_api, builder_api, config_api, memory_api, plugins_api, provider_api, summary_api  # noqa: E402


class QuietServer(ThreadingHTTPServer):
    daemon_threads = True
    # Windows 上 SO_REUSEADDR 允许多个进程绑定同一端口（会静默产生僵尸实例、
    # 请求随机落到旧进程），改为独占绑定：第二个实例启动即报错，单实例有保障。
    allow_reuse_address = False

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            try:
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            except OSError:
                pass
        super().server_bind()

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionAbortedError, ConnectionResetError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


def make_handler(bridge):
    """bridge: CoreBridge 单例。返回 Handler 类。"""

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        # ---------------- 基础输出 ----------------
        def _json(self, obj, code=200):
            data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                self.wfile.write(data)
            except Exception:
                pass

        def _body(self):
            try:
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b"{}"
                return json.loads(raw.decode("utf-8") or "{}")
            except Exception:
                return {}

        def _qs(self):
            return parse_qs(urlparse(self.path).query)

        # ---------------- 静态文件 ----------------
        def _serve_static(self, rel):
            path = os.path.normpath(os.path.join(WEBUI_DIR, rel))
            if not path.startswith(WEBUI_DIR) or not os.path.isfile(path):
                return self._json({"error": "not found"}, 404)
            ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
            if path.endswith((".html", ".js", ".css")):
                ctype += "; charset=utf-8"
            with open(path, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                self.wfile.write(data)
            except Exception:
                pass

        def _serve_raw(self, data: bytes, ctype: str, code: int = 200):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            try:
                self.wfile.write(data)
            except Exception:
                pass

        def _serve_file(self, q):
            """受控文件服务：只允许 qq_bot 与本应用目录内的文件。"""
            p = (q.get("path") or [""])[0]
            try:
                ap = os.path.abspath(p)
                allowed = [os.path.abspath(os.getcwd()), APP_DIR]
                if not any(ap.startswith(a) for a in allowed):
                    return self._json({"error": "forbidden"}, 403)
                if not os.path.isfile(ap):
                    return self._json({"error": "not found"}, 404)
                ctype = mimetypes.guess_type(ap)[0] or "application/octet-stream"
                with open(ap, "rb") as f:
                    data = f.read()
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except Exception as e:
                self._json({"error": repr(e)}, 500)

        # ---------------- SSE ----------------
        def _serve_sse(self, q):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            try:
                for ev in bridge.recent(40):
                    self._sse_write(ev)
                self.wfile.write(b": ping\n\n")
                self.wfile.flush()
                while True:
                    try:
                        ev = q.get(timeout=15)
                        self._sse_write(ev)
                    except Exception:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
            except Exception:
                pass

        def _sse_write(self, ev):
            try:
                data = json.dumps(ev, ensure_ascii=False)
                self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
                self.wfile.flush()
            except Exception:
                pass

        # ---------------- GET 路由 ----------------
        def do_GET(self):
            parsed = urlparse(self.path)
            path = parsed.path
            q = self._qs()
            try:
                if path in ("/", "/index.html"):
                    return self._serve_static("index.html")
                if path.startswith("/static/"):
                    return self._serve_static(path[len("/static/"):])
                if path == "/api/events":
                    session = (q.get("session") or ["web"])[0]
                    qq = bridge.subscribe(session)
                    try:
                        return self._serve_sse(qq)
                    finally:
                        # 连接断开必须退订，否则队列泄漏且重连后旧连接仍占着回放现场
                        bridge.unsubscribe(session, qq)
                if path == "/api/file":
                    return self._serve_file(q)
                if path == "/api/status":
                    return self._json(bridge.status())
                if path == "/api/recent":
                    return self._json({"events": bridge.recent(60)})
                if path == "/api/memories":
                    return self._json(memory_api.get_sessions())
                if path == "/api/memory/stats":
                    return self._json(memory_api.memory_stats())
                if path == "/api/memory/export":
                    return self._json(memory_api.export_memory())
                if path == "/api/memory/users":
                    return self._json({"items": memory_api.list_users()})
                if path == "/api/memory/profiles":
                    return self._json(memory_api.get_profiles(uid=(q.get("uid") or [""])[0]))
                if path == "/api/memory/notes":
                    return self._json(memory_api.get_notes(uid=(q.get("uid") or [""])[0],
                                                           q=(q.get("q") or [""])[0]))
                if path == "/api/memory/persona":
                    return self._json(memory_api.get_persona(uid=(q.get("uid") or [""])[0]))
                if path == "/api/memory/reflection":
                    return self._json(memory_api.get_reflection(uid=(q.get("uid") or [""])[0],
                                                                q=(q.get("q") or [""])[0]))
                if path == "/api/memory/knowledge":
                    return self._json(memory_api.get_knowledge(q=(q.get("q") or [""])[0],
                                                               limit=int((q.get("limit") or ["100"])[0])))
                if path == "/api/memory/history":
                    return self._json(memory_api.get_history(uid=(q.get("uid") or [""])[0],
                                                             q=(q.get("q") or [""])[0],
                                                             limit=int((q.get("limit") or ["100"])[0])))
                if path == "/api/summary/overview":
                    return self._json(summary_api.summary_overview(bridge))
                if path == "/api/plugins":
                    return self._json(plugins_api.list_plugins(bridge))
                if path == "/api/plugins/market":
                    return self._json(plugins_api.market_list(bridge))
                if path.startswith("/api/plugins/") and path.endswith("/config"):
                    name = path[len("/api/plugins/"):-len("/config")].strip("/")
                    if name:
                        return self._json(plugins_api.plugin_config_view(name))
                if path == "/api/config":
                    return self._json(config_api.config_view())
                # ----- 主模型供应商预设 / 当前配置 -----
                if path == "/api/providers/presets":
                    return self._json(provider_api.list_presets())
                if path == "/api/providers":
                    return self._json(provider_api.list_models())
                if path in ("/api/providers/main", "/api/providers/vision", "/api/providers/role"):
                    slot = path.rsplit("/", 1)[-1]
                    return self._json(provider_api.get_provider(slot))
                if path == "/api/appearance":
                    return self._json(appearance_api.get_appearance(
                        (q.get("theme") or [""])[0] or None))
                if path.startswith("/api/appearance/bg"):
                    raw = appearance_api.read_bg()
                    if not raw:
                        return self._serve_raw(b"", "image/png", 404)
                    return self._serve_raw(raw, appearance_api._bg_content_type(raw))
                return self._json({"error": "not found"}, 404)
            except Exception as e:
                return self._json({"error": repr(e)}, 500)

        # ---------------- POST 路由 ----------------
        def do_POST(self):
            path = urlparse(self.path).path
            body = self._body()
            try:
                if path == "/api/chat":
                    return self._json(bridge.submit_chat(
                        text=body.get("text", ""),
                        session=body.get("session", "web"),
                        user_id=body.get("user_id", "app_owner"),
                        name=body.get("name", "主人")))
                if path == "/api/core/start":
                    return self._json(bridge.start(wait=True))
                if path == "/api/core/stop":
                    return self._json(bridge.stop(wait=True))
                if path == "/api/core/restart":
                    return self._json(bridge.restart())
                if path == "/api/memory/action":
                    return self._json(memory_api.memory_action(body.get("kind", ""),
                                                               body.get("payload") or body))
                if path == "/api/memory/import":
                    return self._json(memory_api.import_memory(body.get("data"), body.get("mode", "replace")))
                if path == "/api/memory/import_chatlog":
                    return self._json(memory_api.import_chatlog(
                        body.get("text", ""), body.get("uid") or "app_owner",
                        body.get("target", "knowledge"), body.get("mode", "auto")))
                if path == "/api/summary/session":
                    return self._json(summary_api.set_session_summary(bridge, body.get("text", "")))
                if path == "/api/summary/run":
                    return self._json(self._run_summary(body))
                if path == "/api/plugins/toggle":
                    return self._json(plugins_api.toggle(bridge, body.get("name", ""),
                                                         bool(body.get("on"))))
                if path == "/api/plugins/group":
                    return self._json(plugins_api.group_action(
                        bridge, body.get("group", ""), body.get("action", "on")))
                if path == "/api/plugins/brain":
                    return self._json(plugins_api.brain_action(bridge, body.get("name", ""),
                                                               body.get("action", "status")))
                if path == "/api/plugins/sidecar":
                    return self._json(plugins_api.sidecar_action(bridge, body.get("name", ""),
                                                                 body.get("action", "status")))
                if path == "/api/plugins/rescan":
                    return self._json(plugins_api.rescan(bridge))
                if path.startswith("/api/plugins/") and path.endswith("/config"):
                    name = path[len("/api/plugins/"):-len("/config")].strip("/")
                    if name:
                        return self._json(plugins_api.plugin_config_save(name, body.get("values") or {}))
                if path == "/api/plugins/market":
                    return self._json(plugins_api.market_action(
                        bridge, body.get("action", ""),
                        body.get("name", "")))
                if path == "/api/plugins/seek":
                    return self._json(plugins_api.market_seek(bridge))
                if path == "/api/plugins/local":
                    return self._json(plugins_api.toggle(bridge, body.get("name", ""),
                                                         bool(body.get("on"))))
                if path == "/api/config/save":
                    return self._json(config_api.config_save(body.get("values") or {}))
                if path == "/api/app/settings":
                    return self._json(config_api.save_app_settings(body))
                if path == "/api/appearance":
                    return self._json(appearance_api.save(
                        theme=body.get("theme"), title=body.get("title"),
                        bg=body.get("bg"), bg_data=body.get("bg_data"),
                        clear_bg=bool(body.get("clear_bg"))))
                if path == "/api/appearance/icon":
                    try:
                        return self._json(appearance_api.save_icon(body.get("icon_data") or ""))
                    except ValueError as e:
                        return self._json({"error": str(e)}, 400)
                if path == "/api/appearance/icon/reset":
                    try:
                        return self._json(appearance_api.reset_icon())
                    except ValueError as e:
                        return self._json({"error": str(e)}, 400)
                # ----- 构建助手：智能体 / 插件 生成与落盘 -----
                if path == "/api/builder/files":
                    return self._json(builder_api.list_context_files_sync())
                if path == "/api/builder/file/read":
                    return self._json(builder_api.read_context_file_sync(body.get("path", "")))
                if path == "/api/builder/history":
                    return self._json(builder_api.get_history_sync(int(body.get("limit", 60))))
                if path == "/api/builder/agent/generate":
                    return self._json(builder_api.generate_agent_sync(
                        bridge, body.get("requirement", ""),
                        body.get("model") or None, body.get("think") or "low",
                        body.get("provider") or None,
                        body.get("context_paths") or None,
                        body.get("use_history", True)))
                if path == "/api/builder/plugin/generate":
                    return self._json(builder_api.generate_plugin_sync(
                        bridge, body.get("requirement", ""), body.get("kind", "") or "",
                        body.get("model") or None, body.get("think") or "low",
                        body.get("provider") or None,
                        body.get("context_paths") or None,
                        body.get("use_history", True)))
                if path == "/api/builder/agent/improve":
                    return self._json(builder_api.improve_agent_sync(
                        bridge, body.get("id", ""),
                        body.get("instruction", "") or "",
                        body.get("model") or None, body.get("think") or "low",
                        body.get("provider") or None,
                        body.get("context_paths") or None,
                        body.get("use_history", True)))
                if path == "/api/builder/plugin/improve":
                    return self._json(builder_api.improve_plugin_sync(
                        bridge, body.get("name", ""),
                        body.get("instruction", "") or "",
                        body.get("model") or None, body.get("think") or "low",
                        body.get("provider") or None,
                        body.get("context_paths") or None,
                        body.get("use_history", True)))
                if path == "/api/builder/agent/save":
                    return self._json(builder_api.save_agent_sync(bridge, body.get("data") or {}))
                if path == "/api/builder/agent/import":
                    return self._json(builder_api.import_agent_sync(bridge, body.get("agent") or {}))
                if path == "/api/builder/agent/connect_external":
                    return self._json(builder_api.connect_external_agent_sync(bridge, body))
                if path == "/api/builder/plugin/save":
                    return self._json(builder_api.save_plugin_sync(bridge, body))
                # ----- 构建助手 · 工作区读写（让构建助手像代码 Agent 一样直接改源码） -----
                if path == "/api/builder/workspace/list":
                    return self._json(builder_api.list_workspace_sync(body.get("dir", "") or ""))
                if path == "/api/builder/workspace/read":
                    return self._json(builder_api.read_workspace_sync(body.get("path", "")))
                if path == "/api/builder/workspace/write":
                    return self._json(builder_api.write_workspace_sync(
                        body.get("path", ""), body.get("content", "") or "",
                        backup=bool(body.get("backup", True))))
                if path == "/api/builder/workspace/diff":
                    return self._json(builder_api.workspace_diff_sync(
                        body.get("path", ""), body.get("content", "") or ""))
                if path == "/api/builder/plugin/files":
                    return self._json(builder_api.list_plugin_files_sync(body.get("name", "")))
                # ----- 主模型供应商配置保存 -----
                if path == "/api/providers":
                    return self._json(provider_api.save_model(body))
                if path == "/api/providers/activate":
                    return self._json(provider_api.set_default(
                        body.get("capability", ""), body.get("name", "")))
                if path == "/api/providers/delete":
                    return self._json(provider_api.delete_model(body.get("name", "")))
                if path in ("/api/providers/main", "/api/providers/vision", "/api/providers/role"):
                    slot = path.rsplit("/", 1)[-1]
                    return self._json(provider_api.save_provider(slot, body))
                return self._json({"error": "not found"}, 404)
            except Exception as e:
                return self._json({"error": repr(e)}, 500)

        def _run_summary(self, body):
            import asyncio
            return bridge.lt.run_coro(summary_api.run_summary(
                user_id=body.get("uid", "app_owner"),
                count=int(body.get("count", 60)),
                save_to=body.get("save_to", "none"),
                keyword=body.get("keyword", "")), timeout=180)

    return Handler


def start_server(bridge, port: int) -> QuietServer:
    srv = QuietServer(("127.0.0.1", port), make_handler(bridge))
    threading = __import__("threading")
    t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.5},
                         name="feiyu-app-http", daemon=True)
    t.start()
    return srv
