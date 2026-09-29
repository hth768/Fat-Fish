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
import base64
import hashlib
import json
import os
import re
import tempfile
import time

import httpx
import websockets

import config
import qq_guard
import reminder
import routine
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

# 系统表情标记：[表情22：白眼] / [表情22:白眼]（数字为 QQ face id，冒号全角半角均可）
_SYS_FACE_RE = re.compile(r'^表情\s*(\d{1,3})\s*[：:]\s*(.*)$')


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
        # 系统表情标记「[表情22：白眼]」：数字为 QQ face id，支持全角/半角冒号与可选空格
        # （对齐新版 0.1.8 修复：早期只认半角冒号，bot 照着入站的全角写法输出会变成"不存在的表情"文本）
        _sysface = _SYS_FACE_RE.match(inner)
        if _sysface:
            segments.append({"type": "face", "data": {"id": str(int(_sysface.group(1)))}})
        elif inner.startswith("表情包:"):
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


# 主动回看意图关键词（对齐新版 qq_view_image 主动回看工具）：用户明确要求重新看清/识别图片。
_VISION_RECALL_KEYWORDS = (
    "重看图片", "再看图", "重新看图", "重新看", "再看这张", "再看看这张", "再看下这张",
    "看清楚", "看清点", "认清楚", "认清", "重新识别", "重新描述", "仔细看这张",
    "图里是什么", "图里写了什么", "你看这张", "再看看图", "重新看看", "再看一眼",
)


