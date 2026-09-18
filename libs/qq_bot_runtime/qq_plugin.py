# -*- coding: utf-8 -*-
"""QQ 平台插件：把 QQ（OneBot v11 / NapCat 反向 WebSocket）接入智能体核心。

职责边界（聊天大脑在 chat_service，这里只做"传声筒"）：
- 协议转换：OneBot 消息事件 -> InboundMessage（文本/图片引用/语音解码/视频引用/引用消息）
- 消息呈现：ReplyTarget 实现（QQ 表情码解析、表情包图片、按句拆分、发送限速）
- 媒体抓取：图片字节下载、视频文件获取（NapCat 的 file/url 各种姿势）
- 私聊聚合：用户连发多条消息时等待合并
- 主动发送：实现 MessageSender（供主动说话、余额提醒等核心模块使用）

将来接入 B 站/直播等平台时，参照本文件实现 PlatformPlugin 即可，核心不用改。
"""
import asyncio
import json
import os
import re
import tempfile
import time

import httpx
import websockets

import config
import voice_client
from message_bus import InboundMessage, MessageSender, ReplyTarget
from plugin_base import PlatformPlugin
from quiet import degrade


# ============================================================================
# QQ 自带表情映射：文本代码 -> QQ face id（QQ 特有，属于呈现层）
# ============================================================================
FACE_MAP = {
    "旺柴": 210, "汪汪": 210, "狗头": 210,
    "笑哭": 20, "笑cry": 20,
    "调皮": 12, "吐舌": 13,
    "得意": 4, "开心": 5,
    "惊讶": 0, "发呆": 6,
    "流泪": 9, "哭": 9,
    "愤怒": 11, "生气": 11,
    "冷汗": 35, "尴尬": 35,
    "白眼": 22, "不屑": 22,
    "委屈": 26, "可怜": 26,
    "亲亲": 109, "爱心": 66,
    "玫瑰": 63, "礼物": 69,
    "赞": 73, "大拇指": 73, "点赞": 73,
    "胜利": 74, "握手": 78,
    "ok": 76, "OK": 76,
    "太阳": 74, "月亮": 75,
    "嘘": 21, "睡觉": 84,
    "抓狂": 106, "晕": 34,
    "衰": 96, "骷髅": 87,
    "抱拳": 81, "菜刀": 68,
}


def parse_reply_segments(text: str) -> list:
    """把回复文本解析成 OneBot message 段数组。

    识别 [表情名] 形式的 QQ 自带表情，转成 face 段；
    识别 [表情包:文件名] 标记，转成本地图片 image 段；
    其余文字保持 text 段。
    """
    segments = []
    pattern = re.compile(r'\[([^\[\]]{1,40})\]')
    last = 0
    for m in pattern.finditer(text):
        if m.start() > last:
            segments.append({"type": "text", "data": {"text": text[last:m.start()]}})
        inner = m.group(1)
        if inner.startswith("表情包:"):
            fname = inner.split(":", 1)[1].strip()
            base_dir = os.path.dirname(os.path.abspath(__file__))
            img_path = os.path.join(base_dir, config.EMOJI_DIR, fname)
            if os.path.exists(img_path):
                segments.append({"type": "image", "data": {"file": img_path}})
            # 图片不存在（AI 编造的文件名）时直接丢弃
        elif inner in FACE_MAP:
            segments.append({"type": "face", "data": {"id": str(FACE_MAP[inner])}})
        else:
            segments.append({"type": "text", "data": {"text": m.group(0)}})
        last = m.end()
    if last < len(text):
        segments.append({"type": "text", "data": {"text": text[last:]}})
    return segments if segments else [{"type": "text", "data": {"text": text}}]


def split_text_and_images(text: str) -> list:
    """把回复文本拆成多条消息：文字（含 QQ 自带表情）和表情包图片分开。"""
    segments = parse_reply_segments(text)
    text_msg = []
    image_msgs = []
    for seg in segments:
        if seg.get("type") == "image":
            image_msgs.append([seg])
        else:
            text_msg.append(seg)
    result = []
    if text_msg:
        result.extend(split_text_segments_by_sentence(text_msg))
    result.extend(image_msgs)
    return result if result else [[{"type": "text", "data": {"text": text}}]]


