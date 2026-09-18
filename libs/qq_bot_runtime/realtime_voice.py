# -*- coding: utf-8 -*-
"""GLM-Realtime 实时语音引擎封装（智谱全双工 WebSocket）。

一条 WebSocket 完成 流式 ASR + LLM + TTS：
  上行：麦克风 PCM 帧 → input_audio_buffer.append（base64，100ms/帧）
  下行：response.audio.delta（24kHz PCM）→ 本机声卡即时播放

与 voice_room.py（会话状态机）解耦：引擎只负责「传音 + 事件回调」，
房间负责「何时开/关/打断」。将来换本地自组管线（ASR+LLM+TTS）时，
只需实现同款接口的另一个引擎类。

协议要点（2026 官方文档核实）：
  - 连接 wss://open.bigmodel.cn/api/paas/v4/realtime，Authorization: Bearer <api key>
  - session.update：model / input_audio_format=pcm16 / output_audio_format=pcm /
    turn_detection.type=server_vad(免提)|client_vad(PTT) / beta_fields.{chat_mode:audio,
    tts_source:e2e, auto_search}
  - 打断：free 模式收到 input_audio_buffer.speech_started 即停本地播放（服务器自行接管）；
    ptt 模式由房间调 cancel_response()（清播放 + response.cancel）。
  - 服务器事件 heartbeat 无需应答（websockets 层自带 ping/pong 保活）。
"""
import asyncio
import base64
import json
import queue
import collections
import threading
import uuid
import time
import time_context  # inject current time into LLM on send
import random
from typing import Optional

import numpy as np

import config
from quiet import degrade

# 引擎无音频设备时也允许 py_compile / 单元测试 —— sounddevice 在 start() 里懒加载
_SD = None


def _sd():
    """懒加载 sounddevice；未安装时给出安装提示。"""
    global _SD
    if _SD is None:
        try:
            import sounddevice
            _SD = sounddevice
        except ImportError:
            raise RuntimeError(
                "未安装 sounddevice，无法采集/播放音频。请执行："
                "venv\\Scripts\\pip install sounddevice")
    return _SD