def vision_recall_intent(text: str) -> bool:
    """判断用户文本是否含主动回看图片的意图（用于触发 VLM 主动回看）。"""
    if not text:
        return False
    return any(k in text for k in _VISION_RECALL_KEYWORDS)


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
async def _to_qq_voice_file(wav_path: str) -> str:
    """把本地 TTS 的 wav 转成 QQ 客户端更易播放的 mp3；ffmpeg 不可用/失败则原样返回 wav。"""
    ffmpeg = getattr(config, "FFMPEG_PATH", "") or "ffmpeg"
    if not wav_path.lower().endswith(".wav") or not os.path.exists(wav_path):
        return wav_path
    mp3 = wav_path[:-4] + ".mp3"
    try:
        proc = await asyncio.create_subprocess_exec(
            ffmpeg, "-y", "-i", wav_path, "-b:a", "128k", mp3,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await asyncio.wait_for(proc.communicate(), timeout=60)
        if proc.returncode == 0 and os.path.exists(mp3):
            return mp3
        print(f"[WARN] 语音转 mp3 失败(rc={proc.returncode})，回退 wav")
    except Exception as e:
        print(f"[WARN] 语音转 mp3 异常，回退 wav: {e}")
    return wav_path


class QQReplyTarget(ReplyTarget):
    """一次 QQ 会话的回复上下文。"""

    capabilities = {"group": True, "voice": True, "image": True, "voice_input": True, "video_input": True}

    def __init__(self, plugin: "QQPlugin", msg: InboundMessage):
        super().__init__(msg)
        self.plugin = plugin

    def _high_risk(self, text: str) -> bool:
        """判断是否为高风险回复（需确认）：群发，或含 @all/@全体成员。"""
        if self.msg.channel_type == "group":
            return True
        if "@all" in text or "@全体成员" in text:
            return True
        return False

    async def _raw_send(self, text: str):
        """实际发送（拆分文字/表情/图片为多条消息逐条发送）。"""
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

    def _guard_ok(self) -> bool:
        """发言护栏（对齐新版 groupSpeak / antiLoop）：超限则本次不发言，避免刷屏与话题死循环。"""
        ok, why = qq_guard.allow_outgoing(self.msg.channel_type, self.msg.channel_id)
        if not ok:
            print(f"[QQ-GUARD] 本次发言被拦截（{self.msg.channel_type}/{self.msg.channel_id}）：{why}")
        return ok

    async def reply(self, text: str):
        """回复文本。若开启 QQ_SEND_CONFIRM 且为高风险回复，先发草稿、确认后再正式发。"""
        # 群聊限速 / 防死循环：发送前统一过护栏
        if not self._guard_ok():
            return
        # 可选发送确认（对齐新版 qq_draft/qq_confirm）：高风险回复先发草稿，确认后再正式发
        if getattr(config, "QQ_SEND_CONFIRM", False) and self._high_risk(text):
            # 存 (文本, 时间戳)，供 _dispatch 在过期时作废（对齐新版 onTurnEnded）
            self.plugin._confirm_pending[(self.msg.channel_type, self.msg.channel_id)] = (text, time.time())
            await self._raw_send(f"[草稿·确认后发送]\n{text}")
            qq_guard.note_outgoing(self.msg.channel_type, self.msg.channel_id)
            return
        await self._raw_send(text)
        qq_guard.note_outgoing(self.msg.channel_type, self.msg.channel_id)

    async def reply_voice(self, wav_path: str):
        """发送语音消息（QQ record 段）。本地 TTS 产出 wav，转 mp3 提升 QQ 客户端兼容性（失败回退 wav）。"""
        if not self._guard_ok():
            return
        ws = self.plugin.ws
        if not ws:
            return
        qq_guard.note_outgoing(self.msg.channel_type, self.msg.channel_id)
        voice_file = await _to_qq_voice_file(wav_path)
        message_type = self.msg.channel_type
        target = self.msg.channel_id
        payload = {
            "action": "send_msg",
            "params": {
                "message_type": message_type,
                "message": [{"type": "record", "data": {"file": voice_file}}],
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


def _system_proxy() -> str | None:
    """读取系统（Windows 注册表）代理地址，供 httpx 下载 QQ 媒体（图片/语音）CDN 使用。

    httpx 默认只认 HTTP_PROXY/HTTPS_PROXY 环境变量，不读 Windows 注册表
    Internet Settings 代理；而本机 QQ 经系统代理（如 127.0.0.1:9098）访问外网，
    故需显式取出传给 httpx，否则直连 QQ 图片 CDN 会 All connection attempts failed。
    非 Windows / 未启用代理时返回 None（httpx 直连）。
    """
    try:
        import sys
        if sys.platform != "win32":
            return None
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r"Software\Microsoft\Windows\CurrentVersion\Internet Settings")
        enabled, _ = winreg.QueryValueEx(key, "ProxyEnable")
        if not enabled:
            return None
        server, _ = winreg.QueryValueEx(key, "ProxyServer")
        if not server:
            return None
        server = server.strip()
        # 注册表可能是 "http=host:port;https=host:port" 或纯 "host:port"
        if "=" in server:
            parts = dict(p.split("=", 1) for p in server.split(";") if "=" in p)
            host = parts.get("https") or parts.get("http") or next(iter(parts.values()))
        else:
            host = server
        if not host:
            return None
        if not host.startswith("http://") and not host.startswith("https://"):
            host = "http://" + host
        return host
    except Exception as e:
        print(f"[WARN] 读取系统代理失败，改用直连: {e}")
        return None


async def get_image_bytes(ws, img_data: dict) -> bytes:
    """获取图片二进制：先直接下载 url，失败用 get_image API 刷新后重试。

    动图 / 大图经 get_image 缓存到本地可能尚未就绪（返回空文件或路径暂不存在），
    因此本地读取失败时重试几次并短等待，避免「无法获取图片」。
    """
    file = img_data.get("file", "")
    url = img_data.get("url", "")
    # 系统代理（Windows 注册表）：QQ 图片/语音 CDN 须经系统代理才能连通；
    # httpx 默认不读 Windows 注册表代理，仅认 HTTP_PROXY 环境变量，故显式传入。
    _proxy = _system_proxy()
    if url and isinstance(url, str) and url.startswith(("http://", "https://")):
        try:
            async with httpx.AsyncClient(timeout=60, follow_redirects=True, proxy=_proxy) as c:
                resp = await c.get(url)
                if resp.status_code == 200 and len(resp.content) > 0:
                    return resp.content
                print(f"[WARN] URL 下载失败，状态码 {resp.status_code}")
        except Exception as e:
            print(f"[WARN] URL 下载异常: {e}")
    if file:
        # 重试若干次：动图/大图可能尚未缓存到本地
        for attempt in range(4):
            try:
                result = await call_action(ws, "get_image", {"file": file})
            except Exception as e:
                print(f"[WARN] get_image 获取失败: {e}")
                result = None
            if isinstance(result, dict):
                try:
                    print(f"[DIAG] get_image 返回: keys={list(result.keys())} file={str(result.get('file'))[:160]!r} url={str(result.get('url'))[:160]!r}")
                except Exception:
                    pass
                local_path = result.get("file")
                if local_path and isinstance(local_path, str) and not local_path.startswith(("http://", "https://")):
                    try:
                        with open(local_path, "rb") as f:
                            content = f.read()
                            if len(content) > 0:
                                return content
                            print(f"[WARN] get_image 本地文件为空（尝试 {attempt+1}）")
                    except OSError as e:
                        print(f"[WARN] 本地文件读取失败（尝试 {attempt+1}）: {e}")
                new_url = result.get("url")
                if new_url and isinstance(new_url, str) and new_url.startswith(("http://", "https://")):
                    try:
                        async with httpx.AsyncClient(timeout=60, follow_redirects=True, proxy=_proxy) as c:
                            resp = await c.get(new_url)
                            if resp.status_code == 200 and len(resp.content) > 0:
                                return resp.content
                    except Exception as e:
                        print(f"[WARN] get_image url 下载异常: {e}")
            if attempt < 3:
                await asyncio.sleep(1.2)
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
    """用 get_msg 获取被引用消息的完整内容。

    返回包含引用消息的会话信息（channel_type/channel_id/sender_id），
    供 build_inbound 做 sameConversation 断言，防止跨会话引用串入上下文。
    """
    if not reply_id:
        return {}
    try:
        result = await call_action(ws, "get_msg", {"message_id": reply_id})
        if not isinstance(result, dict):
            return {}
        quoted = {"text": "", "image_refs": [], "user_id": "", "time": result.get("time", ""),
                  "channel_type": "", "channel_id": "", "sender_id": ""}
        sender = result.get("sender", {}) or {}
        quoted["user_id"] = sender.get("nickname") or sender.get("user_id") or ""
        quoted["sender_id"] = str(result.get("user_id") or sender.get("user_id") or "")
        # 引用消息的会话：group 用 group_id，private 用 user_id
        mtype = result.get("message_type") or result.get("message_type_")
        if mtype == "group" or result.get("group_id"):
            quoted["channel_type"] = "group"
            quoted["channel_id"] = str(result.get("group_id") or "")
        elif mtype == "private" or result.get("user_id"):
            quoted["channel_type"] = "private"
            quoted["channel_id"] = str(result.get("user_id") or "")
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
                async with httpx.AsyncClient(timeout=60, follow_redirects=True, proxy=_system_proxy()) as c:
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
        # 消息追踪（对齐新版 known_messages / msgChain）：
        self._known_messages = {}     # message_id -> 处理时间戳（echo/重投去重）
        self._conv_chains = {}        # (channel_type, channel_id) -> 串行任务链（避免并发抢同一会话）
        self._vision_seen = {}        # 图片 sha256 -> 最后描述时间（视觉去重）
        self._confirm_pending = {}     # (channel_type, channel_id) -> (草稿文本, 时间戳)（QQ_SEND_CONFIRM 待确认）
        # 状态落盘（对齐新版 saveState）：把可序列化去重字典持久化到 dataDir json，进程重启后仍有效
        self._state_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "data", "qq_plugin_state.json")
        self._state_timer = None
        self._state_save_delay = 1.5  # debounce 秒数
        self._send_confirm_ttl = float(getattr(config, "QQ_SEND_CONFIRM_TTL", 180))

    def _need_native_asr(self) -> bool:
        """是否该在平台层做原生识别（对齐新版 voice 的 native 供应商）。

        只在「云端 ASR 用不了」时走这条路：GLM Key 没配（纯净包默认）、且语音的 asr 开关开着。
        有 Key 时优先让 chat_service 走云端 GLM ASR（识别质量更好）。
        """
        try:
            import voice_client as _vca
            if not _vca.voice_asr_enabled():
                return False
        except Exception:
            pass
        return not bool(getattr(config, "GLM_API_KEY", ""))

    async def _native_asr(self, record_data: dict) -> str:
        """用 NapCat 原生 translate_record 把语音转成文字（零第三方依赖，由服务端解码 SILK）。"""
        file_field = (record_data.get("path") or record_data.get("url")
                      or record_data.get("file") or "")
        if not file_field or not self.ws:
            return ""
        try:
            result = await call_action(self.ws, "translate_record",
                                       {"file": file_field, "file_id": file_field})
        except Exception as e:
            print(f"[QQ-ASR] 原生识别调用失败: {e}")
            return ""
        try:
            if isinstance(result, dict):
                data = result.get("data") if isinstance(result.get("data"), dict) else {}
                text = data.get("text") or result.get("text") or ""
                return str(text).strip()
        except Exception as e:
            print(f"[QQ-ASR] 原生识别结果解析失败: {e}")
        return ""

    async def start(self):
        from qq_adapter import get_qq_adapter
        from message_bus import set_sender, get_event_bus

        self.adapter = get_qq_adapter()
        set_sender(self.adapter)

        # 状态落盘：从 dataDir json 恢复去重字典（known_messages / vision_seen）
        if getattr(config, "QQ_STATE_PERSIST", True):
            self._load_state()

        # 订阅"AI 请求修改核心代码"事件，私聊通知用户确认
        def _on_edit_request(data: dict):
            req_id = data.get("request_id", "?")
            filepath = data.get("file", "?")
            instruction = data.get("instruction", "?")
            priv_target = getattr(config, "PROACTIVE_PRIVATE_USER_ID", "")
            if priv_target:
                msg = (f"[代码修改确认] AI 想修改核心文件：\n{filepath}\n"
                       f"要求：{instruction}\n"
                       f"回复 /同意issue {req_id} 批准，或 /拒绝issue {req_id} 拒绝。")
                asyncio.get_event_loop().create_task(self.adapter.send_private(priv_target, msg))
        try:
            get_event_bus().on("code.edit_request", _on_edit_request)
        except Exception as e:
            degrade("libs/qq_bot_runtime/qq_plugin.py:560 QQPlugin.start", e, "降级：get_event_bus().on('code.edit_request', _on_edit_r")

        # QQ 连接模式（对齐新版 cortico-world-qq 双向能力）
        mode = str(getattr(config, "QQ_WS_MODE", "reverse")).lower()
        if mode == "forward":
            # 正向 WS 客户端：本机连到 NapCat 暴露的 WebSocket 地址，并自动重连
            self._reconnect_task = asyncio.ensure_future(self._forward_loop())
            wl = set(str(g) for g in getattr(config, "QQ_GROUP_WHITELIST", []) or [])
            print(f"[QQ-PLUGIN] 正向 WS 客户端模式（连 {getattr(config, 'QQ_WS_URL', 'ws://127.0.0.1:3001')}），等待连接…")
            if wl:
                print(f"[QQ-PLUGIN] 群监听白名单已启用（仅处理这些群）: {', '.join(sorted(wl))}")
        else:
            # 反向 WS 服务端（默认）：等 NapCat 主动连进来
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

        # 后台任务（对齐新版 reminder / routine）：到点提醒扫描 + 作息播报检查
        try:
            reminder.start_scan()
        except Exception as e:
            print(f"[WARN] 到点提醒扫描启动失败（忽略）: {e}")
        try:
            routine.start_check()
        except Exception as e:
            print(f"[WARN] 作息播报检查启动失败（忽略）: {e}")
        await super().start()

    async def stop(self):
        if self._server:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception as e:
                degrade("libs/qq_bot_runtime/qq_plugin.py:576 QQPlugin.stop", e, "降级：await self._server.wait_closed()")
            self._server = None
        # 状态落盘：退出前冲刷一次（对齐新版 saveState 在回合/进程结束时持久化）
        if getattr(config, "QQ_STATE_PERSIST", True):
            try:
                self._save_state_now()
            except Exception as e:
                print(f"[WARN] 状态落盘退出冲刷失败（忽略）: {e}")
        self.ws = None
        plugin_pending_calls.clear()
        if self.adapter:
            self.adapter.set_ws(None)
        for t in list(self._tasks):
            t.cancel()
        self._tasks.clear()
        await super().stop()

    # ------------------------------------------------------------------
    # 正向 WS 客户端 + 断线自动重连（对齐新版双向模式）
    # ------------------------------------------------------------------
    async def _forward_loop(self):
        """正向模式：作为客户端连到 NapCat 的 WS，断线按 1s→30s 退避重连。"""
        delay = int(getattr(config, "QQ_WS_RECONNECT_MIN", 1))
        url = getattr(config, "QQ_WS_URL", "ws://127.0.0.1:3001")
        while True:
            try:
                async with websockets.connect(url, max_size=16 * 1024 * 1024) as ws:
                    self.ws = ws
                    self.adapter.set_ws(ws)
                    print(f"[QQ-PLUGIN] 正向 WS 已连接：{url}")
                    delay = int(getattr(config, "QQ_WS_RECONNECT_MIN", 1))
                    try:
                        await self._handler(ws)
                    finally:
                        self.ws = None
                        self.adapter.set_ws(None)
            except Exception as e:
                print(f"[QQ-PLUGIN] 正向 WS 连接失败：{e}")
            if not getattr(config, "QQ_WS_RECONNECT", True):
                break
            await asyncio.sleep(delay)
            delay = min(delay * 2, int(getattr(config, "QQ_WS_RECONNECT_MAX", 30)))

    # ------------------------------------------------------------------
    # 消息追踪（known_messages / 会话串行链）—— 对齐新版
    # ------------------------------------------------------------------
    def _mark_seen(self, mid) -> bool:
        """记录已处理的 message_id；若近期已处理过（echo/重投）返回 True（应跳过）。"""
        now = time.time()
        ttl = max(60, int(getattr(config, "QQ_KNOWN_MESSAGES_TTL", 600)))
        if len(self._known_messages) > 2000:
            self._known_messages = {k: v for k, v in self._known_messages.items() if now - v < ttl}
        if mid in self._known_messages:
            return True
        self._known_messages[mid] = now
        self._schedule_save_state()  # 去重状态落盘
        return False

    # ------------------------------------------------------------------
    # 状态落盘（对齐新版 saveState）：把可序列化去重字典持久化到 dataDir json
    # （_conv_chains 含 asyncio future，不可序列化，仅做内存态）
    # ------------------------------------------------------------------
    def _load_state(self):
        """启动/重载时从 dataDir json 恢复去重字典（带 TTL 过滤）。"""
        try:
            import json as _json
            if not os.path.exists(self._state_path):
                return
            with open(self._state_path, "r", encoding="utf-8") as f:
                st = _json.load(f)
            now = time.time()
            km_ttl = max(60, int(getattr(config, "QQ_KNOWN_MESSAGES_TTL", 600)))
            vs_ttl = max(0, int(getattr(config, "QQ_VISION_DEDUP_SEC", 300)))
            km = st.get("known_messages", {})
            self._known_messages = {k: float(v) for k, v in km.items()
                                    if now - float(v) < km_ttl}
            vs = st.get("vision_seen", {})
            self._vision_seen = {k: float(v) for k, v in vs.items()
                                 if now - float(v) < vs_ttl}
            print(f"[INFO] 状态落盘已恢复：known_messages={len(self._known_messages)}，"
                  f"vision_seen={len(self._vision_seen)}")
        except Exception as e:
            print(f"[WARN] 状态落盘加载失败（忽略）: {e}")

    def _schedule_save_state(self):
        """debounce 写盘：1.5s 内的多次更新只落一次（对齐新版 saveState 防抖）。"""
        if getattr(config, "QQ_STATE_PERSIST", True) is False:
            return
        if self._state_timer is not None:
            self._state_timer.cancel()
        try:
            loop = asyncio.get_event_loop()
            self._state_timer = loop.call_later(self._state_save_delay, self._save_state_now)
        except Exception:
            # 无事件循环（如同步上下文）时退化为直接写
            try:
                self._save_state_now()
            except Exception:
                pass

    def _save_state_now(self):
        """立即把去重字典写盘（供 debounce 与 stop 调用）。"""
        self._state_timer = None
        try:
            import json as _json
            d = os.path.dirname(self._state_path)
            if d:
                os.makedirs(d, exist_ok=True)
            st = {
                "known_messages": self._known_messages,
                "vision_seen": self._vision_seen,
            }
            tmp = self._state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                _json.dump(st, f, ensure_ascii=False)
            os.replace(tmp, self._state_path)
        except Exception as e:
            print(f"[WARN] 状态落盘写入失败（忽略）: {e}")

    # ------------------------------------------------------------------
    # 视觉 VLM 路由（对齐新版 qq_view_image）：收图异步描述 + sha256 去重
    # ------------------------------------------------------------------
    async def _fetch_image_b64(self, seg):
        """下载图片字节 -> (base64, sha256)。失败返回 (None, None)。"""
        try:
            data = await self._fetch_image(seg)
            if not data:
                return None, None
            return base64.b64encode(data).decode("ascii"), hashlib.sha256(data).hexdigest()
        except Exception:
            return None, None

    async def _vision_describe(self, seg, force: bool = False) -> str:
        """对单张图片做视觉描述（需视觉模型）；失败/去重命中返回空串。

        force=True 时绕过去重窗口（用于主动回看，重新识别同一张图）。
        """
        data_b64, sha = await self._fetch_image_b64(seg)
        if not data_b64:
            return ""
        ttl = max(0, int(getattr(config, "QQ_VISION_DEDUP_SEC", 300)))
        now = time.time()
        if not force and sha in self._vision_seen and now - self._vision_seen[sha] < ttl:
            return ""
        self._vision_seen[sha] = now
        self._schedule_save_state()  # 视觉去重状态落盘
        try:
            vision = getattr(self.core, "chat", None)
            if vision is None or not hasattr(vision, "llm"):
                return ""
            resp = await vision.llm.chat(
                [{"role": "user", "content": [
                    {"type": "image", "data": f"data:image;base64,{data_b64}"},
                    {"type": "text", "text": "用一句话描述这张图片的主要内容，中文。"},
                ]}],
                capability="vision")
            resp = (resp or "").strip()
            if resp:
                self._vision_account(sha, len(resp))
            return resp
        except Exception as e:
            print(f"[WARN] 图片视觉描述失败（降级跳过）: {e}")
            return ""

    def _vision_account(self, sha: str, chars: int):
        """视觉用量记账（对齐新版 vision-accounting.jsonl）：每次实际视觉调用追加一行。"""
        try:
            if not getattr(config, "QQ_VISION_ACCOUNTING", True):
                return
            log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
            os.makedirs(log_dir, exist_ok=True)
            log_path = os.path.join(log_dir, "vision-accounting.jsonl")
            rec = {"ts": round(time.time(), 3), "sha256": sha[:16], "chars": chars}
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception as e:
            print(f"[WARN] 视觉记账失败（忽略）: {e}")

    async def get_forward_msg(self, fid):
        """通过 OneBot get_forward_msg 拉取合并转发的全部节点（对齐新版 forwardIdentityCollapsed）。

        返回节点列表：[{sender:{user_id,nickname}, message:[...], time, ...}]，失败返回 None。
        """
        if not self.ws:
            return None
        try:
            res = await self.call_action(self.ws, "get_forward_msg", {"id": str(fid)})
        except Exception as e:
            print(f"[WARN] get_forward_msg 调用失败: {e}")
            return None
        if not isinstance(res, dict) or res.get("status") != "ok":
            print(f"[WARN] get_forward_msg 返回非 ok: {res}")
            return None
        data = res.get("data") or {}
        return data.get("messages") or []

    async def _expand_forward(self, message, self_id=None) -> str:
        """合并转发展开（对齐新版 forwardIdentityCollapsed / recoverForwardSelfNodes）：

        识别 forward 段、拉取节点并展开为「昵称：文本」列表，作为引用上下文。
        QQ 合并转发中，机器人自身的节点可能被折叠为转发者身份，这里做 recoverForwardSelfNodes：
        命中自身 user_id 的节点显式标注为「我(肥鱼娘)」，避免自身发言缺失/错归他人。
        """
        lines = []
        limit = int(getattr(config, "QQ_FORWARD_EXPAND_LIMIT", 40))
        for seg in message:
            if seg.get("type") != "forward":
                continue
            fid = seg.get("data", {}).get("id")
            if not fid:
                continue
            try:
                nodes = await self.get_forward_msg(fid)
            except Exception as e:
                print(f"[WARN] 拉取合并转发失败: {e}")
                continue
            if not nodes:
                continue
            for n in nodes[:limit]:
                sender = n.get("sender") or {}
                node_uid = str(n.get("user_id") or sender.get("user_id") or "")
                # recoverForwardSelfNodes：自身节点补回（QQ 合并转发中自身消息可能折叠）
                if self_id and node_uid == str(self_id):
                    u = sender.get("nickname") or "我(肥鱼娘)"
                elif sender.get("nickname"):
                    u = sender.get("nickname")
                elif node_uid:
                    u = node_uid
                else:
                    u = "某人"
                txt = extract_text(n.get("message", []))
                if txt:
                    lines.append(f"{u}：{txt}")
        return "\n".join(lines)

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
                    # known_messages：已处理过的 message_id（echo 回显/重投）直接跳过
                    mid = data.get("message_id")
                    if mid is not None and self._mark_seen(mid):
                        continue
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
        # 发送确认（对齐新版 qq_draft/qq_confirm）：本会话有待确认草稿且用户回确认词 -> 正式发送
        if getattr(config, "QQ_SEND_CONFIRM", False):
            text0 = extract_text(data.get("message", []))
            conv = (data.get("message_type", "private"),
                    str(data.get("group_id") or data.get("user_id") or ""))
            pend = self._confirm_pending.pop(conv, None)
            if pend is not None:
                # onTurnEnded：草稿过期自动作废，避免旧草稿被后续任意消息误触发确认
                pend_text, pend_ts = pend if isinstance(pend, tuple) else (pend, 0)
                age = time.time() - float(pend_ts)
                if age > self._send_confirm_ttl:
                    print(f"[INFO] 待确认草稿已过期（{age:.0f}s > {self._send_confirm_ttl:.0f}s），作废")
                elif text0.strip() in ("确认", "发送", "好的", "好", "发", "ok", "OK", "可以", "准", "yes", "Yes"):
                    await self._send_confirmed(conv, pend_text)
                    return
                # 否则视为取消，继续正常处理本条

        try:
            msg = await self.build_inbound(data)
        except Exception as e:
            print(f"[QQ-PLUGIN] 消息组装失败: {e}")
            return
        # 发言护栏：收到对方消息 -> 清零 bot 连续发言计数（话题重新有人接了）
        try:
            qq_guard.note_incoming(msg.channel_type, msg.channel_id)
        except Exception as e:
            print(f"[WARN] 发言护栏记账失败（不影响主流程）: {e}")
        reply = QQReplyTarget(self, msg)

        async def _run():
            try:
                await self.core.chat.handle_message(msg, reply)
            except Exception as e:
                print(f"[QQ-PLUGIN] 消息处理失败: {e}")

        # 按会话串行化，避免同一群/会话并发抢上下文导致串台或乱序
        if getattr(config, "QQ_SERIAL_PER_CONV", True):
            key = (msg.channel_type, msg.channel_id)
            prev = self._conv_chains.get(key)

            async def _chained():
                if prev is not None:
                    try:
                        await prev
                    except Exception:
                        pass
                await _run()

            self._conv_chains[key] = asyncio.ensure_future(_chained())
        else:
            asyncio.ensure_future(_run())

    async def _send_confirmed(self, conv, text: str):
        """把已确认的草稿文本正式发出（不经过聊天大脑）。"""
        ws = self.ws
        if not ws:
            return
        message_type, target = conv
        for i, segments in enumerate(split_text_and_images(text)):
            valid = [s for s in segments
                     if not (s.get("type") == "text" and not s.get("data", {}).get("text", "").strip())]
            if not valid:
                continue
            payload = {
                "action": "send_msg",
                "params": {"message_type": message_type, "message": valid},
                "echo": f"confirm-{int(time.time() * 1000)}-{i}",
            }
            if message_type == "group":
                payload["params"]["group_id"] = target
            else:
                payload["params"]["user_id"] = target
            await ws.send(json.dumps(payload, ensure_ascii=False))
            await asyncio.sleep(config.SEND_INTERVAL_SECONDS)

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
                # 云端 ASR 不可用时（GLM Key 没配），在平台层用 NapCat 原生识别兜底：
                # 对齐新版 voice 的 native 供应商——免第三方 Key，由 NapCat 服务端解码 SILK。
                if msg.audio_wav and self._need_native_asr():
                    native_text = await self._native_asr(seg.get("data", {}))
                    if native_text:
                        msg.audio_wav = b""   # 已转成文字，避免上层再走云端识别
                        msg.text = ((msg.text or "").strip() + " " + native_text).strip()
                break

        # 视频
        for seg in message:
            if seg.get("type") == "video":
                msg.has_video = True
                msg.video_ref = {"data": seg.get("data", {}), "message_id": msg.message_id}
                break

        # 合并转发展开（对齐新版）：识别 forward 段、拉取并展开节点为引用上下文
        if getattr(config, "QQ_FORWARD_EXPAND", True):
            try:
                fwd = await self._expand_forward(message, self_id=data.get("self_id"))
                if fwd:
                    msg.text = f"[合并转发内容]\n{fwd}\n" + (msg.text or "")
            except Exception as e:
                print(f"[WARN] 合并转发展开失败（降级忽略）: {e}")

        # 视觉 VLM 路由（对齐新版 qq_view_image）：收图异步描述并去重，作为上下文
        if getattr(config, "QQ_VISION_ON_IMAGE", False):
            try:
                descs = []
                for seg in extract_images(message):
                    d = await self._vision_describe(seg)
                    if d:
                        descs.append(d)
                if descs:
                    msg.text = (msg.text or "") + "\n" + "\n".join(f"[图片内容: {d}]" for d in descs)
            except Exception as e:
                print(f"[WARN] 图片视觉描述失败（降级忽略）: {e}")

        # VLM 主动回看（对齐新版 qq_view_image 主动回看工具）：用户明确要求重新看清/识别图片时，
        # 绕过去重窗口对当前消息中的图片重新描述并注入，作为一次主动回看。
        if getattr(config, "QQ_VISION_RECALL", True):
            try:
                if vision_recall_intent(msg.text or ""):
                    imgs = extract_images(message)
                    if imgs:
                        recalls = []
                        for seg in imgs:
                            d = await self._vision_describe({"data": seg}, force=True)
                            if d:
                                recalls.append(d)
                        if recalls:
                            msg.text = (msg.text or "") + "\n" + "\n".join(
                                f"[图片主动回看: {d}]" for d in recalls)
            except Exception as e:
                print(f"[WARN] 图片主动回看失败（降级忽略）: {e}")

        # 引用消息（需要调 OneBot API，属平台职责）
        reply_info = extract_reply(message)
        if reply_info.get("id"):
            quoted = await get_quoted_message(self.ws, reply_info["id"])
            if quoted:
                # sameConversation 断言：引用的会话必须与当前一致，否则丢弃（防跨会话引用串入上下文）
                q_type = quoted.get("channel_type")
                q_gid = quoted.get("channel_id")
                if q_type and q_gid and (q_type != msg.channel_type or q_gid != msg.channel_id):
                    print(f"[INFO] 引用消息来自其他会话（{q_type}/{q_gid}），"
                          f"当前为 {msg.channel_type}/{msg.channel_id}，已丢弃避免串上下文")
                else:
                    msg.quoted_text = quoted.get("text", "")
                    msg.quoted_image_refs = quoted.get("image_refs", [])
                    msg.quoted_sender = quoted.get("user_id", "")
                    msg.quoted_self = (str(quoted.get("sender_id", "")) == str(data.get("self_id", "")))
        return msg