def split_text_segments_by_sentence(text_msg: list) -> list:
    """把文字消息按句拆分，face 表情跟随最后一条。"""
    if not config.SPLIT_REPLY_BY_SENTENCE:
        return [text_msg]
    full_text = "".join(seg["data"]["text"] for seg in text_msg if seg.get("type") == "text")
    face_segs = [seg for seg in text_msg if seg.get("type") == "face"]
    sentences = re.split(r'(?<=[。！？!?；;\n])', full_text)
    sentences = [s.strip() for s in sentences if s.strip()]
    if len(sentences) <= 1:
        return [text_msg]
    result = []
    per = config.SENTENCES_PER_MESSAGE
    for i in range(0, len(sentences), per):
        chunk = "".join(sentences[i:i + per])
        result.append([{"type": "text", "data": {"text": chunk}}])
    if face_segs and result:
        result[-1].extend(face_segs)
    return result


# ============================================================================
# OneBot 消息解析（事件 -> InboundMessage 的原料）
# ============================================================================

def extract_text(message: list) -> str:
    parts = []
    for seg in message:
        if seg.get("type") == "text":
            parts.append(seg.get("data", {}).get("text", ""))
    return "".join(parts)


def extract_reply(message: list) -> dict:
    """提取引用（回复）段。"""
    for seg in message:
        if seg.get("type") == "reply":
            data = seg.get("data", {})
            return {"id": data.get("id"), "user_id": data.get("user_id") or data.get("qq")}
    return {}


def extract_images(message: list) -> list:
    """提取图片段（返回段 data 引用，不下载）。"""
    refs = []
    for seg in message:
        if seg.get("type") == "image":
            refs.append(seg.get("data", {}))
    return refs


def is_mentioned(data: dict) -> bool:
    """判断群里是否 @了机器人（含 @全体成员）。"""
    self_id = data.get("self_id")
    for seg in data.get("message", []):
        if seg.get("type") == "at":
            qq = str(seg.get("data", {}).get("qq", ""))
            if qq == str(self_id) or qq == "all":
                return True
    return False


def _is_http_url(s: str) -> bool:
    return bool(s) and s.startswith(("http://", "https://"))


# ============================================================================
# QQ 回复目标：ChatService 通过它回复消息
# ============================================================================
class QQReplyTarget(ReplyTarget):
    """一次 QQ 会话的回复上下文。"""

    capabilities = {"group": True, "voice": True, "image": True, "voice_input": True, "video_input": True}

    def __init__(self, plugin: "QQPlugin", msg: InboundMessage):
        super().__init__(msg)
        self.plugin = plugin

    async def reply(self, text: str):
        """回复文本：拆分文字/表情/图片为多条消息逐条发送。"""
        ws = self.plugin.ws
        if not ws:
            print("[QQ-PLUGIN] ws 未连接，回复丢弃")
            return
        message_type = self.msg.channel_type
        target = self.msg.channel_id
        msg_list = split_text_and_images(text)
        for i, segments in enumerate(msg_list):
            valid_segments = []
            for seg in segments:
                if seg.get("type") == "text" and not seg.get("data", {}).get("text", "").strip():
                    continue
                valid_segments.append(seg)
            if not valid_segments:
                continue
            payload = {
                "action": "send_msg",
                "params": {"message_type": message_type, "message": valid_segments},
                "echo": f"reply-{self.msg.message_id}-{i}",
            }
            if message_type == "group":
                payload["params"]["group_id"] = target
            else:
                payload["params"]["user_id"] = target
            await ws.send(json.dumps(payload, ensure_ascii=False))
            await asyncio.sleep(config.SEND_INTERVAL_SECONDS)

    async def reply_voice(self, wav_path: str):
        """发送语音消息（QQ record 段）。"""
        ws = self.plugin.ws
        if not ws:
            return
        message_type = self.msg.channel_type
        target = self.msg.channel_id
        payload = {
            "action": "send_msg",
            "params": {
                "message_type": message_type,
                "message": [{"type": "record", "data": {"file": wav_path}}],
            },
            "echo": f"voice-{self.msg.message_id}",
        }
        if message_type == "group":
            payload["params"]["group_id"] = target
        else:
            payload["params"]["user_id"] = target
        await ws.send(json.dumps(payload, ensure_ascii=False))

    async def fetch_image(self, ref) -> bytes:
        """下载图片二进制（ref 是 OneBot image 段 data）。"""
        return await get_image_bytes(self.plugin.ws, ref or {})

    async def fetch_video(self, ref) -> str:
        """获取视频本地文件路径（ref = {"data": video段data, "message_id": id}）。"""
        ref = ref or {}
        return await get_video_file(
            self.plugin.ws, ref.get("data", {}),
            message_type=self.msg.channel_type,
            group_id=self.msg.channel_id,
            user_id=self.msg.user_id,
            message_id=ref.get("message_id"),
        )


