# -*- coding: utf-8 -*-
"""B站视频理解 + 知识沉淀（移植自 bilibili_learning_bot 的视频理解/知识库范式）。

复用 qq_bot 自有基础设施，不引入 BLB 的 LLM/记忆/人格系统，保持单一人格单账号：
  * bili_api.BiliSession / wbi_sign —— B站公开接口访问（匿名即可，无需登录 Cookie）
  * ai_provider.get_llm().chat     —— DeepSeek 总结 + 知识点提炼
  * knowledge_service.get_knowledge().learn_async —— 把总结沉淀进 qq_bot 已有知识库（闲聊可召回）

命令（QQ/控制台）：
    /b站看 BV1xx411c7mD
    /b站看 https://www.bilibili.com/video/BV1xx411c7mD
"""
import os
import re
import subprocess
import tempfile
import time
import httpx

import config
from bili_api import BiliSession, wbi_sign
from quiet import degrade

_API_BASE = "https://api.bilibili.com"
# 主站接口需要主站 Referer/Origin（live.bilibili.com 的会被网关 412 拦截）
_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    "Referer": "https://www.bilibili.com/",
    "Origin": "https://www.bilibili.com",
}

_BV_RE = re.compile(r"(BV[0-9A-Za-z]+)")
_AV_RE = re.compile(r"av(\d+)", re.IGNORECASE)


def parse_bvid(text: str):
    """从用户输入解析出 bvid / avid。返回 (id_str, is_bv)。"""
    m = _BV_RE.search(text)
    if m:
        return m.group(1), True
    m = _AV_RE.search(text)
    if m:
        return "av" + m.group(1), False
    return "", False


async def parse_bvid_async(text: str) -> str:
    """从文本解析 bvid，支持 b23.tv 短链（跟随重定向还原成 BV）。返回 bvid 或空串。"""
    bid, _ = parse_bvid(text)
    if bid:
        return bid
    m = _BV_RE.search(text)  # 已经是 BV 但上面没匹配到？不会，保留兜底
    if m:
        return m.group(1)
    sm = re.search(r"b23\.tv/[A-Za-z0-9]+", text)
    if not sm:
        return ""
    url = "https://" + sm.group(0)
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=15) as http:
            r = await http.get(url, headers=_HEADERS)
            final = str(r.url)
        bid, _ = parse_bvid(final)
        return bid
    except Exception as e:
        print(f"[BILI_LEARN] b23.tv 短链解析失败: {e}")
        return ""


async def _api_get(http: httpx.AsyncClient, sess: BiliSession, path: str, params: dict) -> dict:
    """带 wbi 签名的 B站公开 GET 接口调用（get_json 不带签名，view 类接口强制 wbi）。"""
    keys = await sess.wbi_keys(http)
    signed = wbi_sign(dict(params), *keys)
    r = await http.get(_API_BASE + path, params=signed,
                        headers={**_HEADERS, "Cookie": sess.cookie_header()}, timeout=15)
    r.raise_for_status()
    d = r.json()
    if d.get("code") != 0:
        raise RuntimeError(f"B站接口 {path} 返回 code={d.get('code')}: {d.get('message', '')}")
    return d.get("data") or {}


async def fetch_video_meta(bvid: str) -> dict:
    async with httpx.AsyncClient() as http:
        sess = BiliSession()
        await sess.ensure_buvid(http)
        params = {"aid": bvid[2:]} if bvid.lower().startswith("av") else {"bvid": bvid}
        return await _api_get(http, sess, "/x/web-interface/view", params)


async def fetch_subtitle_text(bvid: str) -> str:
    """拉字幕并拼接文本；无字幕或下载失败返回空串（降级为仅用元信息总结）。"""
    try:
        async with httpx.AsyncClient() as http:
            sess = BiliSession()
            await sess.ensure_buvid(http)
            params = {"aid": bvid[2:]} if bvid.lower().startswith("av") else {"bvid": bvid}
            info = await _api_get(http, sess, "/x/player/wbi/v2", params)
            subs = (info.get("subtitle") or {}).get("subtitles") or []
            if not subs:
                return ""
            # 优先 AI 生成的中文
            subs.sort(key=lambda s: (0 if s.get("lan") == "zh-CN" else 1,
                                      0 if "ai" in str(s.get("lan_doc", "")).lower() else 1))
            url = (subs[0].get("subtitle_url") or "").strip()
            if not url:
                return ""
            if url.startswith("//"):
                url = "https:" + url
            r = await http.get(url, headers={**_HEADERS}, timeout=15)
            r.raise_for_status()
            data = r.json()
            body = data.get("body") or []
            text = "\n".join(str(b.get("content", "")) for b in body if b.get("content"))
            return text[:8000]
    except Exception as e:
        print(f"[BILI_LEARN] 字幕拉取失败（降级为仅用元信息）: {e}")
        return ""