class RealtimeSession:
    """一次 GLM-Realtime 会话：连接、音频上行/下行、打断。

    线程模型：
      - WebSocket 收发跑在 asyncio（_reader / _sender 任务）
      - 麦克风采集回调跑在 PortAudio 线程 → 帧经 loop.call_soon_threadsafe
        进 asyncio 出站队列（_sender 串行发送，保证帧序）
      - 播放：事件循环任务把 delta 塞进队列，声卡回调（PortAudio 线程）取数据出声；
        打断 = 清空播放队列即刻静音

    状态事件（on_state 回调，全部在事件循环线程执行）：
      "ready" 会话就绪 / "user_talking" 检测到用户开口 / "user_done" 用户说完
      "bot_talking" 开始回答 / "bot_done" 回答结束 / "stopped" 已停止
    文本回调：on_turn_end(text) 每段回答结束时给整段文字（转录，可落日志/记忆）。
    """

    # 采集回调把帧放进线程安全队列后，靠 call_soon_threadsafe 转进事件循环。
    # 上行每帧大小：采样率 * 0.1s * 2 字节（16bit 单声道）
    OUT_RATE = 24000          # GLM-Realtime 输出固定 24kHz 单声道 16bit PCM

    def __init__(self, instructions: str = "", mode: str = "free"):
        self.mode = mode if mode in ("free", "ptt") else "free"
        self.instructions = instructions or ""
        self.ws = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._in_stream = None      # 麦克风 InputStream
        self._out_stream = None     # 音箱 OutputStream
        self._input_rate = int(getattr(config, "REALTIME_INPUT_SAMPLE_RATE", 16000))
        self._send_q: asyncio.Queue = None      # 出站队列（帧/事件统一串行）
        self._sender_task = None
        self._reader_task = None
        self._capture_on = True                 # PTT 模式由房间开关
        self._closed = False

        # 播放缓冲：声卡回调从队列取，data 可为 bytearray 片段
        self._play_q = queue.Queue()
        self._play_buf = b""

        # 会话等待（session.updated 确认）
        self._updated_future: Optional[asyncio.Future] = None

        # 一段回答的转录累积
        self._turn_text = ""
        self._played_bytes = 0       # 本段已播音频字节（audio.done 去重用）

        # 房间注入的回调
        self.on_state = None        # async (state: str, info: str) -> None
        self.on_turn_end = None     # async (text: str) -> None（回答整段文字，可能为空）

    # ======================================================================
    # 生命周期
    # ======================================================================
    async def start(self):
        """建立 WebSocket 会话 + 音频流。任何失败抛 RuntimeError(中文提示)。"""
        if self._closed:
            raise RuntimeError("会话已关闭，请新建实例")
        self._loop = asyncio.get_running_loop()
        self._turn_text = ""

        # ---- 1. 连接 + 会话配置（reader 先启动，否则收不到 session.updated）----
        await self._connect_and_update()

        # ---- 2. 出站队列（采集回调依赖它入队，必须先建）----
        self._send_q = asyncio.Queue()
        self._sender_task = asyncio.create_task(self._sender())

        # ---- 3. 音频流（设备坏时在这里报错，随后补关清理）----
        try:
            self._open_audio_streams()
        except Exception:
            await self._close_streams()
            await self._teardown_tasks()
            await self._safe_close_ws()
            raise
        self._emit("ready", "")

    async def stop(self):
        """关闭会话：停采集/播放、断开 ws、清任务。"""
        if self._closed:
            return
        self._closed = True
        self._capture_on = False
        self._clear_playback()

        # 停声卡流（PortAudio close 可能短暂阻塞，丢线程池）
        await self._close_streams()

        # 停发送/读取任务
        await self._teardown_tasks()
        await self._safe_close_ws()
        self._emit("stopped", "")

    async def _close_streams(self):
        for name in ("_in_stream", "_out_stream"):
            st = getattr(self, name)
            if st is not None:
                try:
                    await asyncio.to_thread(st.stop)
                    await asyncio.to_thread(st.close)
                except Exception as e:
                    degrade("realtime_voice.RealtimeSession._close_streams", e, "关音频流失败")
                setattr(self, name, None)

    async def _teardown_tasks(self):
        """取消发送/读取任务（可重复调用）。"""
        for name in ("_sender_task", "_reader_task"):
            task = getattr(self, name)
            if task is not None:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception) as e:
                    degrade("libs/qq_bot_runtime/realtime_voice.py:167 RealtimeSession._teardown_tasks", e, "降级：await task")
                setattr(self, name, None)

    async def _safe_close_ws(self):
        ws, self.ws = self.ws, None
        if ws is not None:
            try:
                await ws.close()
            except Exception as e:
                degrade("realtime_voice.RealtimeSession._safe_close_ws", e, "关实时 WS 失败")

    # ======================================================================
    # WebSocket 协议
    # ======================================================================
    async def _connect_and_update(self):
        try:
            import websockets
        except ImportError:
            raise RuntimeError("未安装 websockets 库")
        url = getattr(config, "GLM_REALTIME_WS_URL", "") or \
            "wss://open.bigmodel.cn/api/paas/v4/realtime"
        api_key = getattr(config, "GLM_REALTIME_API_KEY", "") or config.GLM_API_KEY
        if not api_key:
            raise RuntimeError("缺少 GLM API Key（config.GLM_API_KEY 或 GLM_REALTIME_API_KEY）")
        try:
            # websockets>=15 用 additional_headers 传鉴权头
            self.ws = await asyncio.wait_for(
                websockets.connect(
                    url, additional_headers={"Authorization": f"Bearer {api_key}"},
                    ping_interval=20, max_queue=256),
                timeout=15)
        except asyncio.TimeoutError:
            raise RuntimeError(f"连接实时语音服务器超时（{url}）")
        except Exception as e:
            raise RuntimeError(self._conn_error_hint(e, url))

        # 读取任务先启动：session.update 的确认/错误全靠它收
        self._reader_task = asyncio.create_task(self._reader())

        # ---- session.update（必须等服务端 session.updated 确认后才推音频）----
        payload = {
            "type": "session.update",
            "event_id": uuid.uuid4().hex,
            "session": {
                "model": getattr(config, "GLM_REALTIME_MODEL", "glm-realtime-flash"),
                "modalities": ["audio", "text"],
                "instructions": self.instructions,
                "input_audio_format": "pcm16",
                "output_audio_format": "pcm",
                "voice": getattr(config, "GLM_REALTIME_VOICE", "tongtong"),
                "temperature": 0.8,
                "turn_detection": {"type": "server_vad" if self.mode == "free" else "client_vad"},
                "beta_fields": {
                    "chat_mode": "audio",
                    "tts_source": "e2e",
                    "auto_search": bool(getattr(config, "REALTIME_AUTO_SEARCH", False)),
                },
            },
        }
        self._updated_future = self._loop.create_future()
        try:
            await self.ws.send(json.dumps(payload, ensure_ascii=False))
            try:
                await asyncio.wait_for(self._updated_future, timeout=15)
            except asyncio.TimeoutError:
                raise RuntimeError("实时语音会话配置超时（未收到 session.updated）")
            except Exception as e:
                raise RuntimeError(f"实时语音会话配置失败: {e}")
        except Exception:
            # 配置失败：停掉 reader、关 ws，避免残留任务与半开连接
            await self._teardown_tasks()
            await self._safe_close_ws()
            raise

    def _conn_error_hint(self, e: Exception, url: str) -> str:
        text = str(e)
        if "401" in text or "403" in text or "unauthorized" in text.lower() or "forbidden" in text.lower():
            return (
                "实时语音鉴权失败(401/403)：当前 GLM key 无法访问该端点。"
                "请确认 key 有 GLM-Realtime 权限，或换端点：把 config.GLM_REALTIME_WS_URL "
                "改为 wss://api.z.ai/api/paas/v4/realtime（若 key 属于 Z.AI 国际站）")
        return f"实时语音连接失败: {text[:200]}"

    # ======================================================================
    # 事件读取与分发
    # ======================================================================
    async def _reader(self):
        try:
            async for raw in self.ws:
                if isinstance(raw, bytes):
                    continue
                try:
                    evt = json.loads(raw)
                except json.JSONDecodeError as e:
                    degrade("libs/qq_bot_runtime/realtime_voice.py:261 RealtimeSession._reader", e, "降级：evt = json.loads(raw)")
                    continue
                try:
                    await self._dispatch(evt)
                except Exception as e:
                    print(f"[REALTIME] 事件处理异常 {evt.get('type')}: {e}")
        except Exception as e:
            # 主动 stop() 关闭时忽略；其余视为断线
            if not self._closed:
                print(f"[REALTIME] 连接断开: {e}")
                self._emit("stopped", f"连接断开: {str(e)[:120]}")

    async def _dispatch(self, evt: dict):
        t = evt.get("type", "")
        if t == "session.updated":
            fut = self._updated_future
            if fut and not fut.done():
                fut.set_result(True)
        elif t == "error":
            err = evt.get("error") or evt.get("error_info") or {}
            if isinstance(err, dict):
                msg = err.get("message") or err.get("type") or json.dumps(err, ensure_ascii=False)
            else:
                msg = str(err)
            fut = self._updated_future
            if fut and not fut.done():
                fut.set_exception(RuntimeError(f"会话错误: {msg[:200]}"))
            else:
                print(f"[REALTIME] 服务端错误: {msg}")
        elif t == "response.audio.delta":
            data = evt.get("delta", "")
            if data:
                try:
                    pcm = base64.b64decode(data)
                except Exception:
                    return
                if pcm:
                    self._play_q.put(pcm)
                    self._played_bytes += len(pcm)
        elif t == "response.audio_transcript.delta":
            self._accumulate_transcript(evt.get("delta", ""))
        elif t == "response.text.delta":
            self._accumulate_transcript(evt.get("delta", ""))
        elif t == "response.created":
            self._turn_text = ""
            self._played_bytes = 0
            self._emit("bot_talking", "")
        elif t == "response.done":
            text = self._turn_text.strip()
            self._emit("bot_done", "")
            if text and self.on_turn_end:
                try:
                    await self.on_turn_end(text)
                except Exception as e:
                    print(f"[REALTIME] on_turn_end 异常: {e}")
        elif t == "response.text.done":
            # 实测该服务端不发 .delta，内容在 .done 终态事件里
            self._merge_final_text(evt.get("text") or evt.get("delta") or "")
        elif t == "response.audio_transcript.done":
            self._merge_final_text(evt.get("transcript") or evt.get("text")
                                   or evt.get("delta") or "")
        elif t == "response.audio.done":
            # 若增量模式已播过音频则跳过，避免重复出声
            data = evt.get("audio") or evt.get("delta") or ""
            if data and self._played_bytes == 0:
                try:
                    pcm = base64.b64decode(data)
                except Exception:
                    pcm = b""
                if pcm:
                    self._play_q.put(pcm)
        elif t == "input_audio_buffer.speech_started":
            # 用户开口 → 立刻停本地播放（打断）；free 模式服务器会自行取消当前回复
            self._clear_playback()
            self._emit("user_talking", "")
        elif t == "input_audio_buffer.speech_stopped":
            self._emit("user_done", "")
        elif t in ("heartbeat", "session.created", "input_audio_buffer.committed",
                   "conversation.created", "rate_limits.updated",
                   "response.output_item.added", "response.output_item.done",
                   "response.content_part.added", "response.content_part.done"):
            pass  # 无需处理
        else:
            print(f"[REALTIME] 未识别事件: {t}")

    def _accumulate_transcript(self, delta: str):
        if delta:
            self._turn_text += delta

    def _merge_final_text(self, final: str):
        """合并 .done 终态事件携带的整段文本。

        该服务端实测只发 .done 不带 .delta；若将来切回 .delta 模式，
        本方法对已累积的文本做后缀去重，避免转录重复。
        """
        final = (final or "").strip()
        if not final:
            return
        if final in self._turn_text:
            return
        if not self._turn_text:
            self._turn_text = final
        elif not self._turn_text.endswith(final):
            self._turn_text += final

    # ======================================================================
    # 音频：采集 → 上行（出站队列统一串行）
    # ======================================================================
    def _open_audio_streams(self):
        sd = _sd()
        dev_in = getattr(config, "REALTIME_INPUT_DEVICE", "") or None
        dev_out = getattr(config, "REALTIME_OUTPUT_DEVICE", "") or None
        try:
            # 输入：麦克风 PCM16；回调线程把帧转交事件循环（100ms/帧）
            self._in_stream = sd.InputStream(
                device=dev_in, samplerate=self._input_rate, channels=1,
                dtype="int16", blocksize=max(160, int(self._input_rate * 0.1)),
                callback=self._on_audio_in)
            # 输出：24kHz 播放（GLM-Realtime 输出格式固定），40ms 一块
            self._out_stream = sd.OutputStream(
                device=dev_out, samplerate=self.OUT_RATE, channels=1,
                dtype="int16", blocksize=960,
                callback=self._on_audio_out)
        except Exception as e:
            raise RuntimeError(f"音频设备打开失败: {e}")
        # 显式启动（auto_start 不可靠，实测打开后 active=False 不采音）
        self._in_stream.start()
        self._out_stream.start()

    def _on_audio_in(self, indata, frames, time_info, status):
        if not self._capture_on:
            return
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        data = indata.tobytes()
        loop.call_soon_threadsafe(self._queue_audio_outbound, data)

    def _queue_audio_outbound(self, data: bytes):
        """事件循环线程内被调：帧进统一出站队列，由 _sender 串行发送。"""
        if self._closed or self._send_q is None:
            return
        try:
            self._send_q.put_nowait(("audio", data))
        except asyncio.QueueFull as e:
            degrade("libs/qq_bot_runtime/realtime_voice.py:406 RealtimeSession._queue_audio_outbound", e, "降级：self._send_q.put_nowait(('audio', data))")

    def set_capture(self, on: bool):
        """开关上行采集（PTT 模式按键用）。线程安全。"""
        self._capture_on = on

    async def _sender(self):
        """唯一写 ws 的任务：音频帧 base64 + 控制事件，保证顺序不交错。"""
        while not self._closed:
            try:
                kind, obj = await self._send_q.get()
            except asyncio.CancelledError:
                break
            if kind == "audio":
                payload = {
                    "type": "input_audio_buffer.append",
                    "event_id": uuid.uuid4().hex,
                    "audio": base64.b64encode(obj).decode("ascii"),
                }
            else:
                payload = obj
            if self.ws is None:
                continue
            try:
                await self.ws.send(json.dumps(payload, ensure_ascii=False))
            except Exception as e:
                if not self._closed:
                    print(f"[REALTIME] 上行发送失败: {e}")
                break

    async def _queue_event(self, evt_type: str, **extra):
        """控制事件（commit/clear/cancel/create）进统一出站队列。"""
        if self._send_q is None or self._closed:
            return
        payload = {"type": evt_type, "event_id": uuid.uuid4().hex}
        payload.update(extra)
        await self._send_q.put(("evt", payload))

    # PTT 组：说完一段 → 提交并请求回答
    async def commit_turn(self):
        self.set_capture(False)
        await self._queue_event("input_audio_buffer.commit")
        await self._queue_event("response.create")

    async def cancel_response(self):
        """打断当前回答：本地立刻静音 + 通知服务器取消。"""
        self._clear_playback()
        await self._queue_event("response.cancel")

    # ======================================================================
    # 播放
    # ======================================================================
    def _clear_playback(self):
        """清空待播音频（打断用），声卡回调随即输出静音。"""
        while True:
            try:
                self._play_q.get_nowait()
            except queue.Empty:
                break
        self._play_buf = b""

    def _on_audio_out(self, outdata, frames, time_info, status):
        """声卡回调：从播放队列取 PCM，不足补静音；绝不阻塞。"""
        want = outdata.nbytes
        buf = self._play_buf
        while len(buf) < want:
            try:
                buf += self._play_q.get_nowait()
            except queue.Empty:
                break
        if buf:
            fill = buf[:want]
            if len(fill) < want:
                fill += b"\x00" * (want - len(fill))
            # bytes 直接赋给 int16 数组会触发逐元素解析报错；outdata 形状 (frames, 声道)
            outdata[:] = np.frombuffer(fill, dtype=np.int16).reshape(outdata.shape)
            self._play_buf = buf[want:]
        else:
            outdata.fill(0)

    def _emit(self, state: str, info: str):
        """触发房间回调。on_state 可以是同步函数或 async；async 的以任务调度，
        不阻塞 _reader 继续收事件。"""
        if not self.on_state:
            return
        try:
            result = self.on_state(state, info)
            if asyncio.iscoroutine(result):
                loop = self._loop
                if loop and not loop.is_closed():
                    asyncio.ensure_future(result)
        except Exception as e:
            print(f"[REALTIME] on_state 异常: {e}")