# ============================================================================
# 媒体抓取（NapCat 的文件接口姿势多，保留原有完整兜底链）
# ============================================================================

async def call_action(ws, action: str, params: dict) -> dict:
    """调用 OneBot API 并等待返回结果（echo + Future 机制）。"""
    echo = f"call-{id(params)}"
    payload = {"action": action, "params": params, "echo": echo}
    loop = asyncio.get_event_loop()
    future = loop.create_future()
    plugin_pending_calls[echo] = future
    try:
        await ws.send(json.dumps(payload, ensure_ascii=False))
        result = await asyncio.wait_for(future, timeout=30)
        return result
    except asyncio.TimeoutError:
        print(f"[WARN] 等待 {action} 响应超时")
        return None
    except Exception as e:
        print(f"[WARN] 等待 {action} 响应失败: {e}")
        return None
    finally:
        plugin_pending_calls.pop(echo, None)


# echo -> Future（模块级，和 ws 会话一一对应；断线时清理）
plugin_pending_calls = {}


async def get_image_bytes(ws, img_data: dict) -> bytes:
    """获取图片二进制：先直接下载 url，失败用 get_image API 刷新后重试。"""
    file = img_data.get("file", "")
    url = img_data.get("url", "")
    if url:
        try:
            async with httpx.AsyncClient(timeout=60, follow_redirects=True) as c:
                resp = await c.get(url)
                if resp.status_code == 200 and len(resp.content) > 0:
                    return resp.content
                print(f"[WARN] URL 下载失败，状态码 {resp.status_code}")
        except Exception as e:
            print(f"[WARN] URL 下载异常: {e}")
    if file:
        try:
            result = await call_action(ws, "get_image", {"file": file})
            if isinstance(result, dict):
                local_path = result.get("file")
                if local_path and isinstance(local_path, str) and not local_path.startswith(("http://", "https://")):
                    try:
                        with open(local_path, "rb") as f:
                            content = f.read()
                            if len(content) > 0:
                                return content
                    except OSError as e:
                        print(f"[WARN] 本地文件读取失败: {e}")
                new_url = result.get("url")
                if new_url:
                    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as c:
                        resp = await c.get(new_url)
                        if resp.status_code == 200 and len(resp.content) > 0:
                            return resp.content
        except Exception as e:
            print(f"[WARN] get_image 获取失败: {e}")
    raise RuntimeError("无法获取图片")


