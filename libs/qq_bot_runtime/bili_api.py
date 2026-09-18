# -*- coding: utf-8 -*-
"""B 站共享 API 层：Cookie 会话、风控应对、公开接口调用。

被 bilibili_plugin.py（弹幕收发）与 bili_streamer.py（开播推流）共用。

风控应对（B 站 2023 起 code=-352）：
  - 匿名请求先经 finger/spi 领 buvid3/buvid4 Cookie；
  - 需要签名的是 getDanmuInfo 等接口，用 wbi 签名（nav 取 key，MD5）。
"""
import asyncio
import hashlib
import re
import time
import urllib.parse
import uuid

import httpx

import config

_API_BASE = "https://api.live.bilibili.com"
_WEB_BASE = "https://api.bilibili.com"

_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    "Referer": "https://live.bilibili.com/",
    "Origin": "https://live.bilibili.com",
}

# 提示去重：每个 BiliApi 实例都会领一次 buvid（进程内多实例），逐实例打印会刷屏，
# 故这两条提示各自整个进程只打一次。
_BUVID_NOTICE_DONE = False
_BUVID_FAIL_NOTICE_DONE = False

# wbi 签名混淆表（bilibili-API-collect，长期不变）
_WBI_MIXIN_KEY_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52,
]


def has_cookies() -> bool:
    """是否配置了发弹幕/开播用的登录 Cookie。"""
    return bool(getattr(config, "BILIBILI_SESSDATA", "") and getattr(config, "BILIBILI_CSRF", ""))


class BiliSession:
    """B 站访问会话：拼 Cookie、给匿名访问领 buvid。

    B 站风控要求请求带 buvid3/buvid4：匿名读弹幕先用 finger/spi 领一对
    （进程内缓存），配置了登录 Cookie（SESSDATA/bili_jct）就一并带上。
    """

    def __init__(self):
        self.buvid3 = ""
        self.buvid4 = ""
        self._fetched = False
        self._wbi = None            # (img_key, sub_key)
        self._wbi_ts = 0.0

    def cookie_header(self) -> str:
        parts = []
        if getattr(config, "BILIBILI_SESSDATA", ""):
            parts.append(f"SESSDATA={config.BILIBILI_SESSDATA}")
        if getattr(config, "BILIBILI_CSRF", ""):
            parts.append(f"bili_jct={config.BILIBILI_CSRF}")
        if self.buvid3:
            parts.append(f"buvid3={self.buvid3}")
        if self.buvid4:
            parts.append(f"buvid4={self.buvid4}")
        if not parts:
            # 兜底：合法格式的随机 buvid（真 buvid = 带横线 UUID + infoc）
            parts.append(f"buvid3={uuid.uuid4()}infoc")
        return "; ".join(parts)

    async def ensure_buvid(self, http: httpx.AsyncClient):
        """领一次匿名 buvid（实例内缓存）。失败不致命，cookie_header 有随机兜底。"""
        if self._fetched:
            return
        self._fetched = True
        try:
            r = await http.get(f"{_WEB_BASE}/x/frontend/finger/spi",
                               headers=_HEADERS, timeout=10)
            r.raise_for_status()
            d = r.json().get("data") or {}
            self.buvid3 = str(d.get("b_3") or "")
            self.buvid4 = str(d.get("b_4") or "")
            # 每个 BiliApi 实例都会领一次，逐实例打印会把控制台刷满 → 整个进程只提示一次
            global _BUVID_NOTICE_DONE
            if self.buvid3 and not _BUVID_NOTICE_DONE:
                _BUVID_NOTICE_DONE = True
                print("[BILI] 已获取匿名 buvid（风控 Cookie）")
        except Exception as e:
            global _BUVID_FAIL_NOTICE_DONE
            if not _BUVID_FAIL_NOTICE_DONE:
                _BUVID_FAIL_NOTICE_DONE = True
                print(f"[BILI] 获取 buvid 失败（用随机兜底，后续不再重复提示）: {e}")

    async def wbi_keys(self, http: httpx.AsyncClient) -> tuple:
        """取 wbi 签名密钥（nav 匿名也能拿，key 每天轮换，进程内缓存 1 小时）。"""
        if self._wbi and time.time() - self._wbi_ts < 3600:
            return self._wbi
        r = await http.get(f"{_WEB_BASE}/x/web-interface/nav",
                           headers={**_HEADERS, "Cookie": self.cookie_header()}, timeout=10)
        wbi = ((r.json().get("data") or {}).get("wbi_img")) or {}
        img_key = str(wbi.get("img_url") or "").rsplit("/", 1)[-1].split(".")[0]
        sub_key = str(wbi.get("sub_url") or "").rsplit("/", 1)[-1].split(".")[0]
        if not img_key or not sub_key:
            raise RuntimeError("获取 wbi 签名密钥失败")
        self._wbi, self._wbi_ts = (img_key, sub_key), time.time()
        return self._wbi


async def get_json(http: httpx.AsyncClient, session: BiliSession, path: str, params: dict, wbi: bool = False) -> dict:
    """调 B 站公开 GET 接口，非 0 返回码抛异常。wbi=True 时自动加 wbi 签名。"""
    if wbi:
        params = wbi_sign(dict(params), *await session.wbi_keys(http))
    r = await http.get(f"{_API_BASE}{path}", params=params,
                       headers={**_HEADERS, "Cookie": session.cookie_header()}, timeout=10)
    r.raise_for_status()
    data = r.json()
    if data.get("code") != 0:
        raise RuntimeError(f"B站接口 {path} 返回 code={data.get('code')}: {data.get('message', '')}")
    return data.get("data") or {}