async def fetch_audio_transcript(bvid: str) -> str:
    """下载视频音频（截前 N 秒），分段 ASR，返回拼接文字。

    用于「字幕缺失」时靠声音补齐学习内容：拉 B站音频流 → ffmpeg 截前
    BILIBILI_AUDIO_MAX_SEC 秒转 16k wav → 复用 video_processor 的分段 ASR
    （底层为 voice_client.speech_to_text / glm-asr-2512）。无音频/失败返回空串。
    """
    try:
        max_sec = int(getattr(config, "BILIBILI_AUDIO_MAX_SEC", 900))
        async with httpx.AsyncClient(timeout=30) as http:
            sess = BiliSession()
            await sess.ensure_buvid(http)
            params = {"aid": bvid[2:]} if bvid.lower().startswith("av") else {"bvid": bvid}
            meta = await _api_get(http, sess, "/x/web-interface/view", params)
            cid = meta.get("cid") or 0
            if not cid:
                return ""
            # playurl 拿 dash 音频流地址（wbi 签名 + 主站 referer）
            info = await _api_get(http, sess, "/x/player/wbi/playurl",
                                  {"bvid": bvid, "cid": cid, "fnval": 16, "fourk": 1})
            dash = info.get("dash") or {}
            audios = [a for a in (dash.get("audio") or []) if a.get("base_url")]
            if not audios:
                return ""   # 无音频流（如仅图文动态）
            audios.sort(key=lambda a: int(a.get("bandwidth") or 0), reverse=True)
            audio_url = audios[0]["base_url"]

        # ffmpeg 直连音频流，截前 max_sec 秒转 16k 单声道 wav（仅拉所需，省存储）
        from video_processor import get_ffmpeg_path
        ffmpeg = get_ffmpeg_path()
        tmp_base = getattr(config, "VIDEO_TMP_DIR", "") or tempfile.gettempdir()
        os.makedirs(tmp_base, exist_ok=True)
        wav_path = os.path.join(tmp_base, f"bili_audio_{os.getpid()}_{int(time.time())}.wav")
        cookie = sess.cookie_header()
        headers = (
            "Referer: https://www.bilibili.com/\r\n"
            f"User-Agent: {_HEADERS['User-Agent']}\r\n"
            f"Cookie: {cookie}\r\n"
        )
        proc = subprocess.run(
            [ffmpeg, "-y", "-loglevel", "error",
             "-headers", headers, "-i", audio_url,
             "-t", str(max_sec), "-vn", "-ac", "1", "-ar", "16000", wav_path],
            capture_output=True, timeout=300,
        )
        if proc.returncode != 0 or not os.path.exists(wav_path):
            print(f"[BILI_LEARN] 音频拉取/转码失败: {proc.stderr.decode('utf-8', errors='ignore')[:160]}")
            return ""
        try:
            from video_processor import _transcribe_audio_segments
            segs = await _transcribe_audio_segments(wav_path, segment_len=30.0)
            text = "\n".join(s.get("text", "") for s in segs if s.get("text"))
            text = text.strip()
            # 控制长度，避免喂爆 LLM
            return text[:8000]
        finally:
            try:
                os.remove(wav_path)
            except OSError as e2:
                degrade("bili_learn.fetch_audio_transcript", e2, "删转写临时音频失败")
    except Exception as e:
        print(f"[BILI_LEARN] 音频 ASR 补齐失败（降级为仅用元信息）: {e}")
        return ""


async def summarize(bvid: str) -> dict:
    """拉视频元信息+字幕，用 DeepSeek 总结并提炼知识点。返回 {title, up, tname, summary}。"""
    from ai_provider import get_llm

    meta = await fetch_video_meta(bvid)
    title = meta.get("title", "") or ""
    up = (meta.get("owner") or {}).get("name", "") or ""
    tname = meta.get("tname", "") or ""
    desc = (meta.get("desc") or "")[:600]
    dur = meta.get("duration", 0) or 0
    tags = "、".join(
        (t.get("tag_name") if isinstance(t, dict) else str(t))
        for t in (meta.get("tag") or [])[:8]
    )
    sub = await fetch_subtitle_text(bvid)
    # 无字幕 → 尝试用音频 ASR 补齐（听声音），受开关与时长上限控制
    audio_text = ""
    if not sub and getattr(config, "ENABLE_BILI_AUDIO_ASR", True):
        audio_text = await fetch_audio_transcript(bvid)

    parts = [f"标题：{title}", f"UP主：{up}　分区：{tname}　时长：{dur}秒"]
    if tags:
        parts.append(f"标签：{tags}")
    if desc:
        parts.append(f"简介：{desc}")
    if sub:
        parts.append("字幕内容（节选）：\n" + sub)
    elif audio_text:
        sec = getattr(config, "BILIBILI_AUDIO_MAX_SEC", 900)
        parts.append("（该视频无字幕，以下为音频语音识别 ASR 文本，节选自前 %d 分钟的声音）：\n%s"
                     % (sec // 60, audio_text))
    else:
        parts.append("（该视频无字幕也无音频，仅基于标题/简介/标签总结）")

    prompt = (
        "你是肥鱼娘（一个软萌但有点毒舌的 AI 少女）的知识整理助手。"
        "下面是一则 B 站视频的元信息"
        + ("和字幕" if sub else ("和音频转写文稿" if audio_text else ""))
        + "，请帮我处理：\n"
        "1) 用 1-2 句话概括这视频讲的是什么；\n"
        "2) 列出 3-5 条核心要点；\n"
        "3) 提炼 2-4 条可以长期记住的『知识点』（通用、可复用的信息，不要流水账）。\n"
        "用中文、分点、简洁，不要编造素材里没有的内容。\n\n"
        + "\n".join(parts)
    )

    llm = get_llm()
    # 注意：ai_provider.chat() 返回纯字符串（msg["content"]），不是元组
    summary = await llm.chat(
        messages=[{"role": "user", "content": prompt}],
        capability="chat", role="bili_learn",
    )
    return {"title": title, "up": up, "tname": tname, "summary": summary or ""}
