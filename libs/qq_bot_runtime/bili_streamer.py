# -*- coding: utf-8 -*-
"""B站开播模块：让她自己成为主播（直播间的"身体"）。

职责（大脑与耳朵在 bilibili_plugin.py 的 streamer 模式里）：
  1. 开播/关播：调直播间 API（startLive/stopLive）拿 RTMP 推流地址，需登录 Cookie；
  2. 推流：ffmpeg 子进程——画面（静态立绘 / 窗口捕获 / 全屏）+ 音频（stdin PCM 管道）；
  3. 直播嗓音：speak()（文字→TTS）与 play_wav()（大脑已合成的语音直接播）都汇入
     音频管道；不说话时写静音，保证音轨连续；写管道按实时节奏
     （16k 单声道 s16，100ms 一块，绝对时钟计时防漂移）。

前置条件：账号已开通直播间（B 站直播权限 + 实名认证），config.py 填好
BILIBILI_SESSDATA / BILIBILI_CSRF / BILIBILI_ROOM_ID。
"""
import asyncio
import os
import queue
import subprocess
import threading
import time

import httpx

import numpy as np

import config
from bili_api import BiliSession, get_json, has_cookies, post_json

_PCM_RATE = 16000                                  # ffmpeg 模式音频管道格式：16k 单声道 s16le
_CHUNK_MS = 100
_CHUNK = _PCM_RATE * 2 * _CHUNK_MS // 1000         # 3200 字节 = 100ms 音频


def _push_mode() -> str:
    """推流模式：hime=直播姬管画面推流（机器人只出声音）；ffmpeg=自研推流。"""
    return str(getattr(config, "BILIBILI_PUSH_MODE", "ffmpeg") or "ffmpeg").lower()


def _find_cable_output() -> int:
    """按名字关键词找虚拟声卡的播放端点（VB-CABLE 装好后叫 CABLE Input）。"""
    want = str(getattr(config, "BILIBILI_CABLE_DEVICE", "CABLE Input") or "CABLE Input").lower()
    try:
        import sounddevice as sd
    except ImportError:
        raise RuntimeError("直播姬模式需要 sounddevice（pip install sounddevice）")
    for d in sd.query_devices():
        if d["max_output_channels"] > 0 and want in d["name"].lower():
            return int(d["index"])
    names = [d["name"] for d in sd.query_devices() if d["max_output_channels"] > 0]
    raise RuntimeError(
        f"直播姬模式没找到播放设备含「{config.BILIBILI_CABLE_DEVICE}」。\n"
        f"  VB-CABLE 装了吗（设备管理器/声音设置里应有 CABLE Input）？\n"
        f"  当前输出设备: {names[:8]}")


def _resolve_ffmpeg() -> str:
    """推流用的 ffmpeg：优先专用配置，其次 imageio-ffmpeg 自带的稳定版。

    注意：极新的 ffmpeg git 构建（如 2026-05-28 git，8.x 多线程调度器）对
    "实时慢喂的管道音频"会一直攒大缓冲不出流（直到 EOF），无法直播；
    稳定版发行构建（经典转码循环）没这个问题。所以推流优先用稳定版。
    """
    custom = str(getattr(config, "BILIBILI_FFMPEG_PATH", "") or "").strip()
    if custom and os.path.exists(custom):
        return custom
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        pass
    return getattr(config, "FFMPEG_PATH", "") or "ffmpeg"


