# -*- coding: utf-8 -*-
"""B 站私信 AI 自动回复（白名单 UID）。

复用 bili_api.BiliSession（SESSDATA/CSRF + 风控 buvid），把 B 站私信转成
message_bus.InboundMessage 走核心 ChatService，回复经 ReplyTarget 发回私信。

安全模型（按 user 选择）：
- 仅对 config.BILIBILI_DM_WHITELIST 内的 UID 自动回复，其余私信只记录不回复（防打扰现实好友）。
- 私信里带「/」的命令仅 config.BILIBILI_DM_OWNER_UID 可触发（防他人乱控机器人）。
- 自己发的消息（sender == 自己 uid）忽略，避免回声。
- 已处理消息按 msg_seqno 去重，轮询重复不会重复回复。

总开关：config.ENABLE_BILIBILI_DM（默认 False）。
"""
import asyncio
import json
import time
import uuid

import httpx

import config
from bili_api import BiliSession, has_cookies
from message_bus import InboundMessage, ReplyTarget
from plugin_base import FeaturePlugin

_VC = "https://api.vc.bilibili.com"
_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    "Referer": "https://message.bilibili.com/",
    "Origin": "https://message.bilibili.com",
}


def _split_text(text: str, limit: int) -> list:
    """把回复切成多条合规私信：先按换行拆，过长再硬切。"""
    limit = max(20, int(limit))
    out = []
    for para in (text or "").split("\n"):
        para = para.rstrip()
        if not para:
            continue
        while len(para) > limit:
            out.append(para[:limit])
            para = para[limit:]
        out.append(para)
    return out or [""]


async def send_bili_dm(uid: str, text: str, own_uid: str = "") -> None:
    """给指定 UID 发一条（可自动切分多条）私信文本。供私信回复与定时分享共用。"""
    from bili_api import BiliSession
    session = BiliSession()
    async with httpx.AsyncClient(timeout=20, headers=_HEADERS) as http:
        await session.ensure_buvid(http)
        if not own_uid:
            try:
                r = await http.get("https://api.bilibili.com/x/web-interface/nav",
                                    headers={**_HEADERS, "Cookie": session.cookie_header()})
                own_uid = str((r.json().get("data") or {}).get("mid") or "")
            except Exception:
                own_uid = ""
        ck = session.cookie_header()
        for part in _split_text(text, getattr(config, "BILIBILI_DM_MAX_CHARS", 500)):
            if not part.strip():
                continue
            payload = {"content": part}
            data = {
                "msg[sender_uid]": own_uid,
                "msg[receiver_id]": str(uid),
                "msg[receiver_type]": "1",
                "msg[msg_type]": "1",
                "msg[content]": json.dumps(payload, ensure_ascii=False),
                "msg[dev_id]": str(uuid.uuid4()),
                "msg[timestamp]": str(int(time.time() * 1000)),
                "csrf": config.BILIBILI_CSRF,
                "csrf_token": config.BILIBILI_CSRF,
            }
            r = await http.post(f"{_VC}/web_im/v1/web_im/send_msg", data=data,
                                headers={**_HEADERS, "Cookie": ck})
            body = r.json()
            if body.get("code") != 0:
                raise RuntimeError("发私信失败 code=%s: %s" % (body.get("code"), body.get("message")))
            await asyncio.sleep(1.0)


class BilibiliDmReplyTarget(ReplyTarget):
    """私信回复上下文：把 ChatService 的回复发回对应 UID。"""

    def __init__(self, msg: InboundMessage, bot: "BilibiliDmPlugin"):
        super().__init__(msg)
        self.bot = bot

    async def reply(self, text: str):
        if not text or not text.strip():
            return
        for part in _split_text(text, getattr(config, "BILIBILI_DM_MAX_CHARS", 500)):
            await self.bot.send_dm(self.msg.user_id, part)
            await asyncio.sleep(1.0)   # 私信有频控，发一条歇一下


