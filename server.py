# -*- coding: utf-8 -*-
"""App HTTP 服务器：静态前端托管 + REST API + SSE 实时事件流（仅本机监听）。"""
import json
import mimetypes
import os
import socket
import subprocess
import sys
import base64
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from quiet import attention, degrade

import agent_ctx

APP_DIR = os.path.dirname(os.path.abspath(__file__))
WEBUI_DIR = os.path.join(APP_DIR, "webui")
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

from bridge import appearance_api, builder_api, config_api, memory_api, plugins_api, provider_api, summary_api  # noqa: E402


# ---------------------------------------------------------------------------
# 聊天媒体附件：data URL 解码落盘 + 语音转码
# ---------------------------------------------------------------------------
_MEDIA_EXT = {
    "image/png": ".png", "image/jpeg": ".jpg", "image/jpg": ".jpg",
    "image/gif": ".gif", "image/webp": ".webp", "image/bmp": ".bmp",
    "video/mp4": ".mp4", "video/webm": ".webm", "video/quicktime": ".mov",
    "video/x-matroska": ".mkv",
    "audio/wav": ".wav", "audio/x-wav": ".wav", "audio/mpeg": ".mp3",
    "audio/webm": ".weba", "audio/ogg": ".ogg", "audio/mp4": ".m4a",
    "audio/aac": ".aac", "audio/flac": ".flac",
}


def _decode_data_url(data_url):
    """data:[mime];base64,xxxx -> (bytes, mime)；非 data URL 按裸 base64 尝试。"""
    if not isinstance(data_url, str):
        return None, ""
    if data_url.startswith("data:"):
        try:
            head, b64 = data_url.split(",", 1)
            mime = head[5:].split(";")[0] or "application/octet-stream"
            return base64.b64decode(b64), mime
        except Exception:
            return None, ""
    try:
        return base64.b64decode(data_url), "application/octet-stream"
    except Exception:
        return None, ""


def _media_dir():
    d = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "media")
    os.makedirs(d, exist_ok=True)
    return d


def _save_media_file(data_url, prefix):
    """data URL -> 落盘 data/media，返回绝对路径；无效返回 None。"""
    raw, mime = _decode_data_url(data_url)
    if not raw:
        return None
    ext = _MEDIA_EXT.get(mime, ".bin")
    path = os.path.join(_media_dir(), f"{prefix}_{uuid.uuid4().hex}{ext}")
    with open(path, "wb") as f:
        f.write(raw)
    return path