async def _download_to_temp(url: str, suffix: str = ".mp4") -> str:
    tmp_dir = getattr(config, "VIDEO_TMP_DIR", "") or tempfile.gettempdir()
    os.makedirs(tmp_dir, exist_ok=True)
    try:
        async with httpx.AsyncClient(timeout=120, follow_redirects=True) as c:
            resp = await c.get(url)
            if resp.status_code == 200 and len(resp.content) > 0:
                fd, tmp_path = tempfile.mkstemp(dir=tmp_dir, suffix=suffix)
                with os.fdopen(fd, "wb") as f:
                    f.write(resp.content)
                return tmp_path
            print(f"[WARN] 视频 URL 下载失败，状态码 {resp.status_code}")
    except Exception as e:
        print(f"[WARN] 视频 URL 下载异常: {e}")
    return ""


async def get_video_file(ws, video_data: dict, message_type: str = "", group_id="", user_id="", message_id=None) -> str:
    """获取视频文件，返回本地路径（保留原有完整兜底链）。"""
    path = video_data.get("path") or ""
    file_id = video_data.get("file") or ""
    url = video_data.get("url") or ""

    candidates = []
    for v in (path, url, file_id):
        if v and v not in candidates:
            candidates.append(v)

    for v in candidates:
        if not _is_http_url(v) and os.path.exists(v):
            return v

    for v in candidates:
        if _is_http_url(v):
            print(f"[INFO] 检测到视频直链，直接下载: {v[:80]}...")
            tmp = await _download_to_temp(v)
            if tmp:
                return tmp

    wait_time = getattr(config, "VIDEO_FILE_WAIT_SECONDS", 30)
    if wait_time > 0:
        for v in candidates:
            if v and not _is_http_url(v):
                print(f"[INFO] 视频文件未就绪，等待下载: {v}（最多 {wait_time} 秒）")
                deadline = time.time() + wait_time
                while time.time() < deadline:
                    await asyncio.sleep(3)
                    if os.path.exists(v) and os.path.getsize(v) > 0:
                        return v
                print(f"[WARN] 等待视频文件超时: {v}")

    print(f"[WARN] 视频字段均未命中本地文件/合法链接: path={path!r}, url={url!r}, file={file_id!r}")

    if message_id:
        try:
            msg_result = await call_action(ws, "get_msg", {"message_id": message_id})
            msg_video = None
            if isinstance(msg_result, dict):
                raw = msg_result.get("message") or msg_result.get("raw_message") or []
                if isinstance(raw, list):
                    for seg in raw:
                        if seg.get("type") == "video":
                            msg_video = seg.get("data", {})
                            break
                elif isinstance(raw, str):
                    m = re.search(r'\[CQ:video,url=([^,\]]+)', raw)
                    if m:
                        msg_video = {"url": m.group(1)}
            if msg_video:
                new_url = msg_video.get("url") or ""
                if _is_http_url(new_url):
                    tmp = await _download_to_temp(new_url)
                    if tmp:
                        return tmp
        except Exception as e:
            print(f"[WARN] get_msg 刷新失败: {e}")

    for candidate in (file_id, path):
        if not candidate:
            continue
        if message_type == "group" and group_id:
            api = "get_group_file_url"
            params = {"file_id": candidate, "group": str(group_id)}
        else:
            api = "get_private_file_url"
            params = {"file_id": candidate}
        try:
            result = await call_action(ws, api, params)
            if isinstance(result, dict):
                direct = result.get("url") or result.get("file") or result.get("fileUrl")
                if _is_http_url(direct):
                    tmp = await _download_to_temp(direct)
                    if tmp:
                        return tmp
                elif direct and os.path.exists(direct):
                    return direct
        except Exception as e:
            print(f"[WARN] {api} 失败: {e}")

    for candidate in (file_id, path, url):
        if not candidate:
            continue
        try:
            result = await call_action(ws, "get_file", {"file": candidate})
            if isinstance(result, dict):
                local = result.get("file") or result.get("path")
                if local and not _is_http_url(local) and os.path.exists(local):
                    return local
                new_url = result.get("url")
                if new_url and not _is_http_url(new_url) and os.path.exists(new_url):
                    return new_url
                if _is_http_url(new_url):
                    tmp = await _download_to_temp(new_url)
                    if tmp:
                        return tmp
                b64 = result.get("base64") or result.get("data")
                if isinstance(b64, str) and b64:
                    try:
                        raw = base64.b64decode(b64)
                        if raw:
                            tmp_dir = getattr(config, "VIDEO_TMP_DIR", "") or tempfile.gettempdir()
                            os.makedirs(tmp_dir, exist_ok=True)
                            fd, tmp_path = tempfile.mkstemp(dir=tmp_dir, suffix=".mp4")
                            with os.fdopen(fd, "wb") as f:
                                f.write(raw)
                            return tmp_path
                    except Exception as e:
                        print(f"[WARN] base64 解码失败: {e}")
        except Exception as e:
            print(f"[WARN] get_file 获取失败: {e}")

    raise RuntimeError("无法获取视频文件")


