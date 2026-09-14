# -*- coding: utf-8 -*-
"""本地 VoxCPM2 TTS sidecar 服务（跑在独立 venv_vox，与 bot 主 venv 隔离）。

用法：venv_vox/Scripts/python.exe vox_tts_server.py [端口]
默认 127.0.0.1:8765（仅本机）。权重目录默认 <脚本目录>/models/VoxCPM2，
可用环境变量 VOXCPM_MODEL_DIR 覆盖。

接口：
    GET  /health  -> {"state":"idle|loading|ready|error","ready":bool,
                      "busy":bool,"vram_free_mb":n,"error":"..."}
    POST /tts     body {"text":"..."}  -> 200 48kHz wav（PCM_16），
                    未就绪 503，超长/参数错 400，合成失败 500

模型加载策略（踩坑记录，勿轻易改动）：
- 构造模型前设默认 dtype=bf16（与 config 一致；voxcpm 内部 Linear 不传 dtype，
  默认 fp32 会吃双倍显存导致 8G 卡 OOM）与默认 device=cuda；
- 权重用 mmap 流式逐张量灌入（safetensors 整文件读入 ~4.6GB 瞬态内存会触发
  WinError 1455，本机提交内存上限紧）；加载在独立线程完成；
- 线程栈 32MB：模型反序列化递归深，默认 1MB 线程栈会 StackOverflowException；
- 加载失败（显存/内存不足）每 60s 自愈重试；期间 /health 报 error，
  bot 侧自动回退 GLM，不哑巴；合成全局锁串行。
"""
import io
import json
import mmap
import os
import struct
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MODEL_DIR = os.environ.get("VOXCPM_MODEL_DIR") or os.path.join(BASE_DIR, "models", "VoxCPM2")
def _resolve_host_port():
    """解析命令行监听地址，兼容两种调用：

    - 旧用法（start.bat / 手动）：`python vox_tts_server.py 8765`
      位置参数第一个当作端口；
    - launcher 编排：`--host 127.0.0.1 --port 8765`
    """
    host, port = "127.0.0.1", 8765
    positional = []
    i = 1
    while i < len(sys.argv):
        a = sys.argv[i]
        if a in ("-h", "--host") and i + 1 < len(sys.argv):
            host = sys.argv[i + 1]
            i += 2
            continue
        if a in ("-p", "--port") and i + 1 < len(sys.argv):
            port = int(sys.argv[i + 1])
            i += 2
            continue
        if not a.startswith("-"):
            positional.append(a)
        i += 1
    if positional:
        port = int(positional[0])
    return host, port


HOST, PORT = _resolve_host_port()
MAX_TEXT_CHARS = 1000  # bot 侧按 140 字分片，这里只做护栏

# ---------------------------------------------------------------------------
# 流式权重加载(替代 voxcpm 内部整文件 load_file,规避 1455)
# ---------------------------------------------------------------------------
def _tensor_loader(safetensors_path):
    """生成器:yield (name, Tensor视图)。逐张量从 mmap 切片,不驻留整文件。

    返回的 Tensor 是 mmap 视图,调用方必须立即 copy,不能跨过 mmap 生命周期保存。
    """
    import torch
    dtype_map = {"BF16": torch.bfloat16, "F16": torch.float16, "F32": torch.float32,
                 "F64": torch.float64, "I64": torch.int64, "I32": torch.int32,
                 "I16": torch.int16, "I8": torch.int8, "U8": torch.uint8, "BOOL": torch.bool}
    with open(safetensors_path, "rb") as f:
        with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
            header_len = struct.unpack("<Q", mm[:8])[0]
            header = json.loads(mm[8:8 + header_len])
            base = 8 + header_len
            for name, info in header.items():
                if name == "__metadata__":
                    continue
                dt = dtype_map.get(info["dtype"])
                if dt is None:
                    continue
                start, end = info["data_offsets"]
                buf = mm[base + start: base + end]
                yield name, torch.frombuffer(buf, dtype=dt).reshape(info["shape"])


