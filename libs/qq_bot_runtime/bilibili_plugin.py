# -*- coding: utf-8 -*-
"""B站直播平台插件：把 B 站接进智能体核心，支持两种模式（config.BILIBILI_MODE）。

streamer 模式（她当主播，最终目标）：
    开播：BiliStreamer（bili_streamer.py）调 API 拿推流地址，ffmpeg 推流；
    耳朵：连自己房间的弹幕 WebSocket，观众的每条弹幕交给聊天大脑
          （记忆/人设/情绪全生效），语音回复经 LiveReplyTarget 写进直播音频——
          她在直播里"开口"回应观众；礼物进核心，她口播感谢；开播有开场白。

viewer 模式（当弹幕机器人）：
    连别人的直播间，命中触发词的弹幕进核心，回复压缩成弹幕发出去
    （需要登录 Cookie 才能发）。

弹幕协议与风控应对（buvid/wbi）见 bili_api.py；收发协议：
    room_init 解析真实房间号 → getDanmuInfo（wbi 签名降级 / getConf 兜底）→
    wss 鉴权(op=7/8) + 30s 心跳(op=2/3) + 消息(op=5, DANMU_MSG/SEND_GIFT...)。

用法：python main.py --bili，或在 config.py 设 ENABLE_BILIBILI_PLUGIN = True
并填 BILIBILI_ROOM_ID。
"""
import asyncio
import json
import struct
import time
import zlib

import httpx
import websockets

import config
from bili_api import BiliSession, _FACEPACK_RE, _FACE_CODE_RE, _fit_danmaku, get_json, has_cookies, wbi_sign
from message_bus import InboundMessage, MessageSender, ReplyTarget, get_sender
from plugin_base import PlatformPlugin

# ---------------------------------------------------------------------------
# 弹幕二进制协议
# ---------------------------------------------------------------------------
# 弹幕包 16 字节头的 op 码
_OP_HEARTBEAT = 2        # 客户端心跳（30s 一个）
_OP_HEARTBEAT_REPLY = 3  # 人气值回包（body 4 字节大端）
_OP_MESSAGE = 5          # 业务消息（DANMU_MSG / SEND_GIFT ...）
_OP_AUTH = 7             # 客户端鉴权
_OP_AUTH_REPLY = 8       # 鉴权回包 {"code":0}

_WS_HEARTBEAT_INTERVAL = 30.0

_EVENT_USER_ID = "bili:__event__"   # 直播间事件（礼物/开播）在记忆里的"用户"
_EVENT_USER_NAME = "直播间事件"


def _start_msg() -> str:
    """开播命令的回复文案：直播姬模式提示去直播姬点推流。"""
    if str(getattr(config, "BILIBILI_PUSH_MODE", "ffmpeg") or "ffmpeg").lower() == "hime":
        return "上线啦～去直播姬点「开始推流」吧（画面和推流它管）"
    return "开播成功，推流中！"


def _pack(op: int, body: bytes = b"", protover: int = 1) -> bytes:
    """打包一个上行包：4B包长 + 2B头长 + 2B协议版本 + 4B op + 4B seq。"""
    return struct.pack(">IHHII", 16 + len(body), 16, protover, op, 1) + body


def _iter_packets(buf: bytes):
    """按 16 字节头切包，产出 (protover, op, body)。坏包/半包直接丢尾部。"""
    offset, total = 0, len(buf)
    while offset + 16 <= total:
        packet_len, header_len, protover, op, _seq = struct.unpack(">IHHII", buf[offset:offset + 16])
        if packet_len < 16 or offset + packet_len > total:
            break
        yield protover, op, buf[offset + header_len:offset + packet_len]
        offset += packet_len


def _decode_message_body(protover: int, body: bytes):
    """解 op=5 消息体：protover 0=裸 JSON，2=zlib，3=brotli（可选依赖）。

    压缩体解开还是「若干个 16 字节头子包」，继续按包切。
    """
    if protover == 0:
        return [(0, body)]
    if protover == 2:
        body = zlib.decompress(body)
    elif protover == 3:
        try:
            import brotli  # 可选依赖：鉴权时已声明 protover 2，正常不会走到这里
        except ImportError:
            return []
        body = brotli.decompress(body)
    else:
        return []
    return [(op, pbody) for _pv, op, pbody in _iter_packets(body)]


