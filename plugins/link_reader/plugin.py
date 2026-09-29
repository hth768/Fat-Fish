# -*- coding: utf-8 -*-
"""link_reader —— 聊天里出现链接时自动抓取正文并（可选）LLM 总结。

实现说明（对齐 manifest.config_schema）：
- 当前核心未提供「入站消息订阅」API，自动抓取走 watch_memory 兜底：
  周期性轮询 core.app_bridge.recent() 日志中近期出现的 http(s) 链接，去重后抓取+总结，
  再把结果 push 回对应会话。
- 抓取：超时受 timeout 控制；HTML 抽取可见正文；以页面 <title> + 正文片段兜底。
- 总结：summarize=True 且有 LLM 时，用 LLM 产出简短摘要，否则直接给正文片段。
- 失败：notify_fail=True 时把错误以回复形式告知，否则仅后台记录。
"""
import asyncio
import logging
import re
import html
import urllib.request
import urllib.error

from libs.qq_bot_runtime.plugin_base import FeaturePlugin, create_plugin

logger = logging.getLogger("link_reader")

URL_RE = re.compile(r"https?://[^\s，。！？、）)】\]<>\"'\\]+", re.IGNORECASE)
TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
META_DESC_RE = re.compile(
    r'<meta[^>]+name=["\']description["\'][^>]+content=["\'](.*?)["\']',
    re.IGNORECASE | re.DOTALL,
)

DEFAULTS = {
    "enabled": True,
    "auto_read": True,
    "max_links": 3,
    "max_chars": 4000,
    "timeout": 15,
    "summarize": True,
    "save_to_knowledge": False,
    "notify_fail": True,
    "watch_memory": True,
    "poll_interval": 5,
}

MANIFEST_NAME = "link_reader"


def _read_config(core) -> dict:
    cfg = dict(DEFAULTS)
    try:
        plugins = (getattr(core, "config", None) or {}).get("plugins") or {}
        saved = plugins.get(MANIFEST_NAME) or {}
        if isinstance(saved, dict):
            cfg.update({k: v for k, v in saved.items() if k in DEFAULTS})
    except Exception:
        pass
    return cfg


async def _fetch_text(url: str, timeout: int, max_chars: int) -> str:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; FeiyuLinkReader/1.0)",
            "Accept-Language": "zh-CN,zh;q=0.9",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read(2_000_000)  # 上限 2MB，避免大文件撑爆内存
        charset = resp.headers.get_content_charset() or "utf-8"
    try:
        data = raw.decode(charset, errors="replace")
    except LookupError:
        data = raw.decode("utf-8", errors="replace")
    title_m = TITLE_RE.search(data)
    title = html.unescape(title_m.group(1).strip()) if title_m else ""
    # 优先用 meta description，否则抽取正文
    desc_m = META_DESC_RE.search(data)
    if desc_m:
        body = html.unescape(desc_m.group(1).strip())
    else:
        body = re.sub(
            r"<(script|style)[^>]*>.*?</\1>", " ", data,
            flags=re.IGNORECASE | re.DOTALL,
        )
        body = re.sub(r"<[^>]+>", " ", body)
        body = html.unescape(re.sub(r"\s+", " ", body)).strip()
    text = (title + "\n" if title else "") + body
    return text[:max_chars]


async def _summarize(text: str, core) -> str:
    llm = getattr(getattr(core, "chat", None), "llm", None)
    if llm is None:
        return text[:800]
    try:
        resp = await llm.chat(
            [
                {
                    "role": "system",
                    "content": "你是链接摘要助手。用简体中文把下面网页正文总结成 3-5 句要点，"
                    "不要复述原文。若正文无效请说明。",
                },
                {"role": "user", "content": text},
            ],
            capability="chat",
        )
        return (resp.get("content") or "").strip() or text[:800]
    except Exception as e:  # noqa: BLE001
        logger.warning("link_reader 总结失败: %s", e)
        return text[:800]


class LinkReaderPlugin(FeaturePlugin):
    def __init__(self, core):
        super().__init__(core)
        self.core = core
        self._task = None
        self._seen = set()
        self._seen_cap = 2000
        self._cfg = _read_config(core)

    async def on_start(self, core):
        self.core = core
        self._cfg = _read_config(core)
        if not self._cfg.get("enabled"):
            logger.info("link_reader 已禁用")
            return
        loop = getattr(core, "loop", None)
        if loop is None:
            loop = asyncio.get_event_loop()
        self._task = loop.create_task(self._run())

    async def on_stop(self, core):
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def _run(self):
        poll = max(1, int(self._cfg.get("poll_interval", 5)))
        while True:
            try:
                await asyncio.sleep(poll)
                if self._cfg.get("auto_read") and self._cfg.get("watch_memory"):
                    await self._scan(self.core)
            except asyncio.CancelledError:
                break
            except Exception as e:  # noqa: BLE001
                logger.warning("link_reader 扫描异常: %s", e)
                await asyncio.sleep(poll)

    async def _scan(self, core):
        bridge = getattr(core, "app_bridge", None)
        if bridge is None or not hasattr(bridge, "recent"):
            return
        try:
            events = bridge.recent(200)
        except Exception:  # noqa: BLE001
            return
        todo = []
        for ev in events or []:
            if not isinstance(ev, dict):
                continue
            txt = ev.get("text") if isinstance(ev.get("text"), str) else ""
            if not txt and isinstance(ev.get("content"), str):
                txt = ev["content"]
            if not txt:
                continue
            for url in URL_RE.findall(txt):
                url = url.rstrip(".,;:!?）)】]")
                if url in self._seen:
                    continue
                self._seen.add(url)
                if len(self._seen) > self._seen_cap:
                    self._seen = set(list(self._seen)[-self._seen_cap // 2:])
                todo.append((url, ev.get("session")))
            if len(todo) >= int(self._cfg.get("max_links", 3)) * 10:
                break
        per_session = {}
        for url, session in todo:
            per_session.setdefault(session, 0)
            if per_session[session] >= int(self._cfg.get("max_links", 3)):
                continue
            per_session[session] += 1
            await self._process(url, session, core)

    async def _process(self, url, session, core):
        try:
            text = await _fetch_text(
                url,
                int(self._cfg.get("timeout", 15)),
                int(self._cfg.get("max_chars", 4000)),
            )
            if self._cfg.get("summarize"):
                out = await _summarize(text, core)
            else:
                out = text[:800]
            if not out:
                out = "（链接内容为空或无法解析）"
            await self._reply(session, f"🔗 {url}\n{out}", core)
            if self._cfg.get("save_to_knowledge"):
                await self._save_knowledge(url, out, core)
        except Exception as e:  # noqa: BLE001
            logger.warning("link_reader 处理 %s 失败: %s", url, e)
            if self._cfg.get("notify_fail"):
                await self._reply(
                    session, f"⚠️ 链接抓取失败：{url}\n错误：{e}", core
                )

    async def _reply(self, session, text, core):
        bridge = getattr(core, "app_bridge", None)
        if bridge is None:
            return
        try:
            bridge.push(session, {"type": "reply", "text": text})
        except Exception as e:  # noqa: BLE001
            logger.warning("link_reader 回复失败: %s", e)

    async def _save_knowledge(self, url, text, core):
        try:
            mem = getattr(core, "memory", None)
            if mem is not None and hasattr(mem, "add_knowledge"):
                await mem.add_knowledge(f"[link] {url}\n{text}")
        except Exception:  # noqa: BLE001
            pass


def create_plugin(core):
    return LinkReaderPlugin(core)
