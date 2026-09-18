# -*- coding: utf-8 -*-
"""本地 VoxCPM2 TTS 接入（sidecar 进程 + HTTP 客户端）。

拓扑：voxcpm 及其依赖（torch / transformers 等）装在独立 venv_vox 里，
由 vox_tts_server.py 组成常驻进程（模型加载一次、显存持续占用）；
本模块跑在主 venv（bot 进程）内，只通过 localhost HTTP 调用：

    GET  /health        -> {"ready": bool, "error": "..."}  模型就绪才 ready=true
    POST /tts {text}    -> 48kHz wav 二进制（服务端串行推理）

职责：
- VoxTTSPlugin：FeaturePlugin，bot 启动时自动拉起 sidecar 子进程并健康轮询，
  stop 时终止；服务缺失/装失败只告警，bot 合成自动回退 GLM（不哑巴）。
- voxcpm_synthesize(text)：chat_service 的引擎分发入口，失败抛异常由上层回退 GLM。
"""
import asyncio
import hashlib
import os
import time

import httpx

import config
from plugin_base import FeaturePlugin
from quiet import attention, degrade

# 项目根目录（本文件所在目录）
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 单例服务句柄（VoxTTSPlugin.__init__ 创建；手动模式时为 None，只做 HTTP 调用）
_service = None


# ============================================================================
# sidecar 子进程管理
# ============================================================================
class _SidecarProcess:
    """管理 vox_tts_server.py 子进程 + 健康状态。"""

    def __init__(self):
        self.proc = None
        self.ready = False
        self.last_error = ""
        self._python = self._find_python()
        self._script = os.path.join(_BASE_DIR, "vox_tts_server.py")

    @staticmethod
    def _find_python() -> str:
        """venv_vox 的解释器；没装好返回空串（上层自动降级 GLM）。"""
        for rel in ("venv_vox/Scripts/python.exe", "venv_vox/Scripts/python"):
            full = os.path.join(_BASE_DIR, rel)
            if os.path.exists(full):
                return full
        return ""

    def usable(self) -> bool:
        """依赖是否齐备（venv + 服务脚本 + 权重）。"""
        if not self._python or not os.path.exists(self._script):
            return False
        model_dir = os.path.join(_BASE_DIR, getattr(config, "VOXCPM_MODEL_DIR", "models/VoxCPM2"))
        return os.path.exists(os.path.join(model_dir, "model.safetensors"))

    def spawn(self) -> bool:
        """拉起 sidecar（已在跑则不重复）。"""
        import subprocess
        if not self.usable():
            self.last_error = "venv_vox / vox_tts_server.py / 权重缺失"
            return False
        if self.proc and self.proc.poll() is None:
            return True
        log_path = os.path.join(_BASE_DIR, getattr(config, "VOXCPM_LOG_FILE", "vox_tts_server.log"))
        logf = open(log_path, "a", encoding="utf-8")
        try:
            env = dict(os.environ)
            env["VOXCPM_FFMPEG"] = str(getattr(config, "FFMPEG_PATH", "") or "")
            self.proc = subprocess.Popen(
                [self._python, self._script],
                cwd=_BASE_DIR,
                env=env,
                stdout=logf,
                stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            self.ready = False
            self.last_error = ""
            print(f"[VOXCPM] sidecar 已拉起 (pid={self.proc.pid})，等待模型加载...")
            return True
        except OSError as e:
            self.proc = None
            self.last_error = f"拉起失败: {e}"
            print(f"[VOXCPM] sidecar 拉起失败: {e}")
            return False

    def terminate(self):
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=10)
            except Exception:
                try:
                    self.proc.kill()
                except Exception as e:
                    attention("tts_vox._SidecarProcess.terminate", e, "兜底 kill 失败（可能残留 TTS sidecar）")
        self.proc = None
        self.ready = False


# ============================================================================
# HTTP 客户端 + 缓存
# ============================================================================
def _url(path: str) -> str:
    base = getattr(config, "VOXCPM_TTS_URL", "http://127.0.0.1:8765").rstrip("/")
    return f"{base}{path}"