# ===========================================================================
# 本地全自组实时语音引擎
#   麦克风 → VAD → 云端 ASR → 云端 LLM(流式) → voxcpm2 流式 TTS → 音箱
# 声线 = config.VOXCPM_VOICE_DESC（voice-design 零样本，与 QQ 离线语音一致）
# 与 RealtimeSession 同接口（on_state/on_turn_end/start/stop/set_capture/
# commit_turn/cancel_response），voice_room 按 config.REALTIME_ENGINE 切换。
# ===========================================================================
import re as _re

_SENT_RE = _re.compile(r"[。！？!?；;\n]")
_PRE_MAX_BLOCKS = 8          # 录制预备缓冲（约 0.5s @16k/1024）
_SIL_END_BLOCKS = 6          # 连续静音块数判定句尾（约 0.4s）
_SPEECH_START_BLOCKS = 2     # 连续语音块数判定句首
_MAX_REC_SEC = 20            # 单次录音最大时长


def _to_wav(audio_int16, sr):
    """手写 16bit PCM WAV 头，避免引入 soundfile 依赖。"""
    import struct as _st
    a = np.asarray(audio_int16, dtype=np.int16)
    data = a.tobytes()
    n = len(data)
    buf = bytearray()
    buf += b"RIFF"
    buf += _st.pack("<I", 36 + n)
    buf += b"WAVE"
    buf += b"fmt "
    buf += _st.pack("<IHHIIHH", 16, 1, 1, sr, sr * 2, 2, 16)
    buf += b"data"
    buf += _st.pack("<I", n)
    buf += data
    return bytes(buf)


