# -*- coding: utf-8 -*-
"""摄像头实时捕获器。

提供与 mss 截图同构的接口：
- open_camera(index): 打开摄像头
- read(): 读取一帧 raw RGB numpy 数组
- release(): 释放摄像头

设计要点：
- 在独立线程中循环 read()，把最新帧放到队列；
- 不依赖 asyncio 主循环读取，避免 asyncio sleep 精度问题；
- 使用 OpenCV VideoCapture，支持设置分辨率、帧率。
"""
import queue
import threading
import time
from typing import Optional, Tuple

import cv2
import numpy as np


class CameraCapture:
    def __init__(
        self,
        index: int = 0,
        width: int = 1280,
        height: int = 720,
        fps: int = 30,
    ):
        self.index = index
        self.target_width = width
        self.target_height = height
        self.target_fps = fps
        self.cap: Optional[cv2.VideoCapture] = None

        self._latest_frame: Optional[np.ndarray] = None
        self._latest_ts = 0.0
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    def open(self) -> bool:
        """打开摄像头并启动采集线程。"""
        self.cap = cv2.VideoCapture(self.index, cv2.CAP_DSHOW)
        if not self.cap.isOpened():
            # 回退默认后端
            self.cap = cv2.VideoCapture(self.index)
        if not self.cap.isOpened():
            print(f"[ERROR] 无法打开摄像头 {self.index}")
            return False

        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.target_width)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.target_height)
        self.cap.set(cv2.CAP_PROP_FPS, self.target_fps)

        # 预热，丢弃前几帧
        for _ in range(5):
            self.cap.read()

        self._running = True
        self._thread = threading.Thread(target=self._capture_loop, daemon=True)
        self._thread.start()
        print(
            f"[INFO] 摄像头 {self.index} 已打开，"
            f"分辨率 {self.target_width}x{self.target_height}，目标帧率 {self.target_fps}"
        )
        return True

    def release(self):
        """停止采集线程并释放摄像头。"""
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        if self.cap:
            self.cap.release()
            self.cap = None
        print(f"[INFO] 摄像头 {self.index} 已释放")

    def _capture_loop(self):
        """后台线程：持续 read() 并保存最新帧。"""
        interval = 1.0 / self.target_fps
        while self._running and self.cap and self.cap.isOpened():
            t0 = time.time()
            ok, frame = self.cap.read()
            if ok and frame is not None:
                # OpenCV 默认 BGR，转成 RGB
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                with self._lock:
                    self._latest_frame = rgb
                    self._latest_ts = time.time()

            # 精确 sleep
            elapsed = time.time() - t0
            sleep_time = interval - elapsed
            if sleep_time > 0:
                time.sleep(sleep_time)

    def read(self) -> Optional[np.ndarray]:
        """获取最新一帧 RGB 图。"""
        with self._lock:
            frame = self._latest_frame
            if frame is not None:
                return frame.copy()
            return None

    def get_timestamp(self) -> float:
        with self._lock:
            return self._latest_ts

    def get_actual_resolution(self) -> Tuple[int, int]:
        if self.cap:
            w = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            h = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            return w, h
        return 0, 0

    def get_actual_fps(self) -> float:
        if self.cap:
            return self.cap.get(cv2.CAP_PROP_FPS)
        return 0.0