async def _check_health(timeout: float = 5) -> dict:
    """探活；服务不可达/异常直接抛。"""
    async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
        resp = await client.get(_url("/health"))
    if resp.status_code != 200:
        raise RuntimeError(f"health HTTP {resp.status_code}")
    return resp.json()


# 上次"等待就绪超时"的时刻：超时后进入冷却，冷却期内快速失败不傻等
_last_wait_timeout = 0.0


async def _wait_ready() -> None:
    """等到本地 TTS 就绪；超时抛异常（上层回退 GLM）。

    服务在线但模型仍在加载（bot 刚启动约 2~4 分钟）时，语音请求会在这里
    轮询等待而不是立刻切 GLM——保证语音永远用 VoxCPM 音色，不跳童童。
    仅当等待超时（模型起不来/显存长期不足）才回退 GLM。
    """
    global _last_wait_timeout
    import time as _t
    now = _t.time()
    cooldown = getattr(config, "VOXCPM_DOWN_COOLDOWN_SECONDS", 60)
    if now - _last_wait_timeout < cooldown:
        # 冷却期内：快速探测一次（服务刚好就绪则立即恢复），否则快速失败
        try:
            health = await _check_health(timeout=2)
            if health.get("ready"):
                return
        except Exception as e:
            degrade("libs/qq_bot_runtime/tts_vox.py:146 _wait_ready", e, "降级：health = await _check_health(timeout=2)")
        raise RuntimeError("本地 TTS 暂不可用（冷却中，等待自愈）")

    deadline = now + getattr(config, "VOXCPM_WAIT_READY_SECONDS", 240)
    last_err = ""
    while True:
        try:
            health = await _check_health(timeout=5)
            last_err = f"{health.get('state', '?')} {health.get('error', '')}".strip()
            if health.get("ready"):
                return
        except Exception as e:
            last_err = str(e)[:150]
            # 依赖缺失（venv/权重）且进程已死 → 等也不会好，立即失败
            if (_service is not None and _service.proc is not None
                    and _service.proc.poll() is not None
                    and not _service.usable()):
                break
        if _t.time() > deadline:
            break
        await asyncio.sleep(5)
    _last_wait_timeout = _t.time()
    raise RuntimeError(f"本地 TTS 等待就绪超时: {last_err}")


def _cache_dir() -> str:
    d = os.path.join(_BASE_DIR, config.VOICE_DIR, "voxcpm_cache")
    os.makedirs(d, exist_ok=True)
    return d


def _voice_desc() -> str:
    return str(getattr(config, "VOXCPM_VOICE_DESC", "") or "").strip()


def _voice_speed() -> float:
    try:
        return float(getattr(config, "VOXCPM_VOICE_SPEED", 1.0) or 1.0)
    except (TypeError, ValueError):
        return 1.0


def _cache_path(text: str) -> str:
    # 缓存键含音色描述与语速：改描述/语速后旧缓存自动失效重合成
    key = hashlib.md5(f"voxcpm2|{_voice_desc()}|{_voice_speed()}|{text}".encode("utf-8")).hexdigest()
    return os.path.join(_cache_dir(), key + ".wav")


def _cache_cleanup():
    """缓存文件数超上限时删最旧。"""
    try:
        limit = getattr(config, "VOXCPM_CACHE_MAX_FILES", 200)
        files = [os.path.join(_cache_dir(), n) for n in os.listdir(_cache_dir())
                 if n.endswith(".wav")]
        for old in sorted(files, key=os.path.getmtime)[:-limit]:
            try:
                os.remove(old)
            except OSError as e:
                degrade("tts_vox._cache_cleanup", e, "删 TTS 缓存失败")
    except OSError as e:
        degrade("tts_vox._cache_cleanup", e, "列/清 TTS 缓存失败")