def _chat_provider_cfg():
    """取 chat 能力路由里首个有 key 的供应商配置（与 bot 同一套聊天配置）。"""
    routing = getattr(config, "AI_CAPABILITY_ROUTING", {}) or {}
    chain = routing.get("chat") or ["deepseek"]
    providers = getattr(config, "AI_PROVIDERS", {}) or {}
    for name in chain:
        p = providers.get(name)
        if p and p.get("api_key"):
            return p
    for p in providers.values():
        if p.get("api_key"):
            return p
    return {}


def _split_index(buf):
    m = _SENT_RE.search(buf)
    return m.end() if m else -1


def _schedule(loop, coro):
    loop.call_soon_threadsafe(lambda: asyncio.ensure_future(coro, loop=loop))


class _StreamResampler:
    """状态化线性插值重采样器（int16 PCM）。

    把 vox 固定的 48k 输出重采样到声卡设备原生率（如 44100）。用浮点相位
    累加保持跨 chunk 连续，避免边界处咔哒声。TTS 输出已带限，线性插值足够。
    """

    def __init__(self, in_sr, out_sr):
        self.in_sr = in_sr
        self.out_sr = out_sr
        self.ratio = out_sr / in_sr          # 输出/输入 采样率比
        self._pos = 0.0
        self._last = 0.0

    def process(self, x: "np.ndarray") -> "np.ndarray":
        if x.size == 0:
            return x
        xf = x.astype(np.float32)
        n = xf.size
        out = np.empty(n + 4, dtype=np.float32)
        o = 0
        while True:
            idx = int(self._pos)
            if idx >= n:
                break
            frac = self._pos - idx
            cur = xf[idx]
            nxt = xf[idx + 1] if idx + 1 < n else self._last
            out[o] = cur * (1.0 - frac) + nxt * frac
            o += 1
            self._pos += self.ratio
        self._pos -= n
        self._last = xf[-1]
        return out[:o]