def install_streaming_from_local():
    """把 VoxCPM2Model.from_local 换成流式权重版本(其余语义保持一致)。"""
    import torch
    import voxcpm.model.voxcpm2 as M
    from transformers import LlamaTokenizerFast

    def streaming_from_local(cls, path, optimize=True, training=False,
                             device=None, lora_config=None):
        with open(os.path.join(path, "config.json"), "r", encoding="utf-8") as _f:
            config = M.VoxCPMConfig.model_validate_json(_f.read())
        tokenizer = LlamaTokenizerFast.from_pretrained(path)
        audio_vae = M.AudioVAEV2(config=config.audio_vae_config)
        ckpt = torch.load(os.path.join(path, "audiovae.pth"), map_location="cpu",
                          weights_only=True)
        vae_sd = ckpt.get("state_dict", ckpt)

        prev_dtype = torch.get_default_dtype()
        torch.set_default_dtype(_cfg_dtype(config))
        try:
            model = cls(config, tokenizer, audio_vae, lora_config, device=device)
        finally:
            torch.set_default_dtype(prev_dtype)

        lm_dtype = M.get_dtype(model.config.dtype)
        if not training:
            model = model.to(lm_dtype)
        model.audio_vae = model.audio_vae.to(torch.float32)

        param_map = dict(model.named_parameters())
        st_path = os.path.join(path, "model.safetensors")
        if os.path.exists(st_path):
            loaded = skipped = 0
            t0 = time.time()
            for name, t in _tensor_loader(st_path):
                p = param_map.get(name)
                if p is not None and tuple(p.shape) == tuple(t.shape):
                    p.data.copy_(t)
                    loaded += 1
                else:
                    skipped += 1
            _log(f"权重灌入 {loaded} 个, 跳过 {skipped}, 用时 {time.time()-t0:.0f}s")
        vae_params = {k: v for k, v in model.named_parameters() if k.startswith("audio_vae.")}
        for name, t in vae_sd.items():
            pk = f"audio_vae.{name}"
            p = vae_params.get(pk)
            if p is not None and tuple(p.shape) == tuple(t.shape):
                p.data.copy_(t)

        if training:
            return model
        return model.to(model.device).eval().optimize(disable=not optimize)

    M.VoxCPM2Model.from_local = classmethod(streaming_from_local)


def _cfg_dtype(config):
    import torch
    dtype = getattr(config, "dtype", "bfloat16")
    if isinstance(dtype, str):
        return {"bfloat16": torch.bfloat16, "float16": torch.float16,
                "float32": torch.float32}.get(dtype, torch.bfloat16)
    return dtype


# ---------------------------------------------------------------------------
# 模型加载与推理（独立线程，串行）
# ---------------------------------------------------------------------------
_load_lock = threading.Lock()
_gen_lock = threading.Lock()

_state = {"state": "idle", "error": "", "vram_free_mb": 0, "model": None}


def _log(msg: str):
    print(f"[vox_tts_server] {msg}", flush=True)


