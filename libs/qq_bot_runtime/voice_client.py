# -*- coding: utf-8 -*-
"""智谱语音能力封装：TTS（文字转语音）+ ASR（语音转文字）。

TTS：返回 PCM 音频，需用 ffmpeg 转成 wav/mp3 后发送 QQ 语音
ASR：接收 wav/mp3，返回识别文字
"""
import asyncio
import hashlib
import subprocess
import tempfile
import os

import httpx

import config

TTS_URL = "https://api.z.ai/api/paas/v4/audio/speech"
ASR_URL = "https://api.z.ai/api/paas/v4/audio/transcriptions"

TTS_MODEL = "glm-tts"
ASR_MODEL = "glm-asr-2512"
TTS_VOICE = "tongtong"  # 童童，可爱音色，适合肥鱼娘


def _tmp_path(suffix: str) -> str:
    """语音中间临时文件：放项目 voice_tmp 目录，避免写进系统 C 盘 TEMP。"""
    base = os.path.dirname(os.path.abspath(__file__))
    d = os.path.join(base, getattr(config, "VOICE_DIR", "voice_tmp") or "voice_tmp")
    try:
        os.makedirs(d, exist_ok=True)
        return tempfile.mktemp(prefix="vc_", suffix=suffix, dir=d)
    except OSError:
        return tempfile.mktemp(suffix=suffix)   # 目录建不了才退回系统 TEMP

# ---------------------------------------------------------------------------
# 并发限流 + TTS 本地缓存
# ---------------------------------------------------------------------------
_concurrency_sem = None


def _limit() -> asyncio.Semaphore:
    """云端请求并发闸：单例惰性创建（绑定首次使用的事件循环，兼容旧 Python）。"""
    global _concurrency_sem
    if _concurrency_sem is None:
        _concurrency_sem = asyncio.Semaphore(getattr(config, "VOICE_MAX_CONCURRENCY", 3))
    return _concurrency_sem


def _tts_cache_dir() -> str:
    """TTS 缓存目录（voice_tmp/tts_cache）。"""
    base = os.path.dirname(os.path.abspath(__file__))
    d = os.path.join(base, config.VOICE_DIR, "tts_cache")
    os.makedirs(d, exist_ok=True)
    return d


def _tts_cache_key(text: str) -> str:
    """缓存键：模型+音色+文本 的 MD5（换音色/模型自动失效）。"""
    raw = f"{TTS_MODEL}|{TTS_VOICE}|{text}".encode("utf-8")
    return hashlib.md5(raw).hexdigest()


def _write_atomic(path: str, data: bytes) -> None:
    """先写临时文件再原子替换，避免进程中断留下半截损坏缓存。"""
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)
    # 缓存文件超过上限时删除最旧的（保目录体积）
    try:
        limit = getattr(config, "VOICE_TTS_CACHE_MAX_FILES", 300)
        files = [os.path.join(_tts_cache_dir(), n) for n in os.listdir(_tts_cache_dir())
                 if n.endswith(".pcm")]
        for old in sorted(files, key=os.path.getmtime)[:-limit]:
            try:
                os.remove(old)
            except OSError:
                pass
    except OSError:
        pass


def _get_api_key():
    return config.GLM_API_KEY


def run_ffmpeg(args: list) -> bool:
    """执行 ffmpeg 命令，返回是否成功。"""
    ffmpeg = config.FFMPEG_PATH if config.FFMPEG_PATH else "ffmpeg"
    try:
        result = subprocess.run(
            [ffmpeg, "-y", "-loglevel", "error"] + args,
            capture_output=True,
            timeout=120,
        )
        if result.returncode != 0:
            # 打印 ffmpeg 的错误输出，方便排查
            err = result.stderr.decode("utf-8", errors="ignore").strip()
            print(f"[WARN] ffmpeg 执行失败: {err[:200]}")
            return False
        return True
    except FileNotFoundError:
        print(f"[WARN] 找不到 ffmpeg 命令（路径: {ffmpeg}）。请安装 ffmpeg 并在 config.py 设置 FFMPEG_PATH。")
        return False
    except (subprocess.TimeoutExpired, OSError) as e:
        print(f"[WARN] ffmpeg 执行异常: {e}")
        return False