def _resolve_output_device(name):
    """把配置里的设备名解析成 sounddevice 整数索引（避免中文名编码/匹配问题）。

    用 ASCII 子串 'LE202' 在所有设备中查找带输出通道的耳机设备，优先 WASAPI。
    返回整数索引或 None（=系统默认输出）。配置文件里的具体中文名只作存在性判断。
    """
    if not name:
        return None
    try:
        sd = _sd()
        best = None
        for i, d in enumerate(sd.query_devices()):
            if "LE202" in d["name"] and d["max_output_channels"] > 0:
                best = i
                if d["hostapi"] == 0:   # WASAPI 优先（最低延迟、原生率）
                    return i
        return best
    except Exception:
        return None


def _device_output_sr(dev):
    """查询输出设备原生采样率（WASAPI 共享模式只接受此率）。dev 可为整数或 None。"""
    try:
        sd = _sd()
        try:
            info = sd.query_devices(dev, kind="output") if dev is not None \
                else sd.query_devices(kind="output")
        except Exception:
            info = sd.query_devices(kind="output")
        return int(info["default_samplerate"])
    except Exception:
        return None


class _PCMPlayer:
    """单声道 int16 PCM 播放器：边收边播，绝不阻塞音频回调。"""

    def __init__(self, sr, device=None):
        self.sr = sr
        self._buf = bytearray()
        self._lock = threading.Lock()
        # 预缓冲：攒够 ~300ms 才开始放音，吸收 TTS 流式网络/合成抖动，
        # 避免声卡「拉」模型在首包或间隙处 underflow 出现咔咔卡顿。
        self._started = False
        self._pre_bytes = int(sr * 2 * 0.30)
        self._max_bytes = int(sr * 2 * 3.0)   # 缓冲上限 3s，防 TTS 推得过快堆积爆内存
        sd = _sd()
        self.stream = sd.OutputStream(samplerate=sr, channels=1, dtype="int16",
                                      blocksize=960, latency="low",
                                      device=device, callback=self._cb)
        self.stream.start()

    def _cb(self, outdata, frames, time_info, status):
        need = frames * 2
        with self._lock:
            have = len(self._buf)
            if not self._started:
                if have >= self._pre_bytes:
                    self._started = True
                else:
                    outdata.fill(0)   # 预缓冲未达标：静音等待，暂不消费
                    return
            if have >= need:
                data = bytes(self._buf[:need])
                del self._buf[:need]
            else:
                # 正常播放中偶发欠载：补静音（有预缓冲后极少见）
                data = bytes(self._buf) + b"\x00" * (need - have)
                self._buf.clear()
        outdata[:] = np.frombuffer(data, dtype=np.int16).reshape(outdata.shape)

    def write(self, pcm_bytes):
        with self._lock:
            self._buf.extend(pcm_bytes)

    def clear(self):
        with self._lock:
            self._buf.clear()

    def stop(self):
        try:
            self.stream.stop()
        except Exception as e:
            degrade("realtime_voice._PCMPlayer.stop", e, "停 PCM 播放失败")
        try:
            self.stream.close()
        except Exception as e:
            degrade("realtime_voice._PCMPlayer.stop", e, "关 PCM 流失败")


