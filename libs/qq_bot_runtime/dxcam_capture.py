# -*- coding: utf-8 -*-
"""基于 DXcam 的高性能 Windows 屏幕捕获器。

DXcam 使用 Windows DXGI Desktop Duplication API，1080p 下可稳定 240+ FPS，
比 mss 快数倍。它返回 numpy BGRA 数组，格式与 vision_capture.py 兼容。

限制：
- 只能捕获显示器（指定 output_idx），不能精确捕获某个窗口。
- 无法零拷贝到 GPU，仍需 torch.from_numpy(...).cuda() 上传，但 CPU 拷贝已经优化到最低。
- 某些全屏游戏/反作弊场景可能无法捕获。

使用方式：
    from dxcam_capture import DXCamCapture
    cap = DXCamCapture(output_idx=0)
    cap.open()
    frame = cap.read()   # np.ndarray RGB
    cap.release()
"""
from typing import Optional

import numpy as np

import dxcam


class DXCamCapture:
    def __init__(self, output_idx: int = 0, region: tuple = None):
        self.output_idx = output_idx
        self.region = region  # (left, top, right, bottom)
        self.camera: Optional[dxcam.DXCamera] = None

    def open(self) -> bool:
        try:
            self.camera = dxcam.create(output_idx=self.output_idx, region=self.region)
            if self.camera is None:
                print(f"[ERROR] DXcam 无法创建 output_idx={self.output_idx} 的相机")
                return False
            # 试抓一帧验证可用（某些笔记本混合显卡/驱动会创建成功但捕获失败）
            test = self.camera.grab()
            if test is None:
                print("[WARN] DXcam 创建成功但无法捕获帧，将视为不可用")
                try:
                    self.camera.release()
                except Exception:
                    pass
                self.camera = None
                return False
            print(f"[INFO] DXcam 已打开，output_idx={self.output_idx}")
            return True
        except Exception as e:
            print(f"[ERROR] DXcam 打开失败: {e}")
            return False

    def read(self) -> Optional[np.ndarray]:
        """读取最新一帧，返回 RGB numpy 数组。"""
        if self.camera is None:
            return None
        try:
            # dxcam.grab() 返回 BGRA numpy 数组，无新帧时返回 None
            frame = self.camera.grab()
            if frame is None:
                return None
            # BGRA -> RGB
            return frame[:, :, :3][:, :, ::-1]
        except Exception as e:
            print(f"[WARN] DXcam 读取失败: {e}")
            return None

    def release(self):
        if self.camera is not None:
            try:
                self.camera.release()
            except Exception as e:
                print(f"[WARN] DXcam 释放失败: {e}")
            self.camera = None
            print("[INFO] DXcam 已释放")