# ---------------------------------------------------------------------------
# 回复上下文 / 主动发送
# ---------------------------------------------------------------------------
class BilibiliReplyTarget(ReplyTarget):
    """viewer 模式：一条 AI 回复压缩成一条弹幕发出去。"""

    capabilities = {"group": True, "voice": False, "image": False,
                    "voice_input": False, "video_input": False}

    def __init__(self, plugin, msg: InboundMessage):
        super().__init__(msg)
        self.plugin = plugin

    async def reply(self, text: str):
        await self.plugin.send_danmaku(text)

    async def reply_voice(self, wav_path: str):
        # 弹幕发不了语音：抛异常让 chat_service 的语音分支退回文字
        raise RuntimeError("B站弹幕不支持语音回复")

    async def fetch_image(self, ref) -> bytes:
        raise RuntimeError("B站弹幕不支持接收图片")

    async def fetch_video(self, ref) -> str:
        raise RuntimeError("B站弹幕不支持接收视频")


class LiveReplyTarget(ReplyTarget):
    """streamer 模式：回复说进直播音频（她在直播里开口），可选再补一条弹幕。

    capabilities.voice_only=True：chat_service 对这个平台恒走语音分支，
    reply_voice(wav) 是主路径（大脑已用 VoxCPM/GLM 合成好，直接进推流音频）；
    reply(text) 是语音引擎关闭时的兜底（内部再 TTS 一次）。
    """

    capabilities = {"group": True, "voice": True, "voice_only": True, "image": False,
                    "voice_input": False, "video_input": False}

    def __init__(self, plugin, msg: InboundMessage):
        super().__init__(msg)
        self.plugin = plugin

    async def reply(self, text: str):
        # 文字兜底路径：speak() 内部已推字幕，这里只需发弹幕回显
        if self.plugin.streamer:
            await self.plugin.streamer.speak(_strip_unspeakable(text))
        if getattr(config, "BILIBILI_HOST_ECHO_DANMAKU", False):
            await self.plugin.send_danmaku(text)

    async def reply_voice(self, wav_path: str):
        # 主路径：大脑已合成好语音，直接写进直播音频（字幕由 chat_service 的 caption 挂钩推送）
        if self.plugin.streamer:
            await self.plugin.streamer.play_wav(wav_path)

    def caption(self, text: str):
        """chat_service 语音分支调用的字幕挂钩：把她说的话同步推给字幕服务。"""
        try:
            import bili_captions
            bili_captions.push(_strip_unspeakable(text))
        except Exception:
            pass

    async def fetch_image(self, ref) -> bytes:
        raise RuntimeError("直播模式不支持接收图片")

    async def fetch_video(self, ref) -> str:
        raise RuntimeError("直播模式不支持接收视频")


def _strip_unspeakable(text: str) -> str:
    """口播前清理：表情包/表情码标记去掉（动作描写保留，念出来也自然）。"""
    return _FACEPACK_RE.sub("", _FACE_CODE_RE.sub("", text or "")).strip()


class BilibiliSender(MessageSender):
    """主动发送适配器。

    streamer 模式：主动说话/通知 → 她在直播里说出口；
    viewer 模式：group:房间号 → 弹幕；私聊不支持。
    """

    def __init__(self, plugin):
        self.plugin = plugin

    async def send_private(self, user_id, text: str) -> bool:
        if self.plugin.mode == "streamer" and self.plugin.streamer:
            # 没有私聊通道：主动内容直接说进直播
            await self.plugin.streamer.speak(_strip_unspeakable(text))
            return True
        print(f"[BILI] 平台不支持私聊主动发送，丢弃: {str(text)[:30]}")
        return False

    async def send_group(self, group_id, text: str) -> bool:
        if self.plugin.mode == "streamer" and self.plugin.streamer:
            await self.plugin.streamer.speak(_strip_unspeakable(text))
            return True
        return await self.plugin.send_danmaku(text, room_id=str(group_id))