async def get_quoted_message(ws, reply_id) -> dict:
    """用 get_msg 获取被引用消息的完整内容。"""
    if not reply_id:
        return {}
    try:
        result = await call_action(ws, "get_msg", {"message_id": reply_id})
        if not isinstance(result, dict):
            return {}
        quoted = {"text": "", "image_refs": [], "user_id": "", "time": result.get("time", "")}
        sender = result.get("sender", {}) or {}
        quoted["user_id"] = sender.get("nickname") or sender.get("user_id") or ""
        raw = result.get("message")
        if isinstance(raw, list):
            for seg in raw:
                t = seg.get("type")
                data = seg.get("data", {})
                if t == "text":
                    quoted["text"] += data.get("text", "")
                elif t == "image":
                    url = data.get("url") or data.get("file")
                    if url:
                        quoted["image_refs"].append({"url": url})
                elif t == "record":
                    quoted["text"] += " [语音消息]"
                elif t == "video":
                    quoted["text"] += " [视频消息]"
                elif t == "face":
                    quoted["text"] += " [表情]"
        elif isinstance(raw, str):
            quoted["text"] = re.sub(r"\[CQ:[^\]]+\]", "", raw).strip()
            for m in re.finditer(r"\[CQ:image,url=([^,\]]+)", raw):
                quoted["image_refs"].append({"url": m.group(1)})
        return quoted
    except Exception as e:
        print(f"[WARN] 获取引用消息失败: {e}")
        return {}


async def decode_voice_wav(record_data: dict) -> bytes:
    """把 QQ 语音（SILK V3，文件名却叫 .amr）解码成 wav；其它格式直接透传。"""
    local_path = record_data.get("path") or record_data.get("url") or record_data.get("file")
    audio_bytes = None
    if local_path:
        if local_path.startswith(("http://", "https://")):
            try:
                async with httpx.AsyncClient(timeout=60, follow_redirects=True) as c:
                    resp = await c.get(local_path)
                    if resp.status_code == 200 and len(resp.content) > 0:
                        audio_bytes = resp.content
            except Exception as e:
                print(f"[WARN] 语音 url 下载失败: {e}")
        else:
            try:
                with open(local_path, "rb") as f:
                    audio_bytes = f.read()
            except OSError as e:
                print(f"[WARN] 语音本地文件读取失败: {e}")
    if audio_bytes is None:
        print("[WARN] 无法获取语音文件")
        return b""

    header = audio_bytes[:10]
    if b"SILK" in header:
        tmp_wav = tempfile.mktemp(suffix=".wav")
        try:
            if voice_client.silk_to_wav(audio_bytes, tmp_wav):
                with open(tmp_wav, "rb") as f:
                    return f.read()
            return b""
        finally:
            if os.path.exists(tmp_wav):
                os.remove(tmp_wav)
    return audio_bytes