class LocalRealtimeSession:
    def __init__(self, instructions="", system_messages=None, mode="free", voice_desc="", tts_base=""):
        self.mode = mode if mode in ("free", "ptt") else "free"
        self.instructions = instructions or ""
        # 完整 system：人设基座 + 记忆上下文（多 system 消息，结构更清晰）。
        # 为空时回退到单条 instructions。
        self.system_messages = list(system_messages) if system_messages else []
        self.voice_desc = voice_desc or getattr(config, "VOXCPM_VOICE_DESC", "")
        self.tts_base = (tts_base or getattr(config, "LOCAL_TTS_BASE", "")
                         or "http://127.0.0.1:8765").rstrip("/")
        self.on_state = None
        self.on_turn_end = None

        self._loop = None
        self._closed = False
        self._capture_on = (self.mode == "free")
        self._in_stream = None
        self._player = None
        self._input_rate = int(getattr(config, "REALTIME_INPUT_SAMPLE_RATE", 16000))

        self._mic_buf = []
        self._pre_buf = collections.deque(maxlen=_PRE_MAX_BLOCKS)
        self._recording = False
        self._sil_frames = 0
        self._speech_frames = 0
        self._rms_thr = float(getattr(config, "REALTIME_VAD_THRESHOLD", 0.012))

        self._cancel = False
        self._tts_resp = None
        self._llm_resp = None
        self._tts_active = False

        self._history = []
        self._max_hist = int(getattr(config, "REALTIME_HISTORY_TURNS", 6)) * 2
        self._turn_text = ""

        # 主动监听/开口相关状态
        self._last_user_speech = 0.0      # 上次用户说话（含说完）时间
        self._last_proactive_at = 0.0     # 上次主动开口时间
        self._last_app = ""               # 上次观察到的前台应用（用于检测切换）
        self._proactive_task = None

    # ---- 生命周期 ----
    async def start(self):
        if self._closed:
            raise RuntimeError("会话已关闭，请新建实例")
        self._loop = asyncio.get_running_loop()
        sd = _sd()
        dev = getattr(config, "REALTIME_INPUT_DEVICE", "") or None
        self._in_stream = sd.InputStream(
            device=dev, samplerate=self._input_rate, channels=1, dtype="int16",
            blocksize=1024, callback=self._mic_cb)
        self._in_stream.start()
        self._emit("ready", "")
        # 预热：触发服务端首次推理（CUDA/torch 编译开销只发生一次），
        # 避免用户第一句话的回复出现十几秒冷启动延迟
        asyncio.ensure_future(self._warmup())
        # 主动监听：空闲时留意你当前窗口/应用，主动找你说话
        if getattr(config, "ENABLE_REALTIME_PROACTIVE", False):
            try:
                self._last_app = (self._get_active_context() or {}).get("app", "")
            except Exception as e:
                degrade("libs/qq_bot_runtime/realtime_voice.py:747 LocalRealtimeSession.start", e, "降级：self._last_app = (self._get_active_context() or {}")
            self._proactive_task = asyncio.ensure_future(self._proactive_watch())

    async def stop(self):
        if self._closed:
            return
        self._closed = True
        self._capture_on = False
        self._clear_playback()
        if self._in_stream is not None:
            try:
                await asyncio.to_thread(self._in_stream.stop)
            except Exception as e:
                degrade("realtime_voice.LocalRealtimeSession.stop", e, "停输入流失败")
            try:
                self._in_stream.close()
            except Exception as e:
                degrade("realtime_voice.LocalRealtimeSession.stop", e, "关输入流失败")
            self._in_stream = None
        if self._proactive_task is not None:
            self._proactive_task.cancel()
            self._proactive_task = None
        if self._player is not None:
            try:
                self._player.stop()
            except Exception as e:
                degrade("realtime_voice.LocalRealtimeSession.stop", e, "停播放器失败")
            self._player = None
        self._emit("stopped", "")

    # ---- 麦克风回调（PortAudio 线程）----
    def _mic_cb(self, indata, frames, time_info, status):
        if not self._capture_on:
            return
        block = np.frombuffer(indata, dtype=np.int16).copy()
        self._pre_buf.append(block)
        if self._recording:
            self._mic_buf.append(block)
        if self.mode == "free":
            rms = float(np.sqrt(np.mean((block.astype(np.float32) / 32768.0) ** 2)))
            self._vad(rms)

    def _vad(self, rms):
        if rms > self._rms_thr:
            self._sil_frames = 0
            self._speech_frames += 1
            if not self._recording and self._speech_frames >= _SPEECH_START_BLOCKS:
                self._recording = True
                self._mic_buf = list(self._pre_buf)
            elif self._recording and self._tts_active:
                _schedule(self._loop, self._interrupt())   # 她说话时主人又开口 → 打断
        else:
            self._speech_frames = 0
            self._sil_frames += 1
            if self._recording and self._sil_frames >= _SIL_END_BLOCKS:
                self._finalize_phrase()
        if self._recording:
            total = sum(len(b) for b in self._mic_buf)
            if total > self._input_rate * _MAX_REC_SEC:
                self._finalize_phrase()

    def _finalize_phrase(self):
        self._recording = False
        audio = np.concatenate(self._mic_buf) if self._mic_buf else None
        self._mic_buf = []
        if audio is not None and len(audio) > 0:
            _schedule(self._loop, self._on_phrase(audio))

    # ---- 说话模式控制（PTT）----
    def set_capture(self, on):
        self._capture_on = on
        if self.mode == "ptt":
            if on:
                self._mic_buf = list(self._pre_buf)
                self._recording = True
            elif self._recording:
                self._finalize_phrase()

    async def commit_turn(self):
        self.set_capture(False)

    async def cancel_response(self):
        if self._loop is not None:
            _schedule(self._loop, self._interrupt())
        self._recording = False
        self._mic_buf = []

    # ---- 管线 ----
    async def _on_phrase(self, audio):
        if self._closed:
            return
        self._emit("user_talking", "")
        wav = _to_wav(audio, self._input_rate)
        try:
            import voice_client
            text = await voice_client.speech_to_text(wav, "voice.wav")
        except Exception as e:
            print(f"[LOCAL-RT] ASR 失败: {e}")
            if not self._closed:
                self._emit("ready", "")
            return
        text = (text or "").strip()
        if not text:
            if not self._closed:
                self._emit("ready", "")
            return
        self._emit("user_done", text)
        self._last_user_speech = time.time()
        if self.on_turn_end:
            try:
                r = self.on_turn_end(text)
                if asyncio.iscoroutine(r):
                    asyncio.ensure_future(r)
            except Exception as e:
                print(f"[LOCAL-RT] on_turn_end 异常: {e}")
        await self._respond(text)

    async def _respond(self, text):
        self._history.append({"role": "user", "content": text})
        self._cancel = False
        self._emit("bot_talking", "")
        full = ""
        q = asyncio.Queue()

        async def _feed():
            """LLM 流式输出按句拆包进队列，与 TTS 播放并行（重叠管线）。"""
            buf = ""
            try:
                async for delta in self._llm_stream(text):
                    if self._cancel:
                        break
                    buf += delta
                    while True:
                        idx = _split_index(buf)
                        if idx <= 0:
                            break
                        sentence = buf[:idx]
                        buf = buf[idx:]
                        await q.put(sentence)
                if buf.strip() and not self._cancel:
                    await q.put(buf)
            except Exception as e:
                print(f"[LOCAL-RT] LLM 流式失败: {e}")
            finally:
                await q.put(None)

        feed_task = asyncio.ensure_future(_feed())

        sent = 0
        try:
            while True:
                sentence = await q.get()
                if sentence is None:
                    break
                if self._cancel:
                    break
                full += sentence
                # vox 已优化(首包~0.4s)：逐句送 TTS，首句到手即出声，
                # 播放当前句时 LLM 在后台继续生成下一句，避免"等整段 LLM
                # 完成才出声"的数秒延迟。每句一次 vox 请求、串行播放不叠音；
                # 超长回复按 1000 字截断。
                if sent >= 1000:
                    continue
                await self._speak(sentence)
                sent += len(sentence)
        finally:
            self._cancel = True
            try:
                await asyncio.wait_for(feed_task, timeout=2.0)
            except Exception:
                feed_task.cancel()
        if full.strip():
            self._history.append({"role": "assistant", "content": full})
            if len(self._history) > self._max_hist:
                self._history = self._history[-self._max_hist:]
        if not self._closed:
            self._emit("bot_done", full)
        if not self._closed:
            self._emit("ready", "")

    async def _speak(self, sentence):
        if self._cancel or not sentence.strip():
            return
        self._tts_active = True
        try:
            await self._tts_stream(sentence)
        except Exception as e:
            print(f"[LOCAL-RT] TTS 流式失败: {e}")
        finally:
            self._tts_active = False

    async def _tts_stream(self, text):
        import httpx
        payload = {"text": text, "voice_desc": self.voice_desc,
                   "seed": None, "speed": 1.0}
        async with httpx.AsyncClient(timeout=300.0) as client:
            async with client.stream(
                    "POST", f"{self.tts_base}/tts/stream", json=payload) as resp:
                self._tts_resp = resp
                sr = int(resp.headers.get("X-Sample-Rate", 48000))
                self._ensure_player(sr)
                async for chunk in resp.aiter_bytes(chunk_size=4096):
                    if self._cancel:
                        break
                    if chunk:
                        self._play_pcm(chunk)
                self._tts_resp = None

    async def _warmup(self):
        """后台预热 TTS 服务（丢弃音频），吸收首次推理的编译开销。"""
        import httpx
        try:
            async with httpx.AsyncClient(timeout=300.0) as client:
                async with client.stream(
                        "POST", f"{self.tts_base}/tts/stream",
                        json={"text": "预热", "voice_desc": self.voice_desc,
                              "seed": None, "speed": 1.0}) as resp:
                    async for _ in resp.aiter_bytes(chunk_size=65536):
                        pass
        except Exception as e:
            print(f"[LOCAL-RT] 预热失败(可忽略): {e}")

    async def _llm_stream(self, text):
        # 组装消息：完整 system(人设+记忆) + 滚动历史 + 本轮用户句
        if self.system_messages:
            messages = list(self.system_messages)
        else:
            messages = [{"role": "system", "content": self.instructions}]
        if self._history:
            messages += self._history[-self._max_hist:]
        messages.append({"role": "user", "content": text})
        async for delta in self._llm_stream_raw(messages):
            yield delta

    async def _llm_stream_raw(self, messages):
        """给定完整 messages 列表做流式补全（供 _respond / 主动开口复用）。"""
        import httpx
        cfg = _chat_provider_cfg()
        if not cfg:
            yield "（未配置聊天模型）"
            return
        base = (cfg.get("base_url") or "https://api.deepseek.com").rstrip("/")
        url = base + "/chat/completions"
        headers = {"Authorization": f"Bearer {cfg.get('api_key', '')}",
                   "Content-Type": "application/json"}
        messages = time_context.inject_time(messages)  # inject current time before sending
        payload = {"model": cfg.get("default_model"), "messages": messages,
                   "stream": True, "temperature": 0.9}
        async with httpx.AsyncClient(timeout=60.0) as client:
            async with client.stream(
                    "POST", url, json=payload, headers=headers) as resp:
                self._llm_resp = resp
                async for line in resp.aiter_lines():
                    if self._cancel:
                        break
                    line = line.strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        obj = json.loads(data)
                    except Exception as e:
                        degrade("libs/qq_bot_runtime/realtime_voice.py:1011 LocalRealtimeSession._llm_stream_raw", e, "降级：obj = json.loads(data)")
                        continue
                    delta = obj.get("choices", [{}])[0].get("delta", {}).get("content")
                    if delta:
                        yield delta
                self._llm_resp = None

    # ---- 主动监听 + 主动开口 ----
    def _get_active_context(self):
        """取前台窗口标题 + 进程名（浏览器窗口标题即当前页面名）。

        用 ctypes 直接调 Win32 API，避免依赖 win32gui/pywin32；psutil 取进程名。
        """
        try:
            import ctypes
            import psutil
            user32 = ctypes.windll.user32
            hwnd = user32.GetForegroundWindow()
            if not hwnd:
                return None
            length = user32.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            title = (buf.value or "").strip()
            pid = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            app = ""
            try:
                app = psutil.Process(pid.value).name()
            except Exception as e:
                degrade("libs/qq_bot_runtime/realtime_voice.py:1042 LocalRealtimeSession._get_active_context", e, "降级：app = psutil.Process(pid.value).name()")
            # 浏览器窗口标题去掉 " - Google Chrome" 等后缀，露出页面名
            for suf in (" - Google Chrome", " - Microsoft Edge", " - Firefox",
                        " - Brave", " — Mozilla Firefox"):
                if title.endswith(suf):
                    title = title[: -len(suf)].strip()
                    break
            return {"app": app, "title": title}
        except Exception as e:
            print(f"[LOCAL-RT] 获取活动窗口失败: {e}")
            return None

    async def _proactive_say(self, ctx):
        """根据当前窗口/应用上下文，生成一句自然闲聊并主动说出口。"""
        app = (ctx or {}).get("app", "") or "某个程序"
        title = (ctx or {}).get("title", "") or ""
        where = f"应用「{app}」，窗口「{title}」" if title and title != app \
            else f"应用「{app}」"
        prompt = (
            f"（主动开口）你注意到主人现在正在使用{where}。"
            f"趁他这会有空、没在跟你说话，主动说一句自然的、贴心的、不超过2句话的闲聊。"
            f"可以自然地结合他在做什么，但别太刻意、别盘问、别连续追问。"
            f"直接说内容，不要加任何前缀、括号说明或引号。"
        )
        if self.system_messages:
            messages = list(self.system_messages)
        else:
            messages = [{"role": "system", "content": self.instructions}]
        if self._history:
            messages += self._history[-self._max_hist:]
        messages.append({"role": "user", "content": prompt})

        self._cancel = False
        self._emit("bot_talking", "")
        full = ""
        q = asyncio.Queue()

        async def _feed():
            buf = ""
            try:
                async for delta in self._llm_stream_raw(messages):
                    if self._cancel:
                        break
                    buf += delta
                    while True:
                        idx = _split_index(buf)
                        if idx <= 0:
                            break
                        sentence = buf[:idx]
                        buf = buf[idx:]
                        await q.put(sentence)
                if buf.strip() and not self._cancel:
                    await q.put(buf)
            except Exception as e:
                print(f"[LOCAL-RT] 主动 LLM 流式失败: {e}")
            finally:
                await q.put(None)

        feed_task = asyncio.ensure_future(_feed())
        try:
            while True:
                sentence = await q.get()
                if sentence is None:
                    break
                if self._cancel:
                    break
                full += sentence
                await self._speak(sentence)
        finally:
            self._cancel = True
            try:
                await asyncio.wait_for(feed_task, timeout=2.0)
            except Exception:
                feed_task.cancel()
        if full.strip():
            self._history.append({"role": "assistant", "content": full})
            if len(self._history) > self._max_hist:
                self._history = self._history[-self._max_hist:]
        if not self._closed:
            self._emit("bot_done", full)
        if not self._closed:
            self._emit("ready", "")

    async def _proactive_watch(self):
        """后台巡查：你安静一阵后，结合当前窗口主动找你说话。"""
        interval = float(getattr(config, "REALTIME_PROACTIVE_INTERVAL", 30))
        idle_sec = float(getattr(config, "REALTIME_PROACTIVE_IDLE_SEC", 25))
        min_int = float(getattr(config, "REALTIME_PROACTIVE_MIN_INTERVAL", 90))
        prob = float(getattr(config, "REALTIME_PROACTIVE_PROB", 0.5))
        while not self._closed:
            await asyncio.sleep(interval)
            if self._closed:
                break
            now = time.time()
            if self._tts_active or self._recording:   # 正在说/你正在说 → 跳过
                continue
            if now - self._last_user_speech < idle_sec:
                continue
            if now - self._last_proactive_at < min_int:
                continue
            ctx = self._get_active_context()
            app = (ctx or {}).get("app", "")
            switched = bool(app) and app != self._last_app
            self._last_app = app or self._last_app
            chance = max(prob, 0.7) if switched else prob
            if random.random() > chance:
                continue
            self._last_proactive_at = now
            try:
                await self._proactive_say(ctx)
            except Exception as e:
                print(f"[LOCAL-RT] 主动说话失败: {e}")

    # ---- 播放 / 打断 ----
    def _ensure_player(self, sr):
        # sr = vox 输出采样率（通常 48000）；声卡设备有各自原生率，不一致需重采样。
        raw = getattr(config, "REALTIME_OUTPUT_DEVICE", "") or None
        dev = _resolve_output_device(raw)          # 整数索引或 None(=默认)
        dev_sr = _device_output_sr(dev) or sr
        if self._player is None or self._player.sr != dev_sr:
            if self._player is not None:
                try:
                    self._player.stop()
                except Exception as e:
                    degrade("realtime_voice.LocalRealtimeSession._ensure_player", e, "停旧播放器失败")
            self._player = _PCMPlayer(dev_sr, device=dev)
        if sr != dev_sr:
            if getattr(self, "_resampler", None) is None \
                    or self._resampler.in_sr != sr \
                    or self._resampler.out_sr != dev_sr:
                self._resampler = _StreamResampler(sr, dev_sr)
        else:
            self._resampler = None

    def _play_pcm(self, chunk):
        if self._player is None:
            return
        if self._resampler is not None:
            pcm = np.frombuffer(chunk, dtype=np.int16)
            resampled = self._resampler.process(pcm)
            chunk = resampled.astype(np.int16).tobytes()
        self._player.write(chunk)

    def _clear_playback(self):
        if self._player is not None:
            self._player.clear()

    async def _interrupt(self):
        self._cancel = True
        for resp in (self._tts_resp, self._llm_resp):
            if resp is not None:
                try:
                    resp.close()
                except Exception as e:
                    degrade("realtime_voice.LocalRealtimeSession._interrupt", e, "关响应流失败")
        self._clear_playback()

    def _emit(self, state, info=""):
        if not self.on_state:
            return
        try:
            result = self.on_state(state, info)
            if asyncio.iscoroutine(result):
                loop = self._loop
                if loop and not loop.is_closed():
                    asyncio.ensure_future(result)
        except Exception as e:
            print(f"[LOCAL-RT] on_state 异常: {e}")