class BiliStreamer:
    """开播管理 + ffmpeg 推流 + 直播音频管道。"""

    def __init__(self, room_id: int):
        self.room_id = int(room_id)
        self.session = BiliSession()
        self.live = False           # 直播进行中（ffmpeg 模式=已开播；hime 模式=声音通道就绪）
        self.pushing = False        # ffmpeg 模式=推流中；hime 模式=虚拟声卡就绪
        self.started_ts = 0.0
        self.title = ""
        self._ffmpeg = None
        self._ffmpeg_log = None
        self._audio_thread = None
        self._q = queue.Queue(maxsize=600)   # 60s 音频上限，满则丢弃（防内存膨胀）
        self._running = False
        self.last_write_ts = 0.0     # 最近一次向直播音频写入内容的时间（主播闲聊看门狗用）
        # hime 模式：虚拟声卡播放
        self._sd_stream = None      # 常驻播放流（打开即占住设备，避免每次开播合的爆音）
        self._sd_sr = 44100
        self._cable_index = None
        self._voice_lock = threading.Lock()  # 她一次说一句（多弹幕并发时不串音）

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    async def start(self):
        """开播准备。失败抛 RuntimeError（带人能看懂的原因）。

        hime 模式：只打开虚拟声卡，开播/画面/推流全由直播姬负责；
        ffmpeg 模式：调 startLive 拿推流地址并自己起 ffmpeg。
        """
        if _push_mode() == "hime":
            await self._start_hime()
            return
        if not has_cookies():
            raise RuntimeError("开播需要登录 Cookie：config.py 填 BILIBILI_SESSDATA / BILIBILI_CSRF")
        async with httpx.AsyncClient() as http:
            await self.session.ensure_buvid(http)
            rtmp_url = await self._open_live(http)
        self._begin_push(rtmp_url)
        self.live = True
        self.started_ts = time.time()
        print(f"[BILI-STREAM] 直播已开始并推流中（房间 {self.room_id}，"
              f"画面源: {str(getattr(config, 'BILIBILI_VIDEO_SOURCE', 'image'))}）")

    async def _start_hime(self):
        """hime 模式启动：打开到虚拟声卡的常驻播放流。

        sounddevice 的 write 自带实时节奏（写多快播多快，写满即阻塞），
        不说话时不写=静音，音轨天然连续。开播/关播/画面都在直播姬里操作。
        """
        import sounddevice as sd
        self._cable_index = _find_cable_output()
        info = sd.query_devices(self._cable_index)
        self._sd_sr = int(info["default_samplerate"] or 44100)
        self._sd_stream = sd.OutputStream(
            samplerate=self._sd_sr, channels=1, dtype="int16",
            device=self._cable_index, blocksize=0)
        self._sd_stream.start()
        self.pushing = True
        self.live = True            # 语义：直播进行中（推流本身由直播姬管理）
        self.started_ts = time.time()
        print(f"[BILI-STREAM] 直播姬模式就绪：她的声音 → {info['name'].strip()} "
              f"(index {self._cable_index}, {self._sd_sr}Hz)。"
              f"记得在直播姬里添加它为音频源，并点「开始推流」")

    async def stop(self):
        """停流并关播（幂等）。hime 模式只关自己的声音通道，不动直播姬的推流。"""
        self._running = False
        if self._ffmpeg:
            try:
                self._ffmpeg.stdin.close()
            except Exception:
                pass
            try:
                self._ffmpeg.terminate()
                self._ffmpeg.wait(timeout=8)
            except Exception:
                try:
                    self._ffmpeg.kill()
                except Exception:
                    pass
            self._ffmpeg = None
        if self._audio_thread:
            self._audio_thread.join(timeout=2)
            self._audio_thread = None
        if self._sd_stream:
            try:
                self._sd_stream.stop()
                self._sd_stream.close()
            except Exception:
                pass
            self._sd_stream = None
        if self._ffmpeg_log:
            try:
                self._ffmpeg_log.close()
            except Exception:
                pass
            self._ffmpeg_log = None
        hime = _push_mode() == "hime"
        self.pushing = False
        if self.live and not hime:
            try:
                async with httpx.AsyncClient() as http:
                    await post_json(http, self.session, "/room/v1/Room/stopLive",
                                    {"room_id": self.room_id, "platform": "pc_link"})
                print("[BILI-STREAM] 已关播")
            except Exception as e:
                print(f"[BILI-STREAM] 关播失败（可能已经关了）: {e}")
        self.live = False

    def status(self) -> dict:
        d = {
            "mode": _push_mode(),
            "live": self.live,
            "pushing": self.pushing,
            "uptime_seconds": int(time.time() - self.started_ts) if self.started_ts else 0,
            "title": self.title,
            "audio_queue_seconds": round(self._q.qsize() * _CHUNK_MS / 1000.0, 1),
        }
        if self._sd_stream is not None:
            d["cable_device"] = _device_name(self._cable_index)
        return d

    # ------------------------------------------------------------------
    # 开播 API：分区 → 标题 → startLive 拿推流地址
    # ------------------------------------------------------------------
    async def _open_live(self, http: httpx.AsyncClient) -> str:
        area_id = int(getattr(config, "BILIBILI_AREA_ID", 0) or 0)
        if not area_id:
            area_id = await self._pick_area(http)
        title = str(getattr(config, "BILIBILI_STREAM_TITLE", "") or "").strip()
        if title:
            try:
                await post_json(http, self.session, "/room/v1/Room/update",
                                {"room_id": self.room_id, "title": title,
                                 "platform": "pc_link", "area_v2": area_id})
                self.title = title
            except Exception as e:
                print(f"[BILI-STREAM] 设置标题/分区失败（继续开播）: {e}")
        d = await post_json(http, self.session, "/room/v1/Room/startLive",
                            {"room_id": self.room_id, "platform": "pc_link", "area_v2": area_id})
        rtmp = d.get("rtmp") or {}
        addr, code = str(rtmp.get("addr") or ""), str(rtmp.get("code") or "")
        if not addr or not code:
            raise RuntimeError(f"开播返回里没有推流地址（账号开通直播间了吗？）: {d}")
        print(f"[BILI-STREAM] 开播成功，已取得推流地址")
        return addr + code

    async def _pick_area(self, http: httpx.AsyncClient) -> int:
        """自动选直播分区：优先名字带「聊天」的二级分区，否则取第一个。"""
        try:
            d = await get_json(http, self.session, "/room/v1/Area/getList", {})
            groups = d if isinstance(d, list) else (d.get("data") or [])
            first = 0
            for grp in groups:
                for sub in (grp.get("list") or []):
                    sid = int(sub.get("id") or 0)
                    if not first:
                        first = sid
                    if "聊天" in str(sub.get("name") or ""):
                        return sid
            if first:
                return first
        except Exception as e:
            print(f"[BILI-STREAM] 获取分区列表失败: {e}")
        return 373   # 兜底（聊天室）

    # ------------------------------------------------------------------
    # 推流：ffmpeg（画面 + 音频管道）
    # ------------------------------------------------------------------
    def _begin_push(self, target: str):
        """启动 ffmpeg 与音频写线程。target 是 rtmp:// 推流地址（测试时可传文件路径）。"""
        w, h = _split_resolution(str(getattr(config, "BILIBILI_STREAM_RESOLUTION", "1280x720")))
        fps = max(5, int(getattr(config, "BILIBILI_STREAM_FPS", 15) or 15))
        ffmpeg = _resolve_ffmpeg()
        cmd = [
            ffmpeg, "-y", "-loglevel", "error",
            *self._video_input_args(),
            "-f", "s16le", "-ar", str(_PCM_RATE), "-ac", "1", "-i", "pipe:0",
            "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
            "-s", f"{w}x{h}", "-r", str(fps), "-g", str(fps * 2),
            "-b:v", f"{int(getattr(config, 'BILIBILI_STREAM_VIDEO_KBPS', 1500) or 1500)}k",
            "-c:a", "aac", "-b:a", "96k",
            "-f", "flv", target,
        ]
        log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bili_stream_ffmpeg.log")
        self._ffmpeg_log = open(log_path, "ab")
        self._ffmpeg = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                        stdout=subprocess.DEVNULL, stderr=self._ffmpeg_log)
        print(f"[BILI-STREAM] 推流 ffmpeg: {ffmpeg}")
        self._running = True
        self.pushing = True
        self._audio_thread = threading.Thread(target=self._audio_loop, daemon=True,
                                              name="bili-stream-audio")
        self._audio_thread.start()

    def _video_input_args(self) -> list:
        """画面源：window=按窗口标题捕获 / desktop=全屏 / 其余=静态立绘图。

        静态图加 -re（按帧率实时读帧）：不然画面时间戳以编码速度狂奔，
        与实时音频的交织复用会把缓冲撑爆；窗口/全屏捕获本身就是实时的。
        """
        fps = str(max(5, int(getattr(config, "BILIBILI_STREAM_FPS", 15) or 15)))
        source = str(getattr(config, "BILIBILI_VIDEO_SOURCE", "image") or "image").lower()
        if source == "window":
            title = self._resolve_window()
            return ["-f", "gdigrab", "-framerate", fps, "-i", f"title={title}"]
        if source == "desktop":
            return ["-f", "gdigrab", "-framerate", fps, "-i", "desktop"]
        img = str(getattr(config, "BILIBILI_STREAM_IMAGE", "") or "").strip()
        if not img or not os.path.exists(img):
            img = self._default_image()
        return ["-re", "-loop", "1", "-framerate", fps, "-i", img]

    def _resolve_window(self) -> str:
        """解析要捕获的游戏窗口，返回 gdigrab 需要的精确窗口标题。

        BILIBILI_WINDOW_TITLE 填了就按子串模糊匹配（不用手抄带版本号的
        完整标题）；留空则自动找标题含 "minecraft" 的窗口。多个命中取
        面积最大的。找不到抛异常并列出当前可见窗口，方便对照改名。
        """
        want = str(getattr(config, "BILIBILI_WINDOW_TITLE", "") or "").strip()
        try:
            import pygetwindow as gw
        except ImportError:
            if want:
                return want            # 没有 pygetwindow 只能按原样精确匹配
            raise RuntimeError("window 模式需要 pygetwindow（pip install pygetwindow）"
                               "或在 config 填 BILIBILI_WINDOW_TITLE 的精确标题")
        wins = [w for w in gw.getAllWindows()
                if w.title.strip() and w.width > 0 and w.height > 0]
        if not want:
            hits = [w for w in wins if "minecraft" in w.title.lower()]
        else:
            low = want.lower()
            hits = [w for w in wins if low in w.title.lower()]
        if not hits:
            titles = [w.title for w in wins][:10]
            raise RuntimeError(
                f"找不到{' Minecraft ' if not want else f'标题含「{want}」的'}窗口。\n"
                f"  游戏开了吗？当前可见窗口: {titles}\n"
                f"  提示：游戏窗口不能最小化，完整标题也可填进 BILIBILI_WINDOW_TITLE")
        best = max(hits, key=lambda w: w.width * w.height)
        print(f"[BILI-STREAM] 捕获窗口: {best.title!r} ({best.width}x{best.height})")
        return best.title

    def _default_image(self) -> str:
        """没配直播画面图时生成一张默认立绘底图（蓝白渐变 + 名字）。"""
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "data", "bili_stream_default.png")
        if os.path.exists(path):
            return path
        w, h = _split_resolution(str(getattr(config, "BILIBILI_STREAM_RESOLUTION", "1280x720")))
        try:
            from PIL import Image, ImageDraw, ImageFont
            img = Image.new("RGB", (w, h))
            top, bottom = (188, 224, 255), (24, 68, 130)   # 深蓝→浅蓝渐变（DeepSeek 娘配色）
            for y in range(h):
                t = y / max(h - 1, 1)
                img.paste(tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)),
                          (0, y, w, y + 1))
            draw = ImageDraw.Draw(img)
            font = None
            for fp in ("C:/Windows/Fonts/msyhbd.ttc", "C:/Windows/Fonts/msyh.ttc"):
                if os.path.exists(fp):
                    font = ImageFont.truetype(fp, max(36, h // 10))
                    break
            if font is None:
                font = ImageFont.load_default()
            text = str(getattr(config, "BILIBILI_STREAM_TITLE", "") or "小鱼直播中～来陪我聊天吧")
            draw.text((w // 2, h // 2 - 20), text, font=font, fill="white", anchor="mm")
            draw.text((w // 2, h // 2 + 40), "config.BILIBILI_STREAM_IMAGE 可换成自己的立绘",
                      font=font, fill=(220, 235, 255), anchor="mm")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            img.save(path)
        except Exception as e:
            raise RuntimeError(f"生成默认直播画面失败，请在 config 填 BILIBILI_STREAM_IMAGE: {e}")
        return path

    # ------------------------------------------------------------------
    # 直播嗓音：所有要说的内容都汇到音频管道
    # ------------------------------------------------------------------
    async def speak(self, text: str, caption: bool = True):
        """文字 → TTS → 直播音频（文字回复的兜底路径 / 主人点播）。

        caption=True 时同步推一句字幕（她说的话上屏），文字兜底/闲聊/点播都走这。
        """
        text = (text or "").strip()
        if not text:
            return
        if caption:
            try:
                import bili_captions
                bili_captions.push(text)
            except Exception:
                pass
        if not self.pushing:
            print(f"[BILI-STREAM] （未推流，仅日志）想说: {text[:40]}")
            return
        from chat_service import text_to_voice_wav
        wav_path = await text_to_voice_wav(text)
        await self.play_wav(wav_path)

    async def play_wav(self, wav_path: str):
        """把一段已合成的语音写进直播音频（大脑语音回复的主路径）。"""
        if not self.pushing:
            print(f"[BILI-STREAM] （未推流，仅日志）有语音待播: {os.path.basename(str(wav_path))}")
            return
        self.last_write_ts = time.time()
        if _push_mode() == "hime":
            # 虚拟声卡：转成声卡采样率后直接写流。stream.write 自带实时节奏
            # （播完才返回），加锁串行写保证一句说完再说下一句，多弹幕不串音。
            pcm = await asyncio.to_thread(self._wav_to_pcm, wav_path, self._sd_sr)
            def _play():
                with self._voice_lock:
                    try:
                        arr = np.frombuffer(pcm, dtype=np.int16)
                        tail = np.zeros(self._sd_sr // 10, dtype=np.int16)  # 0.1s 收尾静音防爆音
                        self._sd_stream.write(np.concatenate([arr, tail]))
                    except Exception as e:
                        print(f"[BILI-STREAM] 虚拟声卡写入失败（直播姬还在吗）: {e}")
            await asyncio.to_thread(_play)
            return
        pcm = await asyncio.to_thread(self._wav_to_pcm, wav_path)
        self._enqueue_pcm(pcm)

    def _wav_to_pcm(self, wav_path: str, rate: int = _PCM_RATE) -> bytes:
        """任意 wav → 单声道 s16 裸 PCM（ffmpeg 归一化到指定采样率）。

        顺带剪掉 GLM 云端 TTS 开头自带的滴滴提示音（对 VoxCPM 干净音频无影响）。
        """
        ffmpeg = _resolve_ffmpeg()
        r = subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-i", str(wav_path),
                            "-f", "s16le", "-ar", str(rate), "-ac", "1", "pipe:1"],
                           capture_output=True, timeout=180)
        if r.returncode != 0:
            raise RuntimeError(f"wav 转码失败: {(r.stderr or b'')[-200:]}")
        return _strip_lead_in_beep(r.stdout, rate)


def _strip_lead_in_beep(pcm: bytes, sr: int) -> bytes:
    """GLM 云端 TTS 返回的音频开头自带几声 1640Hz 滴滴提示音，直播里很出戏。

    从头扫描：找到连续 150ms 的宽频人声 onset，把之前的部分剪掉（留 50ms 余量）。
    本地 VoxCPM 的干净音频开头就是人声，onset 在 0，原样返回不受影响。
    只扫开头 3 秒，找不到 onset 就原样保留（防误剪）。
    """
    arr = np.frombuffer(pcm, dtype=np.int16).astype(np.float64) / 32768.0
    frame = int(sr * 0.05)
    if frame < 8 or len(arr) < frame * 4:
        return pcm
    win = np.hanning(frame)
    freqs = np.fft.rfftfreq(frame, 1 / sr)
    n = min(len(arr) // frame, int(3.0 / 0.05))
    onset = None
    run = 0
    for i in range(n):
        seg = arr[i * frame:(i + 1) * frame] * win
        rms = float(np.sqrt(np.mean(seg ** 2))) + 1e-12
        spec = np.abs(np.fft.rfft(seg))
        pi = int(np.argmax(spec))
        narrow = float(spec[pi]) / (float(spec.sum()) + 1e-12)
        if rms > 0.02 and narrow < 0.30:
            run += 1
            if run >= 3:                     # 连续 150ms 宽频 = 说话开始
                onset = max(0, (i - 2) * frame)
                break
        else:
            run = 0
    if not onset:
        return pcm
    trimmed = arr[max(0, onset - int(sr * 0.05)):].astype(np.int16)
    return trimmed.tobytes()


def _device_name(index: int) -> str:
    """按索引查输出设备名（查不到返回索引本身）。"""
    try:
        import sounddevice as sd
        return sd.query_devices(index)["name"].strip()
    except Exception:
        return str(index)

    def _enqueue_pcm(self, pcm: bytes):
        dropped = False
        for i in range(0, len(pcm), _CHUNK):
            try:
                self._q.put_nowait(pcm[i:i + _CHUNK])
            except queue.Full:
                dropped = True
                break
        if dropped:
            print("[BILI-STREAM] 音频队列已满（积压 60s+），丢弃本次语音尾部")

    def _audio_loop(self):
        """实时节奏写 PCM：有话说就写话音，没话说写静音；ffmpeg 挂了自动标记停流。"""
        silence = b"\x00" * _CHUNK
        next_t = time.time()
        while self._running and self._ffmpeg:
            try:
                chunk = self._q.get(timeout=0.05)
            except queue.Empty:
                chunk = None
            try:
                self._ffmpeg.stdin.write(chunk if chunk else silence)
            except Exception as e:
                self.pushing = False
                print(f"[BILI-STREAM] 推流音频管道断开（ffmpeg 退出？看 bili_stream_ffmpeg.log）: {e}")
                break
            next_t += _CHUNK_MS / 1000.0
            delay = next_t - time.time()
            if delay > 0:
                time.sleep(delay)
            else:
                next_t = time.time()   # 落后就重置基准，避免连续追赶


def _split_resolution(res: str) -> tuple:
    try:
        w, h = str(res).lower().split("x")
        return int(w), int(h)
    except Exception:
        return 1280, 720