# ============================================================================
# QQ 平台插件
# ============================================================================
class QQPlugin(PlatformPlugin):
    """QQ（NapCat / OneBot v11 反向 WebSocket）平台插件。"""

    name = "qq"
    platform = "qq"
    capabilities = {"group": True, "voice": True, "image": True, "voice_input": True, "video_input": True}

    def __init__(self, core):
        super().__init__(core)
        self.ws = None
        self._server = None
        self.adapter = None      # MessageSender（主动发送用）
        self._tasks = set()

    async def start(self):
        from qq_adapter import get_qq_adapter
        from message_bus import set_sender, get_event_bus

        self.adapter = get_qq_adapter()
        set_sender(self.adapter)

        # 订阅"AI 请求修改核心代码"事件，私聊通知用户确认
        def _on_edit_request(data: dict):
            req_id = data.get("request_id", "?")
            filepath = data.get("file", "?")
            instruction = data.get("instruction", "?")
            priv_target = getattr(config, "PROACTIVE_PRIVATE_USER_ID", "")
            if priv_target:
                msg = (f"[代码修改确认] AI 想修改核心文件：\n{filepath}\n"
                       f"要求：{instruction}\n"
                       f"回复 /同意修改 {req_id} 批准，或 /拒绝修改 {req_id} 拒绝。")
                asyncio.get_event_loop().create_task(self.adapter.send_private(priv_target, msg))
        try:
            get_event_bus().on("code.edit_request", _on_edit_request)
        except Exception as e:
            degrade("libs/qq_bot_runtime/qq_plugin.py:560 QQPlugin.start", e, "降级：get_event_bus().on('code.edit_request', _on_edit_r")

        # NapCat 反向 WebSocket 服务端：等 NapCat 主动连进来
        host = config.WS_HOST
        port = config.WS_PORT
        path = config.WS_PATH
        self._server = await websockets.serve(self._handler, host, port, max_size=16 * 1024 * 1024)
        print(f"[QQ-PLUGIN] WebSocket 服务已启动: ws://{host}:{port}{path}（等待 NapCat 连接）")
        wl = set(str(g) for g in getattr(config, "QQ_GROUP_WHITELIST", []) or [])
        if wl:
            print(f"[QQ-PLUGIN] 群监听白名单已启用（仅处理这些群）: {', '.join(sorted(wl))}")
        else:
            print("[QQ-PLUGIN] 群监听白名单未启用：所有群均处理（QQ_GROUP_WHITELIST 为空）")
        await super().start()

    async def stop(self):
        if self._server:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception as e:
                degrade("libs/qq_bot_runtime/qq_plugin.py:576 QQPlugin.stop", e, "降级：await self._server.wait_closed()")
            self._server = None
        self.ws = None
        plugin_pending_calls.clear()
        if self.adapter:
            self.adapter.set_ws(None)
        for t in list(self._tasks):
            t.cancel()
        self._tasks.clear()
        await super().stop()

    async def _handler(self, ws):
        """单个 WebSocket 连接：读事件 -> 组装 InboundMessage -> 交核心处理。"""
        print("[QQ-PLUGIN] NapCat 已连接")
        self.ws = ws
        self.adapter.set_ws(ws)

        # 视觉模块的异常提醒改走统一 sender（不再依赖裸 ws）
        try:
            from vision_capture import vision_capture
            vision_capture.ws = ws
        except Exception as e:
            degrade("libs/qq_bot_runtime/qq_plugin.py:597 QQPlugin._handler", e, "降级：from vision_capture import vision_capture")

        pending = {}      # 私聊聚合缓冲
        tasks = set()
        try:
            async for raw in ws:
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError as e:
                    degrade("libs/qq_bot_runtime/qq_plugin.py:606 QQPlugin._handler", e, "降级：data = json.loads(raw)")
                    continue

                # API 调用响应（echo 匹配）
                echo = data.get("echo")
                if echo and echo in plugin_pending_calls:
                    future = plugin_pending_calls[echo]
                    if not future.done():
                        future.set_result(data.get("data"))
                    continue

                # 消息事件 -> 组装并分发
                if data.get("post_type") == "message":
                    if data.get("message_type") == "group":
                        gid = str(data.get("group_id") or "")
                        wl = set(str(g) for g in getattr(config, "QQ_GROUP_WHITELIST", []) or [])
                        if wl and gid not in wl:
                            continue  # 群不在监听白名单内：忽略，不组装/不分发/不回复
                        task = asyncio.create_task(self._process(data, aggregate=False, pending=pending))
                    elif data.get("message_type") == "private":
                        task = asyncio.create_task(self._process(data, aggregate=True, pending=pending))
                    else:
                        continue
                    tasks.add(task)
                    task.add_done_callback(tasks.discard)
        except websockets.ConnectionClosed:
            print("[QQ-PLUGIN] NapCat 连接断开")
        finally:
            self.ws = None

    async def _process(self, data: dict, aggregate: bool, pending: dict):
        """私聊聚合 + 消息组装 + 调核心。"""
        user_id = str(data.get("user_id", ""))

        if aggregate and config.AGGREGATE_PRIVATE_MESSAGES:
            text = extract_text(data.get("message", []))
            has_image = bool(extract_images(data.get("message", [])))
            # 图片/命令消息立即处理，不参与聚合
            if has_image or text.strip().startswith(("/", "搜索", "思考")):
                await self._dispatch(data)
                return
            # 取消该用户之前的计时器
            if user_id in pending and pending[user_id].get("task"):
                pending[user_id]["task"].cancel()
            if user_id in pending:
                old_text = extract_text(pending[user_id]["data"].get("message", []))
                merged_text = (old_text.rstrip() + "\n" + text.strip()).strip()
                merged_data = dict(pending[user_id]["data"])
                merged_data["message"] = [{"type": "text", "data": {"text": merged_text}}]
            else:
                merged_data = data
            pending[user_id] = {"data": merged_data, "task": None}

            async def flush():
                await asyncio.sleep(config.AGGREGATE_WAIT_SECONDS)
                if user_id in pending:
                    final_data = pending.pop(user_id)["data"]
                    await self._dispatch(final_data)

            task = asyncio.create_task(flush())
            pending[user_id]["task"] = task
        else:
            await self._dispatch(data)

    async def _dispatch(self, data: dict):
        """OneBot 事件 -> InboundMessage -> 核心聊天大脑。"""
        try:
            msg = await self.build_inbound(data)
            reply = QQReplyTarget(self, msg)
            await self.core.chat.handle_message(msg, reply)
        except Exception as e:
            print(f"[QQ-PLUGIN] 消息处理失败: {e}")

    async def build_inbound(self, data: dict) -> InboundMessage:
        """把 OneBot 消息事件转换成平台无关的 InboundMessage。"""
        message = data.get("message", [])
        msg = InboundMessage(
            platform=self.platform,
            channel_type=data.get("message_type", "private"),
            channel_id=str(data.get("group_id") or data.get("user_id") or ""),
            user_id=str(data.get("user_id", "")),
            message_id=str(data.get("message_id", "")),
            text=extract_text(message),
            mentioned=is_mentioned(data),
            raw=data,
        )

        # 图片引用（按需由 ReplyTarget.fetch_image 下载）
        msg.image_refs = extract_images(message)

        # 语音：QQ 语音是 SILK V3，属于 QQ 特有格式 -> 在平台层解码成 wav
        for seg in message:
            if seg.get("type") == "record":
                msg.audio_wav = await decode_voice_wav(seg.get("data", {}))
                break

        # 视频
        for seg in message:
            if seg.get("type") == "video":
                msg.has_video = True
                msg.video_ref = {"data": seg.get("data", {}), "message_id": msg.message_id}
                break

        # 引用消息（需要调 OneBot API，属平台职责）
        reply_info = extract_reply(message)
        if reply_info.get("id"):
            quoted = await get_quoted_message(self.ws, reply_info["id"])
            if quoted:
                msg.quoted_text = quoted.get("text", "")
                msg.quoted_image_refs = quoted.get("image_refs", [])
                msg.quoted_sender = quoted.get("user_id", "")
                msg.quoted_self = (str(quoted.get("user_id", "")) == str(data.get("self_id", "")))
        return msg
