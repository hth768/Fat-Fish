# -*- coding: utf-8 -*-
"""本地视频理解模块（Qwen2.5-VL-3B-Instruct）。

用 RTX 5060 本地跑轻量视觉大模型，实现视频/多帧画面的理解，
摆脱云端 API 的延迟和费用。牺牲画质（低分辨率）换取高帧率。

特性：
- 支持多帧图片输入，理解时间序列动作
- FP16 + GPU 推理
- 模型懒加载（首次调用时才加载，避免占用显存）
- 与 gemini_client.describe_frames 接口对齐，方便无缝切换

依赖：
    pip install transformers accelerate qwen-vl-utils

模型下载（首次运行会自动从 HuggingFace 下载，或手动指定本地路径）：
    Qwen/Qwen2.5-VL-3B-Instruct
"""
import asyncio
import io
import os
import threading
from typing import List, Optional

import config

# 必须在导入 transformers 之前设置这些环境变量：
# huggingface_hub 在导入时就会读取并冻结缓存目录（HF_HUB_CACHE），
# 之后再用 setdefault 不会生效，会导致模型在 E 盘的缓存找不到。
# 用直接赋值保证 config 里的值始终优先。
if getattr(config, "HF_ENDPOINT", ""):
    os.environ["HF_ENDPOINT"] = config.HF_ENDPOINT
if getattr(config, "HF_HOME", ""):
    os.environ["HF_HOME"] = config.HF_HOME
    os.environ["HF_HUB_CACHE"] = config.HF_HOME + "/hub"
# 禁用 Xet 协议（hf-mirror 不完全支持）
os.environ["HF_HUB_DISABLE_XET"] = "1"

import torch
from PIL import Image
from transformers import Qwen2_5_VLForConditionalGeneration, AutoProcessor


class LocalVideoUnderstand:
    def __init__(self, model_name: str = None, device: str = None):
        self.model_name = model_name or getattr(
            config, "LOCAL_VL_MODEL", "Qwen/Qwen2.5-VL-3B-Instruct"
        )
        if device is None:
            device = "cuda" if torch.cuda.is_available() else "cpu"
        self.device = device

        self.model = None
        self.processor = None
        self._load_lock = threading.Lock()

    def is_loaded(self) -> bool:
        return self.model is not None

    def load(self):
        """加载模型（线程安全，只加载一次）。

        8GB 显存跑 3B FP16 会 OOM，改用 4bit 量化（bitsandbytes），
        显存降到约 3GB，给多帧推理留出空间。
        """
        if self.model is not None:
            return
        with self._load_lock:
            if self.model is not None:
                return
            print(f"[INFO] 正在加载本地模型 {self.model_name} (4bit 量化) ...")

            if self.device == "cuda":
                # 4bit 量化加载，用 low_cpu_mem_usage 减少系统内存占用
                from transformers import BitsAndBytesConfig
                quant_config = BitsAndBytesConfig(
                    load_in_4bit=True,
                    bnb_4bit_quant_type="nf4",
                    bnb_4bit_compute_dtype=torch.float16,
                    bnb_4bit_use_double_quant=True,
                )
                self.model = Qwen2_5_VLForConditionalGeneration.from_pretrained(
                    self.model_name,
                    quantization_config=quant_config,
                    device_map="auto",
                    local_files_only=True,
                    low_cpu_mem_usage=True,
                )
            else:
                self.model = Qwen2_5_VLForConditionalGeneration.from_pretrained(
                    self.model_name,
                    torch_dtype=torch.float32,
                    local_files_only=True,
                    low_cpu_mem_usage=True,
                ).to(self.device)

            self.processor = AutoProcessor.from_pretrained(
                self.model_name, local_files_only=True
            )
            self.model.eval()
            print("[INFO] 本地模型加载完成")

    def unload(self):
        """释放模型显存。"""
        if self.model is not None:
            del self.model
            self.model = None
            self.processor = None
            if self.device == "cuda":
                torch.cuda.empty_cache()
            print("[INFO] 本地模型已释放")

    @torch.no_grad()
    def _infer_sync(self, frames: List[bytes], prompt: str, max_tokens: int) -> str:
        """同步推理（在独立线程里调用）。"""
        if self.model is None:
            self.load()

        # bytes -> PIL Images
        images = [Image.open(io.BytesIO(f)).convert("RGB") for f in frames]

        # 构造消息：文本 + 多张图
        content = [{"type": "text", "text": prompt}]
        for img in images:
            content.append({"type": "image", "image": img})

        messages = [{"role": "user", "content": content}]

        # 处理输入
        text = self.processor.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=True
        )
        inputs = self.processor(
            text=[text],
            images=images,
            return_tensors="pt",
        ).to(self.device)

        try:
            # 生成
            generated_ids = self.model.generate(
                **inputs,
                max_new_tokens=max_tokens,
                do_sample=False,
            )

            # 去掉输入部分，只保留生成部分
            input_len = inputs.input_ids.shape[1]
            output_ids = generated_ids[0][input_len:]
            result = self.processor.decode(output_ids, skip_special_tokens=True)
            return result.strip()
        finally:
            # 释放中间变量，清理显存缓存，避免多段推理累积 OOM
            del inputs, images
            if self.device == "cuda":
                torch.cuda.empty_cache()

    async def describe_frames(
        self,
        frames: List[bytes],
        prompt: str = "",
        max_output_tokens: int = 256,
    ) -> str:
        """多帧画面理解（异步接口，与 gemini_client 对齐）。"""
        if not frames:
            return ""

        if not prompt:
            prompt = (
                "这是按时间顺序排列的多帧画面。请简要描述："
                "画面里主要有什么、正在发生什么变化或动作、画面上有什么文字。"
                "控制在 120 字以内，用中文回答。"
            )

        loop = asyncio.get_running_loop()
        # 推理是阻塞的，放到线程池执行，避免阻塞事件循环
        return await loop.run_in_executor(
            None, self._infer_sync, frames, prompt, max_output_tokens
        )

    async def describe_video(
        self,
        frames: List[bytes],
        max_output_tokens: int = 256,
    ) -> str:
        """视频帧序列理解。"""
        prompt = (
            "这是一段视频按时间顺序抽取的关键帧。请描述视频内容："
            "1) 视频里发生了什么；2) 主要人物/物体是谁、在做什么动作；"
            "3) 场景和背景；4) 画面上有什么文字（如有）。"
            "控制在 200 字以内，用中文回答。"
        )
        return await self.describe_frames(frames, prompt=prompt, max_output_tokens=max_output_tokens)

    @torch.no_grad()
    def _chat_sync(self, text: str, max_tokens: int) -> str:
        """纯文本对话（同步）。"""
        if self.model is None:
            self.load()
        messages = [{"role": "user", "content": text}]
        text_input = self.processor.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=True
        )
        inputs = self.processor(text=[text_input], return_tensors="pt").to(self.device)
        try:
            generated_ids = self.model.generate(
                **inputs, max_new_tokens=max_tokens, do_sample=False
            )
            input_len = inputs.input_ids.shape[1]
            output_ids = generated_ids[0][input_len:]
            return self.processor.decode(output_ids, skip_special_tokens=True).strip()
        finally:
            del inputs
            if self.device == "cuda":
                torch.cuda.empty_cache()

    async def chat_text(self, text: str, max_output_tokens: int = 512) -> str:
        """纯文本对话（异步接口）。"""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._chat_sync, text, max_output_tokens)


# 全局单例
local_vl = LocalVideoUnderstand()