# ---------------------------------------------------------------------------
# 平台插件
# ---------------------------------------------------------------------------
class BilibiliPlugin(PlatformPlugin):
    """B站直播平台插件（viewer 弹幕机器人 / streamer 当主播）。

    start() 后维持一条到弹幕服务器的 WebSocket：鉴权 → 30s 心跳 → 收包分发。
    断线自动重连（3s 起指数退避，上限 60s）。
    """

    name = "bilibili"
    platform = "bilibili"
    capabilities = {"group": True, "voice": False, "image": False,
                    "voice_input": False, "video_input": False}

    def __init__(self, core):
        super().__init__(core)
        self.mode = str(getattr(config, "BILIBILI_MODE", "viewer") or "viewer").lower()
        self.room_id = str(getattr(config, "BILIBILI_ROOM_ID", "") or "").strip()
        self.real_room_id = 0        # room_init 解析出的真实房间号（短号会被重定向）
        self.live_status = 0         # 0=未开播 1=开播（room_init 顺手带出来）
        self.ws = None
        self.connected = False
        self.online = 0              # 心跳回包的人气值
        self.last_danmaku_ts = 0.0
        self.last_reply_ts = 0.0     # 上次触发 AI 回复的时间（冷却用）
        self._last_event_ts = 0.0    # 上次直播间事件（礼物/开播）的时间（轻频控用）
        self._greeted = False        # streamer 模式开场白只说一次
        self._mc_started = False     # 开播时是否成功拉起了 MC 大脑（影响开场白）
        self._last_ws_data = 0.0     # 最近一次收到弹幕服务器数据的时间（看门狗用）
        self._last_sent_text = ""    # B站不连发同样内容，重复时补个波浪号
        self._last_send_ts = 0.0
        self._send_warned = False
        self._task = None            # 连接主循环
        self._hb_task = None         # 当前连接的心跳循环
        self._stopped = False
        self.session = BiliSession()
        self.streamer = None         # streamer 模式的 BiliStreamer（懒创建）
        self.sender = BilibiliSender(self)
        self._chatter_task = None    # 主播主动闲聊循环（直播间安静时她自己找话说）
        self._last_chat_ts = 0.0     # 上次主动闲聊开口的时间
        self._on_air = False         # 是否已上线开播（待命=False，/直播 开播后=True）

    # ---- 状态 ----
    def status(self) -> dict:
        d = {**super().status(), "platform": self.platform, "capabilities": dict(self.capabilities)}
        d.update({
            "mode": self.mode,
            "on_air": self._on_air,
            "room_id": self.room_id or "未配置",
            "connected": self.connected,
            "online": self.online,
            "can_send": has_cookies(),
        })
        if self.mode == "streamer":
            try:
                d["streamer"] = self.streamer.status() if self.streamer else None
            except Exception:
                d["streamer"] = None
        if self.last_danmaku_ts:
            d["last_danmaku_seconds_ago"] = int(time.time() - self.last_danmaku_ts)
        if self.mode == "streamer":
            d["host_chat"] = bool(getattr(config, "BILIBILI_HOST_CHAT", True))
            if self._last_chat_ts:
                d["last_chat_seconds_ago"] = int(time.time() - self._last_chat_ts)
        return d

    # ---- 生命周期 ----
    async def start(self):
        """插件注册：默认只待命（能收 /直播 命令），不自动开播。

        开播动作（开声音通道/连弹幕/拉 MC 大脑）在 _boot()，由 /直播 开播
        或 BILIBILI_AUTO_START=True 触发——避免机器人一启动她就"进入直播状态"。
        """
        if not self.room_id:
            print("[BILI] 未配置 BILIBILI_ROOM_ID，B站插件不启动"
                  "（config.py 填直播间房间号后再开）")
            return
        # QQ / 控制台等其他平台没注册全局 sender 时（B 站单独运行）才接管，
        # 避免把主人通知/主动说话抢到弹幕/直播里去
        if get_sender() is None:
            from message_bus import set_sender
            set_sender(self.sender)
        await super().start()
        # viewer 模式是被动监听（无"开播"副作用），启动即连；
        # streamer 模式按 BILIBILI_AUTO_START 决定是否自动上线
        if self.mode != "streamer" or getattr(config, "BILIBILI_AUTO_START", False):
            try:
                await self._boot()
            except Exception as e:
                print(f"[BILI] 启动失败（待命中，可用 /直播 开播 重试）: {e}")
        else:
            print(f"[BILI] B站插件待命中（房间 {self.room_id}，未开播"
                  "；QQ 发「/直播 开播」让她上线）")

    async def _boot(self):
        """真正开播：开声音通道（或 ffmpeg 推流）→ 拉 MC 大脑 → 连弹幕/闲聊循环。"""
        if self._on_air:
            return
        if self.mode == "streamer":
            from bili_streamer import BiliStreamer
            if self.streamer is None:
                self.streamer = BiliStreamer(int(self.room_id))
            await self.streamer.start()      # hime=开虚拟声卡 / ffmpeg=开播+推流；失败抛异常
            # 播 MC：开播时自动拉起 MC 大脑（她开始玩，游戏窗口就是直播画面）
        self._mc_started = False
        await self._try_start_mc()
        # 她说的话 → 直播字幕（直播姬浏览器素材指向字幕服务）
        if getattr(config, "BILIBILI_CAPTIONS", True):
            try:
                import bili_captions
                bili_captions.start()
            except Exception as e:
                print(f"[BILI] 字幕服务启动异常（不影响直播）: {e}")
        self._stopped = False
        self._greeted = False
        self._on_air = True
        self._task = asyncio.get_event_loop().create_task(self._run_loop())
        # 主播主动闲聊：门槛含 _on_air/live_status，没真开播绝不自言自语
        if self._chatter_task is None:
            self._chatter_task = asyncio.get_event_loop().create_task(self._host_chat_loop())
        print(f"[BILI] 已上线开播（房间 {self.room_id}）")

    async def _go_standby(self):
        """回到待命：关弹幕/闲聊循环与声音通道（MC 大脑不动，关不关是 /mc 的事）。"""
        self._on_air = False
        self._stopped = True
        for t in (self._hb_task, self._task, self._chatter_task):
            if t:
                t.cancel()
        self._hb_task = self._task = self._chatter_task = None
        self.connected = False
        if self.streamer:
            try:
                await self.streamer.stop()
            except Exception as e:
                print(f"[BILI] 停止推流/关播异常: {e}")
        try:
            import bili_captions
            bili_captions.stop()
        except Exception:
            pass

    async def stop(self):
        await self._go_standby()
        await super().stop()

    # ---- /直播 命令入口（chat_service 调用） ----
    async def ensure_streaming(self) -> str:
        """开播上线（幂等；已在播则提示）。"""
        if self.mode != "streamer":
            return "当前是 viewer 模式（config.BILIBILI_MODE=viewer），只有 streamer 模式能开播"
        if not self.room_id:
            return "先在 config.py 填 BILIBILI_ROOM_ID 哦"
        if self._on_air:
            return "已经在直播啦"
        try:
            await self._boot()
        except Exception as e:
            return f"开播失败: {e}"
        return _start_msg()

    async def shutdown_streaming(self) -> str:
        """关播回待命（幂等）。直播姬模式下推流由直播姬管，声音/弹幕我们收工。"""
        if not self._on_air:
            return "现在没在播哦（待命中）"
        await self._go_standby()
        if str(getattr(config, "BILIBILI_PUSH_MODE", "ffmpeg") or "ffmpeg").lower() == "hime":
            return "已回待命～直播姬那边记得也点「停止推流」哦"
        return "已关播，下次见～"

    async def _try_start_mc(self):
        """开播时自动拉起 MC 大脑（BILIBILI_STREAM_WITH_MC）。失败只告警，直播照开。"""
        if not getattr(config, "BILIBILI_STREAM_WITH_MC", True):
            return
        try:
            brain = self.core.brains.get("mc_mod")
            ok = await brain.start()
            self._mc_started = bool(ok or brain.status().get("running"))
            print("[BILI] MC 大脑" + ("已启动，她开始玩 Minecraft 了"
                  if self._mc_started else "未运行（游戏开了吗？直播画面需要游戏窗口）"))
        except Exception as e:
            print(f"[BILI] MC 大脑启动异常（继续直播）: {e}")

    # ---- 主播主动闲聊：直播间安静时她自己找话说 ----
    def _host_chat_due(self, now: float) -> bool:
        """闲聊触发门槛：直播间真在播 + 观众沉默够久 + 距上次开口够久。

        直播判据是 B 站侧 live_status==1（live_status==2 是下播后的轮播，不算）；
        hime 模式下直播姬点没点「开始推流」机器人只能从这看到，没开播绝不自言自语。
        「上次开口」取 streamer.last_write_ts，弹幕回复/礼物感谢/开场白/点播都算。
        """
        if self.mode != "streamer":
            return False
        if not self._on_air:                      # 待命状态绝不开口
            return False
        if not self.connected:                    # 弹幕连接断了：既听不见观众也别瞎说
            return False
        if self.live_status != 1:                 # 直播间没在播（直播姬没点推流/已下播）
            return False
        if not (self.streamer and self.streamer.pushing):
            return False
        if not getattr(config, "BILIBILI_HOST_CHAT", True):
            return False
        idle = float(getattr(config, "BILIBILI_HOST_CHAT_IDLE", 180.0))
        interval = float(getattr(config, "BILIBILI_HOST_CHAT_INTERVAL", 300.0))
        if now - self.last_danmaku_ts < idle:
            return False
        if now - self.streamer.last_write_ts < interval:
            return False
        return True

    async def _host_chat_loop(self):
        """每 15s 扫一次触发门槛，满足就让她自己找话题开口。"""
        while True:
            try:
                await asyncio.sleep(15)
                if self._stopped:
                    continue
                if not self._host_chat_due(time.time()):
                    continue
                text = await self._generate_host_chat()
                if not text:
                    continue
                await self.streamer.speak(_strip_unspeakable(text))
                self._last_chat_ts = time.time()
                print(f"[BILI] 主播主动闲聊: {text[:40]}")
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"[BILI] 主播主动闲聊异常: {e}")

    async def _generate_host_chat(self) -> str:
        """让大脑想两句直播闲聊话。失败返回空串（下一轮再试，不打断直播）。"""
        from ai_provider import get_llm
        mc_hint = ""
        try:
            chat = self.core.chat
            if hasattr(chat, "_build_stream_mc_hint"):
                mc_hint = await chat._build_stream_mc_hint()
        except Exception:
            mc_hint = ""
        if mc_hint:
            live_desc = "你正在直播玩 Minecraft，此刻你有一个「正在进行的游戏故事」。"
        else:
            live_desc = "你现在正在直播。"
        prompt = (
            "你是正在 B 站直播的主播肥鱼娘，直播间安静了好一会儿，观众都没有发弹幕，"
            "你决定自己找个话题开口说几句，活跃一下气氛。\n"
            f"{live_desc}\n{mc_hint}\n"
            "要求：\n"
            "1. 两三句话，自然口语，可爱傲娇的语气，符合你的萌娘人设；\n"
            "2. 内容可以聊：游戏里正在发生的事（结合上面的状态说你自己在干什么/在想什么）、"
            "日常吐槽、抛一个问题给观众、随便闲聊；\n"
            "3. 这段话会被合成语音念出来：不要用表情符号、颜文字、动作描写、括号标注；\n"
            "4. 直接输出要说的话本身，不要任何解释或前缀。"
        )
        try:
            client = get_llm()
            text = (await client.chat([{"role": "user", "content": prompt}], capability="chat")).strip()
            return text[:120]
        except Exception as e:
            print(f"[BILI] 闲聊话题生成失败: {e}")
            return ""

    # ---- 连接主循环（自动重连） ----
    async def _run_loop(self):
        backoff = 3.0
        while not self._stopped:
            try:
                await self._connect_once()
                backoff = 3.0          # 正常跑过一轮连接，重置退避
            except asyncio.CancelledError:
                raise
            except Exception as e:
                print(f"[BILI] 连接异常: {e}")
            if self._stopped:
                break
            print(f"[BILI] {backoff:.0f}s 后重连...")
            try:
                await asyncio.sleep(backoff)
            except asyncio.CancelledError:
                raise
            backoff = min(backoff * 1.6, 60.0)

    async def _connect_once(self):
        """一次完整连接：解析房间 → 取弹幕服务器 → 鉴权 → 收消息直到断开。"""
        async with httpx.AsyncClient() as http:
            await self.session.ensure_buvid(http)
            d = await get_json(http, self.session, "/room/v1/Room/room_init", {"id": self.room_id})
            self.real_room_id = int(d.get("room_id") or 0)
            self.live_status = int(d.get("live_status") or 0)
            if not self.real_room_id:
                raise RuntimeError(f"房间号无效: {self.room_id}")
            token, hosts = await self._fetch_danmu_server(http)

        host = hosts[0]
        addr = f"{host.get('host')}:{host.get('wss_port') or 443}"
        print(f"[BILI] 连接直播间 {self.real_room_id}"
              f"（{'开播中' if self.live_status == 1 else '未开播'}，弹幕服务器 {addr}）")

        async with websockets.connect(f"wss://{addr}/sub", max_size=4 * 1024 * 1024,
                                      ping_interval=None) as ws:
            self.ws = ws
            self._last_ws_data = time.time()
            auth = {
                "uid": 0, "roomid": self.real_room_id,
                "protover": 2,             # zlib 压缩，标准库可解，不依赖 brotli
                "platform": "web", "type": 2, "key": token,
            }
            await ws.send(_pack(_OP_AUTH, json.dumps(auth).encode("utf-8")))
            self._hb_task = asyncio.get_event_loop().create_task(self._heartbeat_loop(ws))
            try:
                async for raw in ws:
                    self._on_ws_data(raw)
            finally:
                self.connected = False
                self.ws = None
                if self._hb_task:
                    self._hb_task.cancel()
                    self._hb_task = None

    async def _fetch_danmu_server(self, http: httpx.AsyncClient) -> tuple:
        """取弹幕服务器与鉴权 token，三级降级：明文 getDanmuInfo → wbi 签名 → 旧版 getConf。

        B 站风控（code=-352）近年挡掉了匿名明文请求，wbi 签名是现行 web 端做法。
        """
        try:
            info = await get_json(http, self.session, "/xlive/web-room/v1/index/getDanmuInfo",
                                  {"id": self.real_room_id, "type": 0})
            token, hosts = str(info.get("token") or ""), info.get("host_list") or []
            if token and hosts:
                return token, hosts
        except Exception as e:
            print(f"[BILI] getDanmuInfo 明文请求失败: {e}")
        try:
            img_key, sub_key = await self.session.wbi_keys(http)
            info = await get_json(http, self.session, "/xlive/web-room/v1/index/getDanmuInfo",
                                  wbi_sign({"id": self.real_room_id, "type": 0}, img_key, sub_key))
            token, hosts = str(info.get("token") or ""), info.get("host_list") or []
            if token and hosts:
                print("[BILI] getDanmuInfo wbi 签名请求成功")
                return token, hosts
        except Exception as e:
            print(f"[BILI] getDanmuInfo wbi 签名请求失败: {e}")
        info = await get_json(http, self.session, "/room/v1/Danmu/getConf",
                              {"room_id": self.real_room_id, "platform": "pc", "player": "web"})
        token, hosts = str(info.get("token") or ""), info.get("host_server_list") or []
        if not token or not hosts:
            raise RuntimeError("拿不到弹幕服务器（token 为空），可尝试在 config 配登录 Cookie")
        print("[BILI] 已从旧版 getConf 接口取得弹幕服务器")
        return token, hosts

    async def _heartbeat_loop(self, ws):
        """30s 应用层心跳（B 站协议 op=2），顺带做两件事：
        1. 僵尸连接看门狗：超过 120s 没收到任何数据（心跳回包/弹幕都算）就掐掉重连；
        2. 刷新开播状态：hime 模式下直播姬何时点推流机器人无从得知，定时问 B 站，
           主播闲聊等行为以 live_status==1 为准（没开播不说话）。
        库自带的协议层 ping 已关闭——B 站弹幕服务器经常不回，会误杀连接。"""
        try:
            while True:
                await ws.send(_pack(_OP_HEARTBEAT, b"[object Object]"))
                await asyncio.sleep(_WS_HEARTBEAT_INTERVAL)
                if time.time() - self._last_ws_data > 120:
                    self.connected = False
                    print("[BILI] 120s 未收到弹幕服务器任何数据，主动断开重连")
                    try:
                        await ws.close()
                    except Exception:
                        pass
                    return
                await self._refresh_live_status()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(f"[BILI] 心跳发送失败: {e}")

    async def _refresh_live_status(self):
        """拉一次房间开播状态（0=未开播 1=直播中 2=轮播）。失败保持旧值不折腾。"""
        try:
            async with httpx.AsyncClient() as http:
                d = await get_json(http, self.session, "/room/v1/Room/room_init",
                                   {"id": self.room_id or self.real_room_id})
                self.live_status = int(d.get("live_status") or 0)
        except Exception:
            pass

    # ---- 收包分发 ----
    def _on_ws_data(self, raw: bytes):
        self._last_ws_data = time.time()
        for protover, op, body in _iter_packets(raw):
            if op == _OP_AUTH_REPLY:
                try:
                    code = json.loads(body.decode("utf-8")).get("code")
                except Exception:
                    code = None
                if code == 0:
                    self.connected = True
                    print(f"[BILI] 鉴权成功，开始监听直播间 {self.real_room_id} 的弹幕")
                    if self.mode == "streamer" and not self._greeted:
                        self._greeted = True
                        if getattr(self, "_mc_started", False):
                            self._post_event("[直播事件] 直播刚刚开始，Minecraft 已经在玩了，"
                                             "跟直播间的大家打个招呼吧")
                        else:
                            self._post_event("[直播事件] 直播刚刚开始，跟大家打个招呼吧")
                else:
                    print(f"[BILI] 鉴权失败 code={code}（房间需要登录？可在 config 配 Cookie）")
            elif op == _OP_HEARTBEAT_REPLY:
                if len(body) >= 4:
                    self.online = struct.unpack(">I", body[:4])[0]
            elif op == _OP_MESSAGE:
                self._on_message_body(protover, body)
            # 其余 op（reconnect 通知等）忽略

    def _on_message_body(self, protover: int, body: bytes):
        try:
            packets = _decode_message_body(protover, body)
        except Exception as e:
            print(f"[BILI] 消息体解压失败（protover={protover}）: {e}")
            return
        for op, pbody in packets:
            if op != _OP_MESSAGE:
                continue
            try:
                data = json.loads(pbody.decode("utf-8"))
            except Exception:
                continue
            self._dispatch_event(data)

    def _dispatch_event(self, data: dict):
        cmd = str(data.get("cmd") or "")
        try:
            if cmd.startswith("DANMU_MSG"):
                self._on_danmu(data)
            elif cmd == "SEND_GIFT":
                g = data.get("data") or {}
                uname, gift, num = g.get("uname"), g.get("giftName"), g.get("num")
                print(f"[BILI] 礼物: {uname} 送 {gift}x{num}")
                if self.mode == "streamer" and getattr(config, "BILIBILI_HOST_THANK_GIFT", True):
                    self._post_event(f"[直播事件] 观众 {uname} 送出了 {gift}x{num}，"
                                     f"开心地口头感谢一下（别太长）")
            elif cmd == "GUARD_BUY":
                g = data.get("data") or {}
                print(f"[BILI] 上舰: {g.get('username')} 购买 {g.get('gift_name')}")
                if self.mode == "streamer":
                    self._post_event(f"[直播事件] 观众 {g.get('username')} 上舰了"
                                     f"（{g.get('gift_name')}），隆重地口头感谢一下")
            # 其它 cmd（进场、人气、醒目留言推送壳等）静默忽略
        except Exception as e:
            print(f"[BILI] 事件处理异常 ({cmd}): {e}")

    # ---- 弹幕/事件 → 聊天大脑 ----
    def _on_danmu(self, data: dict):
        info = data.get("info") or []
        if len(info) < 3:
            return
        text = str(info[1] or "").strip()
        who = info[2] or []
        uid = who[0] if len(who) > 0 else 0
        uname = str(who[1] or f"用户{uid}")
        if not text:
            return
        self.last_danmaku_ts = time.time()
        # 触发策略：主播模式默认回应所有弹幕；viewer 模式按触发词
        if self.mode == "streamer":
            if not (getattr(config, "BILIBILI_HOST_REPLY_ALL", True) or self._should_reply(text)):
                return
        elif not self._should_reply(text):
            return
        now = time.time()
        cooldown = float(getattr(config, "BILIBILI_REPLY_COOLDOWN", 8.0))
        if now - self.last_reply_ts < cooldown:
            print(f"[BILI] 回复冷却中，忽略: {uname}: {text[:30]}")
            return
        self.last_reply_ts = now
        msg = InboundMessage(
            platform=self.platform,
            channel_type="group",
            channel_id=str(self.real_room_id),
            user_id=f"bili:{uid}",           # 带命名空间，记忆/档案按 B 站用户隔离
            user_name=uname,
            message_id=f"bili-{int(now * 1000)}-{uid}",
            text=text,
            mentioned=True,                  # 过了筛选/主播模式，等同点名要她回应
            raw={"room_id": self.real_room_id, "uid": uid, "uname": uname},
        )
        reply = LiveReplyTarget(self, msg) if self.mode == "streamer" else BilibiliReplyTarget(self, msg)
        asyncio.get_event_loop().create_task(self._handle(msg, reply))

    def _post_event(self, text: str):
        """把直播间事件（开播/礼物）合成为一条进核心的消息，让她口播回应。

        事件用自己的轻频控（2s）：礼物感谢不该被弹幕回复的 8s 冷却误杀。
        """
        now = time.time()
        if now - self._last_event_ts < 2.0:
            print(f"[BILI] 事件频控中，丢弃: {text[:40]}")
            return
        self._last_event_ts = now
        msg = InboundMessage(
            platform=self.platform,
            channel_type="group",
            channel_id=str(self.real_room_id),
            user_id=_EVENT_USER_ID,
            user_name=_EVENT_USER_NAME,
            message_id=f"bili-event-{int(now * 1000)}",
            text=text,
            mentioned=True,
            raw={"room_id": self.real_room_id, "event": text},
        )
        reply = LiveReplyTarget(self, msg)
        asyncio.get_event_loop().create_task(self._handle(msg, reply))

    def _should_reply(self, text: str) -> bool:
        """viewer 触发策略：全回复开关，或命中触发词（@机器人名 也在词表里配）。"""
        if getattr(config, "BILIBILI_REPLY_ALL", False):
            return True
        words = getattr(config, "BILIBILI_TRIGGER_WORDS", None) or []
        low = text.lower()
        return any(w and str(w).lower() in low for w in words)

    async def _handle(self, msg: InboundMessage, reply: ReplyTarget):
        try:
            await self.core.chat.handle_message(msg, reply)
        except Exception as e:
            print(f"[BILI] 处理弹幕失败: {e}")

    # ---- 发弹幕（viewer 模式回复 / 弹幕回显用） ----
    async def send_danmaku(self, text: str, room_id: str = "") -> bool:
        """发一条弹幕。未配 Cookie 时只记录不发送；有风控频控与同内容去重。"""
        text = _fit_danmaku(_strip_unspeakable(text))
        if not text:
            return False
        if not has_cookies():
            if not self._send_warned:
                self._send_warned = True
                print("[BILI] 未配置 BILIBILI_SESSDATA/BILIBILI_CSRF，机器人只能听不能说"
                      "（config.py 填登录 Cookie 后才会真发弹幕）")
            print(f"[BILI] （仅记录，未发送）想发弹幕: {text}")
            return False
        # 发送频控：距上次发送不足间隔就等一等
        min_gap = float(getattr(config, "BILIBILI_DANMAKU_INTERVAL", 5.0))
        wait = min_gap - (time.time() - self._last_send_ts)
        if wait > 0:
            await asyncio.sleep(wait)
        if text == self._last_sent_text:
            text += "~"
        target = int(room_id or self.real_room_id or self.room_id or 0)
        try:
            async with httpx.AsyncClient() as http:
                r = await http.post(
                    "https://api.live.bilibili.com/msg/send",
                    headers={**{"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                                "Referer": "https://live.bilibili.com/"},
                             "Cookie": self.session.cookie_header()},
                    data={
                        "bubble": 0,
                        "msg": text,
                        "color": int(getattr(config, "BILIBILI_DANMAKU_COLOR", 16777215)),
                        "mode": 1,
                        "room_type": 0,
                        "jumpfrom": 0,
                        "reply_mid": 0,
                        "reply_attr": 0,
                        "recon_str": "",
                        "at_uids": "",
                        "emoji": "",
                        "csrf_token": config.BILIBILI_CSRF,
                        "csrf": config.BILIBILI_CSRF,
                        "platform": "pc",
                        "fontsize": 25,
                        "rnd": int(time.time()),
                        "roomid": target,
                        "anonymous": 0,
                        "visit_id": "",
                    },
                    timeout=10,
                )
            self._last_send_ts = time.time()
            self._last_sent_text = text
            resp = r.json() if r.status_code == 200 else {}
            if resp.get("code") == 0:
                print(f"[BILI] 弹幕已发送: {text}")
                return True
            print(f"[BILI] 弹幕发送失败 code={resp.get('code')}: {resp.get('message', r.status_code)}"
                  f"（文本: {text}）")
            return False
        except Exception as e:
            print(f"[BILI] 弹幕发送异常: {e}")
            return False
