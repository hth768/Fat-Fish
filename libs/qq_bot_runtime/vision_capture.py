# -*- coding: utf-8 -*-
"""实时画面捕获与动态视觉理解模块（高帧率版）。

优化点：
- 采样循环只截图 raw RGB + 灰度差分，不编码，最大限度提升帧率；
- 识别时再调用 opencv 把最新帧编码成 JPEG，送入 Gemini；
- 截图在独立线程池中执行，不阻塞 asyncio 事件循环；
- 采样循环与识别循环分离，采样帧率（截图）和识别帧率（Gemini）独立可调。

控制命令（在 bot.py 中处理）：
- /看屏幕          开始捕获整个主屏幕
- /看窗口 <标题>    开始捕获指定窗口（Windows）
- /别看            停止捕获
"""
import asyncio
import base64
import io
import time
from concurrent.futures import ThreadPoolExecutor
from typing import List, Optional, Tuple

import cv2
import httpx
import mss
import numpy as np
from PIL import Image

import config
from camera_capture import CameraCapture
from ai_provider import get_vision


# 线程池：截图是 CPU/IO 混合操作，单线程截图足够；编码识别异步执行
_SCREENSHOT_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="vision-cap-")


def _raw_to_jpeg(rgb: np.ndarray, max_size: int = 1280, quality: int = 85) -> bytes:
    """把 raw RGB numpy 数组编码为 JPEG 字节。"""
    h, w = rgb.shape[:2]
    if max(w, h) > max_size:
        ratio = max_size / max(w, h)
        rgb = cv2.resize(rgb, (int(w * ratio), int(h * ratio)), interpolation=cv2.INTER_LANCZOS4)
    ok, buf = cv2.imencode(".jpg", cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR), [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    if not ok:
        raise RuntimeError("JPEG 编码失败")
    return buf.tobytes()


def _gray_thumb(rgb: np.ndarray, size: Tuple[int, int] = (320, 180)) -> np.ndarray:
    """生成用于变化检测的灰度缩略图。"""
    small = cv2.resize(rgb, size, interpolation=cv2.INTER_AREA)
    return cv2.cvtColor(small, cv2.COLOR_RGB2GRAY).astype(np.float32)


class VisionCapture:
    def __init__(self, ws=None):
        self.ws = ws
        self.running = False
        self._capture_task: Optional[asyncio.Task] = None
        self._recognition_task: Optional[asyncio.Task] = None
        self._recognition_event = asyncio.Event()
        self._executor = _SCREENSHOT_EXECUTOR

        self.mode: Optional[str] = None  # "screen" / "window" / "camera" / "dxgi"
        self.window_title: Optional[str] = None

        # 帧缓冲区，保存 (timestamp, raw_rgb) 元组
        self.frame_buffer: List[Tuple[float, np.ndarray]] = []
        self.last_frame_gray: Optional[np.ndarray] = None
        self.latest_description = ""
        self.last_description_time = 0.0
        self.last_report_time = 0.0
        self.last_alert_time = 0.0
        self.start_time = 0.0

        self.vision = get_vision()
        self._lock = asyncio.Lock()
        self._camera: Optional[CameraCapture] = None
        self._dxcam: Optional["DXCamCapture"] = None

    # ----------------- 公共控制接口 -----------------

    async def start_camera(self, index: int = 0):
        """开始捕获摄像头画面。"""
        await self.stop()
        cam = CameraCapture(
            index=index,
            width=getattr(config, "CAMERA_WIDTH", 1280),
            height=getattr(config, "CAMERA_HEIGHT", 720),
            fps=getattr(config, "CAMERA_FPS", 30),
        )
        if not cam.open():
            raise RuntimeError(f"无法打开摄像头 {index}")
        self._camera = cam
        self.mode = "camera"
        self.window_title = None
        self._reset_state()
        self.running = True
        self.start_time = time.time()
        self._capture_task = asyncio.create_task(self._capture_loop())
        self._recognition_task = asyncio.create_task(self._recognition_loop())
        print(f"[INFO] 实时视觉已启动：摄像头 {index}")

    async def start_dxgi(self, output_idx: int = 0):
        """开始用 DXcam (DXGI Desktop Duplication) 捕获指定显示器。

        如果 DXcam 打不开（笔记本混合显卡 Optimus 下常见），自动回退到 mss 屏幕捕获。
        """
        await self.stop()
        from dxcam_capture import DXCamCapture
        cap = DXCamCapture(output_idx=output_idx)
        if cap.open():
            self._dxcam = cap
            self.mode = "dxgi"
            self.window_title = None
            self._reset_state()
            self.running = True
            self.start_time = time.time()
            self._capture_task = asyncio.create_task(self._capture_loop())
            self._recognition_task = asyncio.create_task(self._recognition_loop())
            print(f"[INFO] 实时视觉已启动：DXGI 显示器 {output_idx}")
        else:
            # 回退到 mss 屏幕捕获（核显也能用）
            print("[WARN] DXcam 不可用（可能混合显卡/Optimus），回退到 mss 屏幕捕获")
            await self._start_screen_internal()

    async def start_screen(self):
        """开始捕获主屏幕。"""
        await self._start_screen_internal()

    async def _start_screen_internal(self):
        """内部方法：启动 mss 屏幕捕获。"""
        await self.stop()
        self.mode = "screen"
        self.window_title = None
        self._reset_state()
        self.running = True
        self.start_time = time.time()
        self._capture_task = asyncio.create_task(self._capture_loop())
        self._recognition_task = asyncio.create_task(self._recognition_loop())
        print("[INFO] 实时视觉已启动：捕获主屏幕")

    async def start_window(self, title: str):
        """开始捕获指定标题的窗口（Windows 平台）。"""
        await self.stop()
        self.mode = "window"
        self.window_title = title.strip()
        self._reset_state()
        self.running = True
        self.start_time = time.time()
        self._capture_task = asyncio.create_task(self._capture_loop())
        self._recognition_task = asyncio.create_task(self._recognition_loop())
        print(f"[INFO] 实时视觉已启动：捕获窗口 [{self.window_title}]")

    async def stop(self):
        """停止捕获。"""
        self.running = False
        self._recognition_event.set()
        if self._capture_task and not self._capture_task.done():
            self._capture_task.cancel()
            try:
                await self._capture_task
            except asyncio.CancelledError:
                pass
        if self._recognition_task and not self._recognition_task.done():
            self._recognition_task.cancel()
            try:
                await self._recognition_task
            except asyncio.CancelledError:
                pass
        self._capture_task = None
        self._recognition_task = None
        if self._camera:
            self._camera.release()
            self._camera = None
        if self._dxcam:
            self._dxcam.release()
            self._dxcam = None
        self.mode = None
        self.window_title = None
        async with self._lock:
            self.latest_description = ""
            self.frame_buffer.clear()
        print("[INFO] 实时视觉已停止")

    def is_running(self) -> bool:
        return self.running

    async def get_scene_hint(self) -> str:
        """供 bot.py 注入 system 上下文，返回当前场景描述。"""
        if not self.running:
            return ""
        async with self._lock:
            desc = self.latest_description
            elapsed = int(time.time() - self.last_description_time)
        if not desc:
            return ""
        return f"【当前实时画面（{elapsed}秒前更新）】{desc}"

    # ----------------- 内部实现 -----------------

    def _reset_state(self):
        self.frame_buffer.clear()
        self.last_frame_gray = None
        self.latest_description = ""
        self.last_description_time = 0.0
        self.last_report_time = 0.0
        self.last_alert_time = 0.0

    async def _send_alert(self, message: str):
        """向配置的管理员发送提醒（通过统一 MessageSender，平台无关）。"""
        if not getattr(config, "BALANCE_ALERT_USER_ID", ""):
            return
        now = time.time()
        if now - self.last_alert_time < 60:
            return
        self.last_alert_time = now
        try:
            from message_bus import get_sender
            sender = get_sender()
            if sender is None:
                print(f"[VISION-ALERT] 无平台 sender，提醒未发送: {message[:60]}")
                return
            await sender.send_private(config.BALANCE_ALERT_USER_ID, message)
        except Exception as e:
            print(f"[WARN] 视觉提醒发送失败: {e}")

    async def _capture_loop(self):
        """采样循环：高频截图 + 变化检测 + 唤醒识别循环。"""
        if self.mode == "camera":
            interval = 1.0 / getattr(config, "CAMERA_FPS", 30)
        else:
            interval = getattr(config, "VISION_CAPTURE_INTERVAL", 1 / 24)
        max_duration = getattr(config, "VISION_MAX_DURATION", 600)

        try:
            while self.running:
                if time.time() - self.start_time > max_duration:
                    await self._send_alert(
                        f"实时视觉已自动停止（超过最大运行时间 {max_duration} 秒），"
                        f"如需继续请看屏幕/看窗口。"
                    )
                    await self.stop()
                    return

                t0 = time.time()
                try:
                    frame = await self._capture_frame()
                    if frame is not None:
                        changed = self._detect_change(frame)
                        async with self._lock:
                            self.frame_buffer.append((time.time(), frame))
                            # 按时间保留最近 N 秒的帧
                            buffer_time = getattr(config, "VISION_BUFFER_TIME", 2.0)
                            cutoff = time.time() - buffer_time
                            while self.frame_buffer and self.frame_buffer[0][0] < cutoff:
                                self.frame_buffer.pop(0)
                            # 同时限制最大数量，防止内存无限增长
                            max_buf = getattr(config, "VISION_FRAME_BUFFER_SIZE", 48)
                            while len(self.frame_buffer) > max_buf:
                                self.frame_buffer.pop(0)

                        if changed:
                            self._recognition_event.set()
                except Exception as e:
                    print(f"[WARN] 画面捕获异常: {e}")

                # 精确睡到目标帧率
                elapsed = time.time() - t0
                sleep_time = interval - elapsed
                if sleep_time > 0:
                    await asyncio.sleep(sleep_time)
        except asyncio.CancelledError:
            print("[INFO] 实时视觉采样循环已取消")
        finally:
            self.running = False

    async def _recognition_loop(self):
        """识别循环：按最小间隔消费最新帧，保证高采样率下 API 不被打爆。"""
        min_interval = getattr(config, "VISION_MIN_RECOGNITION_INTERVAL", 0.5)
        max_interval = getattr(config, "VISION_MAX_RECOGNITION_INTERVAL", 3.0)

        try:
            while self.running:
                try:
                    wait = max(0.05, max_interval - (time.time() - self.last_report_time))
                    await asyncio.wait_for(self._recognition_event.wait(), timeout=wait)
                except asyncio.TimeoutError:
                    pass

                if not self.running:
                    break
                self._recognition_event.clear()

                now = time.time()
                wait = min_interval - (now - self.last_report_time)
                if wait > 0:
                    await asyncio.sleep(wait)

                if not self.running:
                    break

                async with self._lock:
                    if not self.frame_buffer:
                        continue
                    # 取最近 N 帧均匀采样，避免缓冲区里帧太多 token 爆炸
                    frames = [f for _, f in self.frame_buffer]
                    sample_count = getattr(config, "VISION_RECOGNITION_SAMPLE_COUNT", 5)
                    if len(frames) > sample_count:
                        idx = np.linspace(0, len(frames) - 1, sample_count, dtype=int)
                        frames = [frames[i] for i in idx]

                self.last_report_time = time.time()
                await self._recognize_scene(frames)
        except asyncio.CancelledError:
            print("[INFO] 实时视觉识别循环已取消")
        except Exception as e:
            print(f"[WARN] 识别循环异常: {e}")

    async def _capture_frame(self) -> Optional[np.ndarray]:
        """捕获一帧 raw RGB 画面，返回 numpy 数组。"""
        if self.mode == "camera":
            return self._camera.read() if self._camera else None
        elif self.mode == "dxgi":
            return self._dxcam.read() if self._dxcam else None
        elif self.mode == "screen":
            fn = self._capture_screen_raw
        elif self.mode == "window":
            fn = lambda: self._capture_window_raw(self.window_title)
        else:
            return None
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, fn)

    def _capture_screen_raw(self) -> Optional[np.ndarray]:
        """截取主屏幕，返回 RGB numpy 数组。"""
        try:
            with mss.MSS() as sct:
                monitor = sct.monitors[0]
                img = sct.grab(monitor)
                arr = np.frombuffer(img.raw, dtype=np.uint8).reshape((img.height, img.width, 4))
                return arr[:, :, :3].copy()
        except Exception as e:
            print(f"[WARN] 屏幕截图失败: {e}")
            return None

    def _capture_window_raw(self, title: str) -> Optional[np.ndarray]:
        """截取指定标题窗口（Windows），返回 RGB numpy 数组。"""
        try:
            import pygetwindow as gw
            win = None
            try:
                win = gw.getWindowsWithTitle(title)[0]
            except IndexError:
                wins = gw.getAllWindows()
                for w in wins:
                    if title.lower() in w.title.lower() and w.title.strip():
                        win = w
                        break
            if not win or win.width <= 0 or win.height <= 0:
                print(f"[WARN] 未找到窗口或窗口不可见: {title}")
                return None

            with mss.MSS() as sct:
                monitor = {"left": win.left, "top": win.top, "width": win.width, "height": win.height}
                img = sct.grab(monitor)
                arr = np.frombuffer(img.raw, dtype=np.uint8).reshape((img.height, img.width, 4))
                return arr[:, :, :3].copy()
        except Exception as e:
            print(f"[WARN] 窗口截图失败: {e}")
            return None

    def _detect_change(self, frame: np.ndarray) -> bool:
        """检测当前帧相对上一帧是否有显著变化。"""
        try:
            gray = _gray_thumb(frame)
            if self.last_frame_gray is None:
                self.last_frame_gray = gray
                return True

            diff = np.mean(np.abs(gray - self.last_frame_gray))
            self.last_frame_gray = gray
            threshold = getattr(config, "VISION_CHANGE_THRESHOLD", 2.0)
            return diff > threshold
        except Exception as e:
            print(f"[WARN] 变化检测失败: {e}")
            return True

    async def _recognize_scene(self, frames: List[np.ndarray]):
        """调用 Gemini 识别帧序列，更新场景描述。"""
        if not frames:
            return
        try:
            loop = asyncio.get_running_loop()
            # 在独立线程中编码多帧 JPEG，避免阻塞事件循环
            jpeg_frames = await loop.run_in_executor(
                None,
                lambda: [_raw_to_jpeg(f) for f in frames],
            )

            prompt = (
                "这是按时间顺序排列的连续画面帧。请用一句话简要描述："
                "画面里主要有什么、正在发生什么变化或动作、画面上有什么文字。"
                "控制在 120 字以内，用中文回答。"
            )
            desc = await self.vision.describe_frames(jpeg_frames, prompt=prompt, max_output_tokens=256)
            if desc:
                async with self._lock:
                    self.latest_description = desc
                    self.last_description_time = time.time()
                print(f"[INFO] 实时视觉识别: {desc[:120]}")
        except httpx.TimeoutException:
            print("[WARN] Gemini 视觉识别超时")
        except Exception as e:
            print(f"[WARN] Gemini 视觉识别失败: {e}")


# 全局单例，供 bot.py 和 scheduler.py 共享
vision_capture = VisionCapture()