async def post_json(http: httpx.AsyncClient, session: BiliSession, path: str, data: dict) -> dict:
    """调 B 站 POST 接口（自动带上 csrf），非 0 返回码抛异常。"""
    if getattr(config, "BILIBILI_CSRF", ""):
        data = {**data, "csrf": config.BILIBILI_CSRF, "csrf_token": config.BILIBILI_CSRF}
    r = await http.post(f"{_API_BASE}{path}", data=data,
                        headers={**_HEADERS, "Cookie": session.cookie_header()}, timeout=15)
    r.raise_for_status()
    body = r.json()
    if body.get("code") != 0:
        raise RuntimeError(f"B站接口 {path} 返回 code={body.get('code')}: {body.get('message', '')}")
    return body.get("data") or {}


async def fetch_up_videos(http: httpx.AsyncClient, session: BiliSession, uid: str, ps: int = 30) -> list:
    """取某 UP 主最新投稿的 bvid 列表（按发布时间倒序，走主站 WEB_BASE）。"""
    web_headers = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
        "Referer": "https://www.bilibili.com/",
        "Origin": "https://www.bilibili.com",
    }
    params = {"mid": str(uid), "ps": ps, "pn": 1, "order": "pubdate"}
    keys = await session.wbi_keys(http)
    signed = wbi_sign(params, *keys)
    # B站常对「新 buvid 的首个请求」返回 412，重试一次即可
    last = None
    for _ in range(2):
        r = await http.get(f"{_WEB_BASE}/x/space/wbi/arc/search", params=signed,
                           headers={**web_headers, "Cookie": session.cookie_header()}, timeout=10)
        if r.status_code == 412:
            last = r
            await asyncio.sleep(1.5)
            continue
        r.raise_for_status()
        d = r.json()
        if d.get("code") != 0:
            raise RuntimeError(f"B站 space 接口返回 code={d.get('code')}: {d.get('message', '')}")
        vlist = ((d.get("data") or {}).get("list") or {}).get("vlist") or []
        return [v.get("bvid") for v in vlist if v.get("bvid")]
    raise RuntimeError(f"fetch_up_videos 412: {getattr(last, 'text', '')[:80]}")


async def fetch_search_videos(http: httpx.AsyncClient, session: BiliSession, keyword: str, ps: int = 20, pn: int = 1) -> list:
    """按关键词搜索 B 站视频，返回 bvid 列表（综合排序，已过滤直播/付费/广告）。

    用于「按方向学习」：用户只给大致主题词（如「编程」「心理学」「冷知识」），
    由 bot 自行搜索相关视频来学习，而不必指定具体 UP。
    """
    search_headers = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
        "Referer": "https://search.bilibili.com/",
        "Origin": "https://search.bilibili.com",
    }
    params = {
        "search_type": "video",
        "keyword": keyword,
        "page": pn,
        "page_size": ps,
        "order": "totalrank",
        "web_location": "333.1007",
    }
    keys = await session.wbi_keys(http)
    signed = wbi_sign(params, *keys)
    last = None
    for _ in range(2):
        r = await http.get(f"{_WEB_BASE}/x/web-interface/wbi/search/type", params=signed,
                           headers={**search_headers, "Cookie": session.cookie_header()}, timeout=10)
        if r.status_code == 412:
            last = r
            await asyncio.sleep(1.5)
            continue
        r.raise_for_status()
        d = r.json()
        if d.get("code") != 0:
            raise RuntimeError(f"B站搜索接口返回 code={d.get('code')}: {d.get('message', '')}")
        items = (d.get("data") or {}).get("result") or []
        out = []
        for it in items:
            b = it.get("bvid") or ""
            if not b:
                continue
            if it.get("is_live") or it.get("type") == "live":
                continue
            if it.get("is_pay") or it.get("is_charge") or it.get("corresponding_anchor"):
                continue
            out.append(b)
        return out[:ps]
    raise RuntimeError(f"fetch_search_videos 412: {getattr(last, 'text', '')[:80]}")


def wbi_sign(params: dict, img_key: str, sub_key: str) -> dict:
    """wbi 签名（bilibili-API-collect 标准算法）：params + wts/w_rid。"""
    mixin_key = (img_key + sub_key)
    mixin_key = "".join(mixin_key[i] for i in _WBI_MIXIN_KEY_TAB)[:32]
    signed = {k: "".join(c for c in str(v) if c not in "!'()*")
              for k, v in sorted({**params, "wts": int(time.time())}.items())}
    query = urllib.parse.urlencode(signed)
    signed["w_rid"] = hashlib.md5((query + mixin_key).encode()).hexdigest()
    return signed


def _fit_danmaku(text: str) -> str:
    """把 AI 回复压成一条合规弹幕：去表情码/动作描写，断在标点上截到长度上限。"""
    limit = max(1, int(getattr(config, "BILIBILI_DANMAKU_MAX_CHARS", 20)))
    text = _ACTION_RE.sub("", _FACE_CODE_RE.sub("", _FACEPACK_RE.sub("", text or "")))
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    for i in range(len(cut) - 1, max(len(cut) - 8, 1) - 1, -1):
        if cut[i] in "。，！？、；：~～,.!?;: ":
            return cut[:i].rstrip("，,、 ")
    return cut


# 回复文本清洗（弹幕用）：弹幕里渲染不了的标记都去掉
_FACEPACK_RE = re.compile(r"\[表情包:[^\[\]]*\]")   # [表情包:xxx]
_FACE_CODE_RE = re.compile(r"\[[^\[\]]{1,8}\]")     # [旺柴] 等 QQ 表情码
_ACTION_RE = re.compile(r"[（(][^）()]{0,40}[)）]")  # （动作描写）
