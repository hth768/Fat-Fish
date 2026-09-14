# -*- coding: utf-8 -*-
"""TensorRT 推理占位示例（针对 RTX 5060）。

环境准备：
    pip install tensorrt
    或安装 NVIDIA TensorRT wheel

本文件不直接参与 QQ bot 运行，仅作为你后续把本地 3D CNN 接入时的参考模板。
如果你现在只是用 Gemini 云端理解，可以忽略这个文件。
"""
from typing import List

import numpy as np
import torch

try:
    import tensorrt as trt
    import pycuda.driver as cuda
    import pycuda.autoinit
    HAS_TRT = True
except ImportError:
    HAS_TRT = False
    print("[WARN] TensorRT / pycuda 未安装，tensorrt_example.py 仅作占位")


class TensorRTInferencer:
    def __init__(self, engine_path: str):
        if not HAS_TRT:
            raise RuntimeError("TensorRT 未安装")
        self.engine_path = engine_path
        self.logger = trt.Logger(trt.Logger.WARNING)
        self.engine = self._load_engine()
        self.context = self.engine.create_execution_context()
        self.stream = cuda.Stream()
        self._allocate_buffers()

    def _load_engine(self):
        with open(self.engine_path, "rb") as f, trt.Runtime(self.logger) as runtime:
            return runtime.deserialize_cuda_engine(f.read())

    def _allocate_buffers(self):
        """分配 GPU 输入输出缓冲区。"""
        self.inputs = []
        self.outputs = []
        self.bindings = []
        for i in range(self.engine.num_io_tensors):
            name = self.engine.get_tensor_name(i)
            mode = self.engine.get_tensor_mode(name)
            shape = self.engine.get_tensor_shape(name)
            dtype = trt.nptype(self.engine.get_tensor_dtype(name))
            size = trt.volume(shape)
            if mode == trt.TensorIOMode.INPUT:
                # 输入绑定到 GPU
                mem = cuda.mem_alloc(size * np.dtype(dtype).itemsize)
                self.bindings.append(int(mem))
                self.inputs.append({"name": name, "mem": mem, "shape": shape, "dtype": dtype})
            else:
                # 输出绑定到 GPU，再拷贝回 CPU
                mem = cuda.mem_alloc(size * np.dtype(dtype).itemsize)
                self.bindings.append(int(mem))
                self.outputs.append({"name": name, "mem": mem, "shape": shape, "dtype": dtype})

    def infer(self, input_np: np.ndarray) -> np.ndarray:
        """执行一次推理，输入输出都是 numpy。"""
        # 上传输入
        cuda.memcpy_htod_async(
            self.inputs[0]["mem"], input_np.ravel(), self.stream.handle
        )
        # 执行
        self.context.execute_async_v3(stream_handle=self.stream.handle)
        # 下载输出
        out = np.empty(trt.volume(self.outputs[0]["shape"]), dtype=self.outputs[0]["dtype"])
        cuda.memcpy_dtoh_async(out, self.outputs[0]["mem"], self.stream.handle)
        self.stream.synchronize()
        return out


# 生成 FP16 TensorRT engine 的占位函数（需要先有 ONNX 模型）
def build_fp16_engine(onnx_path: str, engine_path: str):
    """把 ONNX 模型导出为 FP16 TensorRT engine。"""
    if not HAS_TRT:
        raise RuntimeError("TensorRT 未安装")
    logger = trt.Logger(trt.Logger.WARNING)
    builder = trt.Builder(logger)
    network = builder.create_network(
        1 << int(trt.NetworkDefinitionCreationFlag.EXPLICIT_BATCH)
    )
    parser = trt.OnnxParser(network, logger)
    with open(onnx_path, "rb") as f:
        parser.parse(f.read())

    config = builder.create_builder_config()
    config.set_memory_pool_limit(trt.MemoryPoolType.WORKSPACE, 1 << 30)
    config.set_flag(trt.BuilderFlag.FP16)

    profile = builder.create_optimization_profile()
    input_name = network.get_input(0).name
    profile.set_shape(input_name, (1, 3, 8, 224, 224), (1, 3, 16, 224, 224), (1, 3, 32, 224, 224))
    config.add_optimization_profile(profile)

    engine = builder.build_engine(network, config)
    with open(engine_path, "wb") as f:
        f.write(engine.serialize())
    print(f"[INFO] TensorRT FP16 engine 已保存到 {engine_path}")
