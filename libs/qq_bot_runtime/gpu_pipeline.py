# -*- coding: utf-8 -*-
"""GPU 流水线辅助：把 numpy 帧批量上传到 CUDA，并做预处理。

适配 RTX 5060：支持 CUDA + FP16，可配合 TensorRT/ONNXRuntime 使用。
当前只提供预处理占位；真正接入本地 3D CNN 时可复用这些函数。
"""
from typing import List, Tuple

import numpy as np
import torch
import torchvision.transforms as T


class GPUPipeline:
    def __init__(self, device: str = "cuda", dtype: torch.dtype = torch.float16):
        if not torch.cuda.is_available():
            device = "cpu"
            dtype = torch.float32
        self.device = device
        self.dtype = dtype

        # 标准 ImageNet 归一化
        self.normalize = T.Normalize(
            mean=[0.485, 0.456, 0.406],
            std=[0.229, 0.224, 0.225],
        )

    def upload_batch(
        self,
        frames: List[np.ndarray],
        size: Tuple[int, int] = (224, 224),
    ) -> torch.Tensor:
        """把 list[np.ndarray] RGB 转成 GPU 上的 (B, C, T, H, W) 视频 clip。

        frames: list of RGB numpy 数组，每帧 (H, W, 3)
        size: 模型输入尺寸
        """
        tensors = []
        for frame in frames:
            # (H, W, 3) float32 in [0,1]
            t = torch.from_numpy(frame).permute(2, 0, 1).float() / 255.0
            # resize
            t = torch.nn.functional.interpolate(
                t.unsqueeze(0),
                size=size,
                mode="bilinear",
                align_corners=False,
            ).squeeze(0)
            t = self.normalize(t)
            tensors.append(t)

        # (T, C, H, W) -> (C, T, H, W)
        clip = torch.stack(tensors, dim=1)
        # (1, C, T, H, W)
        clip = clip.unsqueeze(0).to(device=self.device, dtype=self.dtype)
        return clip

    def to_onnx_input(
        self,
        frames: List[np.ndarray],
        size: Tuple[int, int] = (224, 224),
    ) -> np.ndarray:
        """把帧转成 ONNX Runtime 可接受的 numpy 输入 (1, C, T, H, W) float32。"""
        batch = self.upload_batch(frames, size).float().cpu().numpy()
        return batch


# 快捷函数：如果后续接入 TensorRT，可在这里扩展 INT8/FP16 context 推理