async def text_to_speech(text: str) -> bytes:
    """文字转语音，返回 PCM 音频字节（相同文本命中本地缓存时不再调云端）。"""
    # ---- 缓存查找：命中直接返回，省一次云端调用 ----
    cache_path = None
    if getattr(config, "ENABLE_VOICE_TTS_CACHE", True):
        cache_path = os.path.join(_tts_cache_dir(), _tts_cache_key(text) + ".pcm")
        try:
            with open(cache_path, "rb") as f:
                return f.read()
        except FileNotFoundError:
            pass
        except OSError as e:
            print(f"[WARN] TTS 缓存读取失败，本次不缓存: {e}")
            cache_path = None  # 缓存损坏：放弃写缓存，避免把坏数据固化成永久错误

    headers = {
        "Authorization": f"Bearer {_get_api_key()}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": TTS_MODEL,
        "input": text,
        "voice": TTS_VOICE,
    }
    async with _limit():
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(TTS_URL, json=payload, headers=headers)
    if resp.status_code != 200:
        raise RuntimeError(f"TTS 失败 {resp.status_code}: {resp.text[:200]}")

    # ---- 缓存写入（失败只告警，不影响本次结果）----
    if cache_path:
        try:
            _write_atomic(cache_path, resp.content)
        except OSError as e:
            print(f"[WARN] TTS 缓存写入失败: {e}")
    return resp.content  # PCM 音频


def pcm_to_wav(pcm_bytes: bytes, out_path: str, sample_rate: int = 24000) -> bool:
    """PCM 转 WAV。sample_rate 需与 TTS 返回的采样率匹配，glm-tts 默认 24kHz。"""
    tmp_pcm = _tmp_path(".pcm")
    with open(tmp_pcm, "wb") as f:
        f.write(pcm_bytes)
    ok = run_ffmpeg([
        "-f", "s16le", "-ar", str(sample_rate), "-ac", "1",
        "-i", tmp_pcm, out_path,
    ])
    if os.path.exists(tmp_pcm):
        os.remove(tmp_pcm)
    return ok


def pcm_to_mp3(pcm_bytes: bytes, out_path: str, sample_rate: int = 24000) -> bool:
    """PCM 转 MP3。"""
    tmp_pcm = _tmp_path(".pcm")
    with open(tmp_pcm, "wb") as f:
        f.write(pcm_bytes)
    ok = run_ffmpeg([
        "-f", "s16le", "-ar", str(sample_rate), "-ac", "1",
        "-i", tmp_pcm, "-codec:a", "libmp3lame", "-b:a", "64k", out_path,
    ])
    if os.path.exists(tmp_pcm):
        os.remove(tmp_pcm)
    return ok


def silk_to_wav(silk_bytes: bytes, out_path: str, sample_rate: int = 24000) -> bool:
    """SILK V3 语音解码成 WAV。

    流程：silk_v3_decoder 解码成 PCM → ffmpeg 转 WAV。
    """
    tmp_silk = _tmp_path(".silk")
    tmp_pcm = _tmp_path(".pcm")
    with open(tmp_silk, "wb") as f:
        f.write(silk_bytes)

    decoder = config.SILK_DECODER_PATH if config.SILK_DECODER_PATH else "silk_v3_decoder.exe"
    try:
        result = subprocess.run(
            [decoder, tmp_silk, tmp_pcm],
            capture_output=True,
            timeout=120,
        )
        if result.returncode != 0:
            err = result.stderr.decode("utf-8", errors="ignore").strip()
            print(f"[WARN] silk 解码失败: {err[:200]}")
            return False
    except FileNotFoundError:
        print(f"[WARN] 找不到 silk_v3_decoder（路径: {decoder}）")
        return False
    except (subprocess.TimeoutExpired, OSError) as e:
        print(f"[WARN] silk 解码异常: {e}")
        return False

    # PCM 转 WAV
    ok = run_ffmpeg([
        "-f", "s16le", "-ar", str(sample_rate), "-ac", "1",
        "-i", tmp_pcm, out_path,
    ])

    for p in (tmp_silk, tmp_pcm):
        if os.path.exists(p):
            os.remove(p)
    return ok


async def speech_to_text(audio_bytes: bytes, filename: str = "audio.wav", language: str = "") -> str:
    """语音转文字。audio_bytes 为 wav/mp3 音频字节。

    language 留空由模型自动检测；指定如 "ja" 可强制按该语言识别
    （用于自动识别失败后按日语重试的场景）。
    """
    headers = {
        "Authorization": f"Bearer {_get_api_key()}",
    }
    files = {
        "file": (filename, audio_bytes, "audio/wav"),
        "model": (None, ASR_MODEL),
        "stream": (None, "false"),
    }
    if language:
        files["language"] = (None, language)
    async with _limit():
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(ASR_URL, headers=headers, files=files)
    if resp.status_code != 200:
        raise RuntimeError(f"ASR 失败 {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    return data.get("text", "") or ""