class BilibiliDmPlugin(FeaturePlugin):
    """B 站私信监听 + 白名单 UID 自动 AI 回复。"""

    name = "bilibili_dm"

    def __init__(self, core):
        super().__init__(core)
        self.session = BiliSession()
        self._task = None
        self._seen = set()          # 已处理消息 seqno（进程内去重）
        self._primed = set()        # 已建立「启动基线」的会话 UID（基线内历史不触发动作）
        self._own_uid = str(getattr(config, "BILIBILI_DM_OWNER_UID", "") or "")
        self._http = None
        self._whitelist = set(str(u) for u in getattr(config, "BILIBILI_DM_WHITELIST", []) or [])

    # ---------------- 生命周期 ----------------
    async def start(self):
        if not getattr(config, "ENABLE_BILIBILI_DM", False):
            return
        if not has_cookies():
            print("[BILI_DM] 未配置 BILIBILI_SESSDATA/CSRF，私信功能不可用")
            return
        self._http = httpx.AsyncClient(timeout=20, headers=_HEADERS)
        try:
            await self.session.ensure_buvid(self._http)
            uid = await self._my_uid()
            if uid:
                self._own_uid = str(uid)
                print("[BILI_DM] 已登录账号 mid=%s" % uid)
        except Exception as e:
            print("[BILI_DM] 初始化警告（不致命）: %s" % e)
        if not self._whitelist:
            print("[BILI_DM] 白名单为空：仅监听不回复（在 config.BILIBILI_DM_WHITELIST 填入 UID 启用自动回复）")
        else:
            print("[BILI_DM] 白名单 UID：%s" % ", ".join(sorted(self._whitelist)))
        self._task = asyncio.get_event_loop().create_task(self._loop())
        await super().start()

    async def stop(self):
        if self._task:
            self._task.cancel()
            self._task = None
        if self._http is not None:
            try:
                await self._http.aclose()
            except Exception:
                pass
            self._http = None
        await super().stop()

    # ---------------- 主循环 ----------------
    async def _loop(self):
        try:
            while True:
                try:
                    await self._poll_once()
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    print("[BILI_DM] 轮询异常: %s" % e)
                await asyncio.sleep(max(5, int(getattr(config, "BILIBILI_DM_POLL_SEC", 15))))
        except asyncio.CancelledError:
            pass

    async def _poll_once(self):
        # 白名单模式：直接轮询每个白名单 UID 的私信历史（无需会话列表接口）
        if not self._whitelist:
            return
        for uid in sorted(self._whitelist):
            try:
                await self._handle_session(uid)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                print("[BILI_DM] 处理 %s 失败: %s" % (uid, e))

    async def _handle_session(self, other_uid: str):
        msgs = await self._fetch_messages(other_uid)
        prime = other_uid not in self._primed
        for m in msgs:
            seq = self._seqno(m)
            if not seq:
                continue
            key = "%s_%s" % (other_uid, seq)
            if key in self._seen:
                continue
            # 启动首次拉取：仅把当前会话里的既有消息标记为「已读历史」，不触发任何动作
            # （避免一启动就把之前发过的视频/聊天全部重新解析）
            if prime:
                self._seen.add(key)
                continue
            sender = str(m.get("sender_uid") or "")
            if sender == self._own_uid:
                self._seen.add(key)      # 自己的消息不回复，也标记为已看
                continue
            content = self._parse_content(m)
            if not content:
                self._seen.add(key)
                continue
            self._seen.add(key)
            await self._dispatch(other_uid, sender, content)
        self._primed.add(other_uid)

    async def _dispatch(self, other_uid: str, sender_uid: str, text: str):
        print("[BILI_DM] 收到私信 from %s: %s" % (other_uid, text[:60]))
        lowered = text.strip()
        # 命令仅主人可触发（防他人借私信控制机器人）
        if lowered.startswith("/") and sender_uid != self._own_uid:
            print("[BILI_DM] 非主人私信带命令，忽略: %s" % lowered[:40])
            return
        # 带 B站视频链接 → 走「看视频并学习」流程（白名单内即可，主人/测试号都能用）
        from bili_learn import parse_bvid, parse_bvid_async
        bid, _ = parse_bvid(text)
        if not bid and "b23.tv" in text:
            bid = await parse_bvid_async(text)
        if bid:
            await self._watch_and_reply(other_uid, bid)
            return
        msg = InboundMessage(
            platform="bilibili_dm",
            channel_type="private",
            channel_id=other_uid,
            user_id=other_uid,
            message_id="%s_%s" % (other_uid, int(time.time() * 1000)),
            text=text,
            mentioned=True,
        )
        try:
            await self.core.chat.handle_message(msg, BilibiliDmReplyTarget(msg, self))
        except Exception as e:
            print("[BILI_DM] 处理消息失败: %s" % e)

    async def _watch_and_reply(self, other_uid: str, bid: str):
        """收到 B站视频链接：拉字幕+总结、沉淀知识库、把总结回给发送者。"""
        from bili_learn import summarize
        import knowledge_service
        await self.send_dm(other_uid, "收到视频啦，我去看看～")
        try:
            res = await summarize(bid)
        except Exception as e:
            print("[BILI_DM] 看视频 %s 失败: %s" % (bid, e))
            await self.send_dm(other_uid, "这个视频我没能看懂（%s），换个链接试试？" % e)
            return
        title = res.get("title", "") or bid
        summary = res.get("summary", "")
        # 沉淀进知识库（闲聊可召回）
        try:
            knowledge_service.get_knowledge().learn_async(title, summary)
        except Exception as e:
            print("[BILI_DM] 看后沉淀失败（不影响回复）: %s" % e)
        reply = "【看了《%s》】\n%s" % (title, summary)
        await self.send_dm(other_uid, reply)
        print("[BILI_DM] 已看完《%s》并回复 %s" % (title, other_uid))

    # ---------------- 网络 ----------------
    async def _my_uid(self) -> str:
        r = await self._http.get("https://api.bilibili.com/x/web-interface/nav",
                                 headers={**_HEADERS, "Cookie": self.session.cookie_header()})
        d = r.json().get("data") or {}
        return str(d.get("mid") or "")

    async def _fetch_messages(self, other_uid: str) -> list:
        # 按 B 站官方 svr_sync/fetch_session_msgs：拉该 UID 的最近私信
        r = await self._http.get(
            f"{_VC}/svr_sync/v1/svr_sync/fetch_session_msgs",
            params={"talker_id": other_uid, "session_type": 1, "size": 20,
                    "begin_seqno": 0, "end_seqno": 0},
            headers={**_HEADERS, "Cookie": self.session.cookie_header()})
        data = r.json().get("data") or {}
        return data.get("messages") or []

    @staticmethod
    def _seqno(m: dict) -> str:
        return str(m.get("msg_seqno") or m.get("msg_key") or m.get("timestamp") or "")

    @staticmethod
    def _parse_content(m: dict) -> str:
        mt = int(m.get("msg_type") or 0)
        raw = m.get("content") or ""
        try:
            obj = json.loads(raw) if isinstance(raw, str) else raw
        except Exception:
            obj = None
        if mt == 1:
            if isinstance(obj, dict):
                return str(obj.get("content") or "").strip()
            return raw.strip() if isinstance(raw, str) else ""
        # 分享卡片（视频/链接等，msg_type=7 等）：取出其中的 url，交给上层判断是否视频
        if isinstance(obj, dict) and obj.get("url"):
            return str(obj.get("url")).strip()
        return ""

    async def send_dm(self, uid: str, text: str):
        """给指定 UID 发私信（复用模块级 send_bili_dm）。"""
        await send_bili_dm(uid, text, self._own_uid)
        print("[BILI_DM] 已回复 %s: %s" % (uid, text[:60]))