def _audio_to_wav(data_url):
    """语音 data URL -> (wav_bytes, error)。wav/mp3 直接返回；webm/ogg 经 ffmpeg 转 16k 单声道 wav。

    浏览器 MediaRecorder 产出 webm/opus，引擎 ASR 需要 wav；转码复用视频处理共用的 ffmpeg
    （config.FFMPEG_PATH，未配置则尝试 PATH 中的 ffmpeg）。
    """
    raw, mime = _decode_data_url(data_url)
    if not raw:
        return None, "语音数据无效"
    if mime in ("audio/wav", "audio/x-wav", "audio/mpeg"):
        return raw, ""
    try:
        from config import FFMPEG_PATH
    except Exception:
        FFMPEG_PATH = ""
    ff = FFMPEG_PATH or "ffmpeg"
    try:
        p = subprocess.run(
            [ff, "-y", "-i", "pipe:0", "-ar", "16000", "-ac", "1", "-f", "wav", "pipe:1"],
            input=raw, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
        if p.returncode != 0 or not p.stdout:
            return None, "语音转码失败（ffmpeg 不可用或格式不支持）"
        return p.stdout, ""
    except FileNotFoundError:
        return None, "未安装 ffmpeg，无法处理该格式语音（请在「本地 AI 依赖」安装 ffmpeg）"
    except Exception as e:  # noqa: BLE001
        return None, f"语音转码异常：{e}"


class QuietServer(ThreadingHTTPServer):
    daemon_threads = True
    # Windows 上 SO_REUSEADDR 允许多个进程绑定同一端口（会静默产生僵尸实例、
    # 请求随机落到旧进程），改为独占绑定：第二个实例启动即报错，单实例有保障。
    allow_reuse_address = False

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            try:
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            except OSError as e:
                degrade("server.QuietServer.server_bind", e, "设独占绑定选项失败")
        super().server_bind()

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionAbortedError, ConnectionResetError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


# ---------------- 本地 AI 依赖按需后装 ----------------
# 首启只装最小 UI 集（requirements-ui.txt）；torch / 视觉 / 语音等重依赖在此后台安装，
# 主界面可立即使用云端功能，本地能力按需开启（检测 N 卡可装 CUDA 版）。
def _detect_nvidia():
    try:
        r = subprocess.run(["nvidia-smi"], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, shell=True)
        return r.returncode == 0
    except Exception:
        return False


_NVIDIA = _detect_nvidia()


class LocalDepInstaller:
    def __init__(self):
        self.lock = threading.Lock()
        self.state = {"running": False, "percent": 0, "log": [], "done": False,
                      "error": "", "nvidia": _NVIDIA}

    def _engine_dir(self):
        cands = [os.getcwd(), os.path.join(APP_DIR, "libs", "qq_bot_runtime")]
        for c in cands:
            if os.path.isfile(os.path.join(c, "requirements-local.txt")):
                return c
        return cands[0]

    def _count(self, path):
        n = 0
        try:
            for line in open(path, encoding="utf-8"):
                s = line.strip()
                if not s or s.startswith("#") or s.startswith("-"):
                    continue
                n += 1
        except Exception:
            pass
        return max(n, 1)

    def status(self):
        with self.lock:
            return dict(self.state)

    def start(self, cuda):
        with self.lock:
            if self.state["running"]:
                return False
            self.state.update(running=True, percent=0, log=[], done=False, error="")
        threading.Thread(target=self._run, args=(cuda,), daemon=True,
                        name="feiyu-local-deps").start()
        return True

    def _log(self, msg):
        with self.lock:
            self.state["log"].append(msg)
            if len(self.state["log"]) > 600:
                self.state["log"] = self.state["log"][-600:]
        try:
            print(msg)
        except Exception:
            pass

    def _run(self, cuda):
        try:
            eng = self._engine_dir()
            py = sys.executable
            local_req = os.path.join(eng, "requirements-local.txt")
            if cuda:
                self._log("[local] 安装 CUDA 版 torch（cu128，加速本地模型）…")
                rc = subprocess.run([py, "-m", "pip", "install", "torch",
                                     "torchvision", "torchaudio", "--index-url",
                                     "https://download.pytorch.org/whl/cu128"]).returncode
                if rc != 0:
                    raise RuntimeError("CUDA 版 torch 安装失败")
            self._log(f"[local] 安装本地 AI 依赖集：{local_req}")
            total = self._count(local_req)
            seen = set()
            proc = subprocess.Popen([py, "-m", "pip", "install", "-r", local_req],
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, bufsize=1, encoding="utf-8",
                                    errors="replace")
            for line in proc.stdout:
                line = line.rstrip("\n")
                self._log(line)
                if line.startswith("Collecting "):
                    name = line[len("Collecting "):].split()[0].split("(")[0].strip()
                    if name and name not in seen:
                        seen.add(name)
                        with self.lock:
                            self.state["percent"] = min(int(95 * len(seen) / total), 95)
            rc = proc.wait()
            if rc != 0:
                raise RuntimeError("本地 AI 依赖安装失败（可能无网络/被墙）")
            with self.lock:
                self.state.update(running=False, done=True, percent=100)
            self._log("[local] 安装完成：本地模型 / 视觉 / 语音等能力已可用")
        except Exception as e:
            with self.lock:
                self.state.update(running=False, done=False, error=repr(e))
            self._log(f"[local][ERR] {e!r}")


_local_installer = LocalDepInstaller()


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
            except Exception as e:
                degrade("server._json", e, "响应写入失败（客户端可能已断开）")

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
            except Exception as e:
                degrade("server._serve_static", e, "静态资源写入失败（客户端可能已断开）")

        def _serve_raw(self, data: bytes, ctype: str, code: int = 200):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            try:
                self.wfile.write(data)
            except Exception as e:
                degrade("server._serve_raw", e, "原始响应写入失败（客户端可能已断开）")

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
            except Exception as e:
                degrade("server._serve_sse", e, "SSE 事件流断开（客户端可能已离开）")

        def _sse_write(self, ev):
            try:
                data = json.dumps(ev, ensure_ascii=False)
                self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
                self.wfile.flush()
            except Exception as e:
                degrade("server._sse_write", e, "SSE 写入失败")

        # ---------------- 构建助手：流式对话（POST 上行的 SSE） ----------------
        def _builder_chat_stream(self, body):
            """POST 请求体上行，响应按 SSE 分块下发（Transfer-Encoding: chunked）。

            前端用 fetch + ReadableStream 逐帧解析，实现思维链 / 正文的实时增量显示。
            """
            closed = {"v": False}

            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()

            def _chunk(payload: bytes):
                self.wfile.write(b"%x\r\n" % len(payload) + payload + b"\r\n")
                self.wfile.flush()

            def emit(ev):
                if closed["v"]:
                    return
                try:
                    _chunk(b"data: " + json.dumps(ev, ensure_ascii=False).encode("utf-8")
                           + b"\n\n")
                except Exception:
                    closed["v"] = True      # 客户端断开：静默停写，等本轮结束

            try:
                _chunk(b": open\n\n")
            except Exception:
                closed["v"] = True
            try:
                body = dict(body or {})
                body["stream"] = True
                res = builder_api.run_chat_sync(bridge, body, emit=emit)
                if isinstance(res, dict) and not res.get("streamed"):
                    # 供应商不支持流式（如 Anthropic 路径）→ 明确告知界面降级
                    emit({"type": "notice",
                          "text": "当前供应商不支持流式输出，已按非流式返回。"})
                    emit({"type": "done", "payload": res})
            except Exception as e:
                emit({"type": "error", "error": "%r" % e})
            try:
                self.wfile.write(b"0\r\n\r\n")      # chunked 结束帧
                self.wfile.flush()
            except Exception as e:
                degrade("server._builder_chat_stream", e, "chunked 结束帧写入失败")

        # ---------------- GET 路由 ----------------
        def do_GET(self):
            parsed = urlparse(self.path)
            path = parsed.path
            q = self._qs()
            # 按 bot_id 路由：记忆子系统按 agent_ctx.current_agent() 隔离各 bot 数据
            bid = (q.get("bot_id") or ["feiyu"])[0] or "feiyu"
            tok = agent_ctx.set_agent(bid)
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
                # 轻量启动探针：前端 splash 用它判断是否已连上后端（先启动 UI，后端就绪即隐藏遮罩）
                if path == "/api/boot/status":
                    return self._json({"ready": True, "core": bridge.is_running()})
                # 本地 AI 依赖按需后装：状态查询
                if path == "/api/system/install_local_deps":
                    return self._json(_local_installer.status())
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
                    return self._json(summary_api.summary_overview(bridge, bid))
                if path == "/api/bots":
                    return self._json({"bots": bridge.bot_manager.list_specs(), "default": "feiyu"})
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
                # ----- 构建助手：文件上下文 / 构建历史 / 会话（只读查询，走 GET） -----
                if path == "/api/builder/files":
                    return self._json(builder_api.list_context_files_sync())
                # 磁盘目录浏览（工作区「从硬盘选择」用；只列目录，只读）
                if path == "/api/fs/dirs":
                    return self._json(builder_api.list_disk_dirs_sync(
                        (q.get("path") or [""])[0]))
                if path == "/api/builder/file/read":
                    return self._json(builder_api.read_context_file_sync((q.get("path") or [""])[0]))
                if path == "/api/builder/history":
                    return self._json(builder_api.get_history_sync(int((q.get("limit") or ["60"])[0])))
                if path == "/api/builder/sessions":
                    return self._json(builder_api.list_chat_sessions())
                if path == "/api/builder/settings":
                    return self._json(builder_api.get_settings_sync())
                if path == "/api/builder/approvals":
                    return self._json(builder_api.list_approvals_sync())
                if path == "/api/builder/rules":
                    return self._json(builder_api.list_rules_sync())
                if path == "/api/builder/session":
                    return self._json({"ok": True, "state": builder_api.load_chat_session(
                        (q.get("name") or [""])[0])})
                # ----- 智能体自编程：Issue 队列（只读查询走 GET） -----
                if path == "/api/self_coding/issues":
                    import self_coding
                    st = (q.get("state") or [""])[0] or None
                    return self._json({"issues": self_coding.list_issues(st),
                                       "enabled": self_coding.is_enabled()})
                return self._json({"error": "not found"}, 404)
            except Exception as e:
                return self._json({"error": repr(e)}, 500)
            finally:
                agent_ctx.reset_agent(tok)

        # ---------------- POST 路由 ----------------
        def do_POST(self):
            path = urlparse(self.path).path
            body = self._body()
            # 按 bot_id 路由：记忆子系统按 agent_ctx.current_agent() 隔离各 bot 数据
            bid = (body.get("bot_id") or (self._qs().get("bot_id") or ["feiyu"])[0] or "feiyu")
            tok = agent_ctx.set_agent(bid or "feiyu")
            try:
                # 本地 AI 依赖按需后装：触发安装（mode=cpu|cuda）
                if path == "/api/system/install_local_deps":
                    mode = (body.get("mode") or "cpu")
                    ok = _local_installer.start(cuda=(mode == "cuda"))
                    return self._json({"ok": ok, "status": _local_installer.status()})
                if path == "/api/chat":
                    body_text = body.get("text", "") or ""
                    images = [u for u in (body.get("images") or []) if isinstance(u, str)]
                    video = body.get("video") or None
                    audio = body.get("audio") or None
                    image_paths, video_path, audio_wav, audio_err = [], None, None, None
                    for u in images:
                        p = _save_media_file(u, "img")
                        if p:
                            image_paths.append(p)
                    if video:
                        video_path = _save_media_file(video, "vid")
                    if audio:
                        audio_wav, audio_err = _audio_to_wav(audio)
                    if audio and audio_err:
                        return self._json({"ok": False, "error": audio_err})
                    if not body_text.strip() and not image_paths and not video_path and not audio_wav:
                        return self._json({"ok": False, "error": "消息为空"})
                    return self._json(bridge.submit_chat(
                        text=body_text,
                        session=body.get("session", "web"),
                        user_id=body.get("user_id", "app_owner"),
                        name=body.get("name", "主人"),
                        bot_id=body.get("bot_id"),
                        image_paths=image_paths,
                        video_path=video_path,
                        audio_wav=audio_wav))
                if path == "/api/core/start":
                    return self._json(bridge.start(wait=True))
                if path == "/api/core/stop":
                    return self._json(bridge.stop(wait=True))
                if path == "/api/core/restart":
                    return self._json(bridge.restart())
                # ----- 多 bot 管理 -----
                if path == "/api/bots/create":
                    return self._json(bridge.bot_manager.create_bot(
                        name=body.get("name", "新 Bot"),
                        persona=body.get("persona"),
                        model=body.get("model"),
                        plugins=body.get("plugins"),
                        autostart=bool(body.get("autostart", False))))
                if path == "/api/bots/update":
                    return self._json(bridge.bot_manager.update_bot(
                        body.get("id", ""), **{
                            "name": body.get("name"),
                            "persona": body.get("persona"),
                            "model": body.get("model"),
                            "plugins": body.get("plugins"),
                            "autostart": body.get("autostart"),
                            "enabled": body.get("enabled"),
                        }))
                if path == "/api/bots/start":
                    return self._json(bridge.bot_manager.start_bot(body.get("id", ""), wait=True))
                if path == "/api/bots/stop":
                    return self._json(bridge.bot_manager.stop_bot(body.get("id", ""), wait=True))
                if path == "/api/bots/delete":
                    return self._json({"ok": bridge.bot_manager.delete_bot(body.get("id", ""))})
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
                    return self._json(summary_api.set_session_summary(bridge, body.get("text", ""), bid))
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
                # ----- 构建助手：对话式 Agent（工具循环）与会话管理 -----
                if path == "/api/builder/chat":
                    return self._json(builder_api.run_chat_sync(bridge, body))
                if path == "/api/builder/chat/stream":
                    return self._builder_chat_stream(body)
                if path == "/api/builder/session/new":
                    return self._json(builder_api.new_chat_session(body.get("title", "") or ""))
                if path == "/api/builder/session/delete":
                    return self._json(builder_api.delete_chat_session(body.get("name", "") or ""))
                # ----- 构建助手：权限 / 工作区设置 与 高危操作审批 -----
                if path == "/api/builder/settings/save":
                    return self._json(builder_api.set_settings_sync(body))
                if path == "/api/builder/approval/approve":
                    return self._json(builder_api.approve_approval_http_sync(
                        bridge, body.get("id", "") or "",
                        remember=bool(body.get("remember")),
                        scope=body.get("scope") or "once"))
                if path == "/api/builder/approval/approve_all":
                    return self._json(builder_api.approve_all_http_sync(
                        bridge, remember=bool(body.get("remember")),
                        scope=body.get("scope") or "once"))
                if path == "/api/builder/approval/reject":
                    return self._json(builder_api.reject_approval_sync(body.get("id", "") or ""))
                if path == "/api/builder/approval/clear":
                    return self._json(builder_api.clear_approvals_sync())
                if path == "/api/builder/rules/delete":
                    return self._json(builder_api.delete_rule_sync(body.get("id", "") or ""))
                if path == "/api/builder/rules/clear":
                    return self._json(builder_api.clear_rules_sync())
                # ----- 构建助手：智能体 / 插件 生成与落盘 -----
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
                # ----- 智能体自编程：Issue 批准 / 拒绝 / 手动提交 -----
                if path == "/api/self_coding/approve":
                    import self_coding
                    return self._json(self_coding.approve_issue(body.get("id", "")))
                if path == "/api/self_coding/reject":
                    import self_coding
                    return self._json(self_coding.reject_issue(body.get("id", "")))
                if path == "/api/self_coding/retry":
                    import self_coding
                    return self._json(self_coding.retry_issue(body.get("id", "")))
                if path == "/api/self_coding/file":
                    import self_coding
                    return self._json(self_coding.file_issue(
                        title=body.get("title", ""), body=body.get("body", ""),
                        kind=body.get("kind", "feature")))
                return self._json({"error": "not found"}, 404)
            except Exception as e:
                return self._json({"error": repr(e)}, 500)
            finally:
                agent_ctx.reset_agent(tok)

        def _run_summary(self, body):
            import asyncio
            return bridge.lt.run_coro(summary_api.run_summary(
                user_id=body.get("uid", "app_owner"),
                count=int(body.get("count", 60)),
                save_to=body.get("save_to", "none"),
                keyword=body.get("keyword", ""),
                bot_id=body.get("bot_id")), timeout=180)

    return Handler


def start_server(bridge, port: int) -> QuietServer:
    srv = QuietServer(("127.0.0.1", port), make_handler(bridge))
    threading = __import__("threading")
    t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.5},
                         name="feiyu-app-http", daemon=True)
    t.start()
    return srv