async def voxcpm_synthesize(text: str) -> str:
    """调本地 VoxCPM2 合成一段文字，返回 wav 文件路径（命中缓存直接返回）。

    服务在加载/短暂不可用时会先等待就绪（VOXCPM_WAIT_READY_SECONDS），
    超时才抛异常——由 chat_service.text_to_voice_wav 捕获回退 GLM 云端。
    """
    cache_enabled = getattr(config, "VOXCPM_ENABLE_CACHE", True)
    cached = _cache_path(text) if cache_enabled else ""
    if cache_enabled and os.path.exists(cached):
        return cached

    await _wait_ready()

    timeout = getattr(config, "VOXCPM_TIMEOUT_SECONDS", 180)
    async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
        resp = await client.post(_url("/tts"), json={
            "text": text, "voice_desc": _voice_desc(), "speed": _voice_speed()})
    if resp.status_code != 200:
        raise RuntimeError(f"本地 TTS 失败 {resp.status_code}: {resp.text[:200]}")

    # ---- 落盘：优先移进缓存目录，失败则留在 voice_tmp ----
    voice_dir = os.path.join(_BASE_DIR, config.VOICE_DIR)
    os.makedirs(voice_dir, exist_ok=True)
    wav_path = os.path.join(voice_dir, f"voice_{int(time.time() * 1000)}.wav")
    with open(wav_path, "wb") as f:
        f.write(resp.content)
    if cache_enabled:
        try:
            if not os.path.exists(cached):  # 并发同文本时另一请求已建缓存
                os.replace(wav_path, cached)
                _cache_cleanup()
            return cached
        except OSError as e:
            print(f"[VOXCPM] 缓存写入失败: {e}")
    return wav_path


# ============================================================================
# FeaturePlugin：随 bot 生命周期管理 sidecar
# ============================================================================
class VoxTTSPlugin(FeaturePlugin):
    """拉起/守护本地 VoxCPM2 TTS 服务子进程（缺失则自动降级 GLM）。"""

    name = "vox_tts"

    def __init__(self, core):
        super().__init__(core)
        self._keepalive = None
        global _service
        _service = _SidecarProcess()

    async def start(self):
        engine = getattr(config, "VOICE_TTS_ENGINE", "glm")
        if engine != "voxcpm":
            print(f"[VOXCPM] VOICE_TTS_ENGINE={engine}，本地 TTS 未启用")
            await super().start()
            return
        # 由 launcher 托管（QQBOT_MANAGED=1，launcher 已独家拉起 sidecar）
        # 或配置关闭自拉起时，不再重复 spawn，只作为 HTTP 客户端连接。
        if os.environ.get("QQBOT_MANAGED") or not getattr(config, "VOXCPM_SPAWN_SERVER", True):
            print("[VOXCPM] 由 launcher 托管或配置关闭自拉起，假定 local TTS 服务已运行")
        else:
            _service.spawn()
            self._keepalive = asyncio.get_event_loop().create_task(self._health_loop())
        await super().start()

    async def _health_loop(self):
        """后台守护：探活更新 ready；进程意外退出则冷却后重新拉起。"""
        last_spawn = 0.0
        try:
            while True:
                await asyncio.sleep(5)
                # 进程死了（或从未成功拉起过且依赖齐全）→ 冷却 30s 后尝试重启，
                # 避免系统内存紧张（页面文件不足）时形成密集重启风暴
                if not (_service.proc and _service.proc.poll() is None):
                    now = time.time()
                    if _service.usable() and now - last_spawn >= 30:
                        _service.ready = False
                        if _service.spawn():
                            last_spawn = now
                    continue
                try:
                    health = await _check_health()
                    _service.ready = bool(health.get("ready"))
                except Exception:
                    _service.ready = False
        except asyncio.CancelledError as e:
            degrade("libs/qq_bot_runtime/tts_vox.py:296 VoxTTSPlugin._health_loop", e, "降级：while True")
        except Exception as e:
            print(f"[VOXCPM] 健康轮询异常: {e}")

    async def stop(self):
        if self._keepalive:
            self._keepalive.cancel()
            self._keepalive = None
        _service.terminate()
        await super().stop()