def _commit_headroom_mb() -> int:
    """系统可用提交内存余量（MB）；查询失败返回 -1（不设防）。"""
    try:
        import ctypes

        class _MS(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_uint32), ("dwMemoryLoad", ctypes.c_uint32),
                ("ullTotalPhys", ctypes.c_uint64), ("ullAvailPhys", ctypes.c_uint64),
                ("ullTotalPageFile", ctypes.c_uint64), ("ullAvailPageFile", ctypes.c_uint64),
                ("ullTotalVirtual", ctypes.c_uint64), ("ullAvailVirtual", ctypes.c_uint64),
                ("ullAvailExtendedVirtual", ctypes.c_uint64),
            ]

        ms = _MS()
        ms.dwLength = ctypes.sizeof(_MS)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(ms)):
            return -1
        return int(ms.ullAvailPageFile // (1024 * 1024))
    except Exception:
        return -1


def _vram_free_mb() -> int:
    try:
        import torch
        if torch.cuda.is_available():
            free, _ = torch.cuda.mem_get_info()
            return int(free // (1024 * 1024))
    except Exception:
        pass
    return -1


def _load_model():
    """后台线程入口：加载模型，失败自动重试（内存/显存腾出后自愈）。"""
    while True:
        try:
            _load_lock.acquire()
            try:
                import torch
                if torch.cuda.is_available():
                    # 权重直接分配在显存:默认在 CPU 上构建 2B 参数会额外吃
                    # ~4GB 系统提交内存(WinError 1455/段错误的根源之一);
                    # dtype 由 install_streaming_from_local 内按 config 设 bf16
                    torch.set_default_device("cuda")
                from voxcpm import VoxCPM
                install_streaming_from_local()
                _log(f"开始加载模型: {MODEL_DIR}（空闲显存 {_vram_free_mb()} MB）")
                t0 = time.time()
                model = VoxCPM.from_pretrained(MODEL_DIR, load_denoiser=False, optimize=True)
                _log(f"模型加载完成，用时 {time.time() - t0:.0f}s，空闲显存 {_vram_free_mb()} MB")
                _state["model"] = model
                _state["state"] = "ready"
                _state["error"] = ""
                return
            finally:
                _load_lock.release()
        except Exception as e:
            _state["state"] = "error"
            _state["error"] = f"{type(e).__name__}: {e}"
            _log(f"模型加载失败（{_state['error']}），60s 后重试")
            time.sleep(60)


def _ffmpeg_path() -> str:
    """ffmpeg 可执行路径：优先环境变量，其次常见位置。"""
    cand = os.environ.get("VOXCPM_FFMPEG", "")
    if not cand:
        cand = os.path.join(BASE_DIR, "..", "ffmpeg-2026-05-28-git-7b46c6a2a3-full_build",
                            "ffmpeg-2026-05-28-git-7b46c6a2a3-full_build", "bin", "ffmpeg.exe")
    if not cand:
        cand = "ffmpeg"
    return cand


def _resample_speed(wav_bytes: bytes, speed: float) -> bytes:
    """对 wav 做保音高变速（atempo），用于不改变声线地微调语速。

    speed<1 变慢、>1 变快；仅音高不变的整体时长伸缩。
    """
    import subprocess
    import tempfile
    if abs(speed - 1.0) < 0.005:
        return wav_bytes
    ff = _ffmpeg_path()
    with tempfile.TemporaryDirectory() as td:
        tin = os.path.join(td, "in.wav")
        tout = os.path.join(td, "out.wav")
        with open(tin, "wb") as f:
            f.write(wav_bytes)
        # atempo 单段支持 0.5~2.0；超出范围可级联，这里限制 0.6~1.6 足够
        speed = max(0.6, min(1.6, speed))
        r = subprocess.run(
            [ff, "-y", "-loglevel", "error", "-i", tin, "-af", f"atempo={speed:.4f}", tout],
            capture_output=True, timeout=180)
        if r.returncode != 0:
            _log(f"变速失败({r.stderr.decode('utf-8', 'ignore')[:150]})，返回原速音频")
            return wav_bytes
        with open(tout, "rb") as f:
            return f.read()


def _synthesize(text: str, voice_desc: str = "", seed: int = None,
                speed: float = 1.0) -> bytes:
    """合成（调用方需持 _gen_lock）；返回 48kHz wav 字节。

    voice_desc 非空时按 VoxCPM2 语音设计协议在正文前拼接 "(描述)"——
    模型据此凭空生成指定音色（性别/年龄/语气/情绪/语速等自然语言描述）。

    seed：VoxCPM 的 Voice Design 无内置 seed，同一描述每次生成声线都会随机
    漂移（听感=每次声音都不一样）。这里在生成前固定 torch 随机种子：
    seed=None 时按描述哈希派生（同描述恒同声线、换描述自动换声线），
    保证 QQ 端听到的音色与试听样本一致、可复现。

    speed：合成后整体保音高变速（atempo）。改描述里的语速词会连声线种子
    一起变（声音就变了），用变速则声音采样完全不变、只慢/快一点。
    """
    import hashlib

    import torch
    model = _state.get("model")
    full_text = f"({voice_desc.strip()}){text}" if voice_desc and voice_desc.strip() else text
    if seed is None:
        seed = int(hashlib.md5((voice_desc.strip() or "default").encode("utf-8")).hexdigest(),
                   16) % (2 ** 31)
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    t0 = time.time()
    wav = model.generate(text=full_text, cfg_value=2.0, inference_timesteps=10)
    sr = getattr(model, "tts_model", None)
    sample_rate = getattr(sr, "sample_rate", 48000) if sr is not None else 48000
    _log(f"合成 {len(text)} 字（voice_desc={bool(voice_desc)} seed={seed} speed={speed}）"
         f"用时 {time.time() - t0:.1f}s（{sample_rate}Hz）")
    buf = io.BytesIO()
    # voxcpm 依赖自带 soundfile；float -> PCM_16 wav
    import soundfile as sf
    sf.write(buf, np.asarray(wav, dtype=np.float32), sample_rate,
             format="WAV", subtype="PCM_16")
    out = buf.getvalue()
    if abs(speed - 1.0) >= 0.005:
        out = _resample_speed(out, speed)
    return out


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # 静默访问日志（错误仍由 do_* 打印）
        pass

    def _send_json(self, code: int, obj: dict):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.rstrip("/") in ("", "/health"):
            state = _state["state"]
            busy = False
            if state == "ready":
                busy = not _gen_lock.acquire(blocking=False)
                if not busy:
                    _gen_lock.release()
            self._send_json(200, {
                "ready": state == "ready",
                "state": state,
                "busy": busy,
                "vram_free_mb": _vram_free_mb(),
                "error": _state["error"],
            })
            return
        self._send_json(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.rstrip("/")
        if path == "/tts/stream":
            self._do_tts_stream()
            return
        if path != "/tts":
            self._send_json(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(length) or b"{}")
            text = str(req.get("text", "")).strip()
            voice_desc = str(req.get("voice_desc", "")).strip()
            seed = req.get("seed")
            seed = int(seed) if seed is not None else None
            speed = float(req.get("speed", 1.0) or 1.0)
            speed = max(0.6, min(1.6, speed))
        except Exception as e:
            self._send_json(400, {"error": f"bad request: {e}"})
            return
        if not text:
            self._send_json(400, {"error": "empty text"})
            return
        if len(text) > MAX_TEXT_CHARS:
            self._send_json(400, {"error": f"text too long ({len(text)}>{MAX_TEXT_CHARS})"})
            return
        if _state["state"] != "ready":
            self._send_json(503, {"error": f"model not ready: {_state['state']} {_state['error']}"})
            return
        with _gen_lock:
            try:
                wav = _synthesize(text, voice_desc, seed, speed)
            except Exception as e:
                _log(f"合成异常: {type(e).__name__}: {e}")
                self._send_json(500, {"error": f"{type(e).__name__}: {e}"})
                return
        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Content-Length", str(len(wav)))
        self.end_headers()
        self.wfile.write(wav)

    def _do_tts_stream(self):
        """流式合成端点 /tts/stream：逐块推 raw PCM(int16,单声道,模型原生采样率)。

        连接关闭即结束（HTTP/1.0 + Connection: close，客户端读到 EOF 为止）。
        首包在 diffusion 前若干 patch 后产出，配合本地实时引擎实现
        「肥鱼娘声线（voxcpm2 voice-design）+ 低延迟」。声线由 voice_desc 哈希
        种子固定，与 /tts 整句合成、QQ 离线语音、试听样本完全一致。
        """
        try:
            length = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(length) or b"{}")
            text = str(req.get("text", "")).strip()
            voice_desc = str(req.get("voice_desc", "")).strip()
            seed = req.get("seed")
            seed = int(seed) if seed is not None else None
            speed = float(req.get("speed", 1.0) or 1.0)
            speed = max(0.6, min(1.6, speed))
            cfg_value = float(req.get("cfg_value", 2.0) or 2.0)
            inference_timesteps = int(req.get("inference_timesteps", 10) or 10)
            inference_timesteps = max(1, min(50, inference_timesteps))
        except Exception as e:
            self._send_json(400, {"error": f"bad request: {e}"})
            return
        if not text:
            self._send_json(400, {"error": "empty text"})
            return
        if len(text) > MAX_TEXT_CHARS:
            self._send_json(400, {"error": f"text too long ({len(text)}>{MAX_TEXT_CHARS})"})
            return
        if _state["state"] != "ready":
            self._send_json(503, {"error": f"model not ready: {_state['state']} {_state['error']}"})
            return
        import hashlib

        import torch
        model = _state["model"]
        sr = getattr(getattr(model, "tts_model", None), "sample_rate", 48000)
        full_text = f"({voice_desc.strip()}){text}" if voice_desc and voice_desc.strip() else text
        if seed is None:
            seed = int(hashlib.md5((voice_desc.strip() or "default").encode("utf-8")).hexdigest(),
                       16) % (2 ** 31)
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("X-Sample-Rate", str(sr))
        self.send_header("X-Channels", "1")
        self.send_header("X-Bits", "16")
        self.send_header("Connection", "close")
        self.end_headers()
        torch.manual_seed(seed)
        torch.cuda.manual_seed_all(seed)
        try:
            with _gen_lock:
                for chunk in model.generate_streaming(text=full_text,
                                                      cfg_value=cfg_value,
                                                      inference_timesteps=inference_timesteps):
                    arr = np.asarray(chunk, dtype=np.float32)
                    if arr.size == 0:
                        continue
                    pcm = (np.clip(arr, -1.0, 1.0) * 32767.0).astype(np.int16)
                    data = pcm.tobytes()
                    if not data:
                        continue
                    try:
                        self.wfile.write(data)
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        return  # 客户端（实时引擎）打断/断开 → 释放 _gen_lock
        except Exception as e:
            _log(f"流式合成异常: {type(e).__name__}: {e}")
            return


def main():
    # pythonw 下 stdout/stderr 默认被丢弃，崩溃无迹可寻；重定向到日志文件。
    try:
        _vox_log = open(os.path.join(BASE_DIR, "vox_tts_server.log"), "a", encoding="utf-8")
        sys.stdout = _vox_log
        sys.stderr = _vox_log
    except Exception:
        pass
    sys.setrecursionlimit(20000)  # 模型反序列化/构建有深层递归，默认 1000 不够
    _log(f"VoxCPM2 TTS sidecar 启动: http://{HOST}:{PORT}  模型: {MODEL_DIR}")
    headroom = _commit_headroom_mb()
    if 0 <= headroom < 1024:
        # 系统提交内存余量不足（典型：C 盘满/内存被占），torch 加载必炸 1455。
        # 安静退出，由 bot 侧 30s 冷却后重试，腾出空间后自愈。
        _log(f"系统提交内存余量仅 {headroom} MB（<1024），本次退出等待内存释放...")
        sys.exit(2)
    # 模型加载放独立线程：Windows 默认线程栈仅 1MB，反序列化递归易栈溢出(StackOverflowException)，
    # 需先放大线程栈再创建线程；加载失败(显存/内存不足)自动重试不退出。
    threading.stack_size(32 * 1024 * 1024)
    threading.Thread(target=_load_model, daemon=True).start()
    ThreadingHTTPServer((HOST, PORT), _Handler).serve_forever()


if __name__ == "__main__":
    main()
