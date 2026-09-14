# -*- coding: utf-8 -*-
"""Gemini 多模态视觉客户端封装（OpenAI 兼容中转站）。

支持单张图片识别、多帧图片序列理解（用于动态视觉）。
使用 OpenAI 兼容接口 /v1/chat/completions，可对接中转站或官方网关。
"""
import base64
import io
from typing import List

import httpx
from PIL import Image

import config


class GeminiClient:
    def __init__(self, api_key: str = None, model: str = None):
        self.api_key = api_key or getattr(config, "GEMINI_API_KEY", "")
        self.model = model or getattr(config, "GEMINI_MODEL", "gemini-3.7-flash")
        # 默认走中转站，可被 config.GEMINI_BASE_URL 覆盖
        self.base_url = getattr(config, "GEMINI_BASE_URL", "https://api.openclawplan.com").rstrip("/")

    def _resize_frame(self, image_bytes: bytes, max_size: int = 1280) -> bytes:
        """把图片等比缩放到最大边不超过 max_size，减少 token 消耗。"""
        try:
            img = Image.open(io.BytesIO(image_bytes))
            img = img.convert("RGB")
            w, h = img.size
            if max(w, h) > max_size:
                ratio = max_size / max(w, h)
                new_size = (int(w * ratio), int(h * ratio))
                img = img.resize(new_size, Image.Resampling.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=85)
            return buf.getvalue()
        except Exception as e:
            print(f"[WARN] 图片压缩失败，使用原图: {e}")
            return image_bytes

    def _build_image_part(self, image_bytes: bytes) -> dict:
        """把图片字节转成 OpenAI 兼容的 image_url 格式。"""
        image_bytes = self._resize_frame(image_bytes)
        b64 = base64.b64encode(image_bytes).decode("utf-8")
        return {
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
        }

    async def describe_frames(
        self,
        frames: List[bytes],
        prompt: str = "",
        max_output_tokens: int = 512,
    ) -> str:
        """理解多帧画面序列，返回对动态场景的简要描述。

        frames: 图片字节列表，按时间顺序排列。
        prompt: 额外的文字提示，例如"描述当前屏幕里正在发生什么"。
        """
        if not frames:
            return ""

        if not self.api_key:
            raise RuntimeError("未配置 GEMINI_API_KEY")

        text = prompt.strip()
        if not text:
            text = (
                "这是按时间顺序排列的多帧画面。请简要描述："
                "1) 画面主体是什么；2) 主体正在做什么动作或发生了什么变化；"
                "3) 画面上的文字（如有）；4) 整体场景。"
                "控制在 150 字以内，用中文回答。"
            )

        # 构造 OpenAI 兼容的 content 数组：先文字，再图片
        content = [{"type": "text", "text": text}]
        for frame in frames:
            content.append(self._build_image_part(frame))

        url = f"{self.base_url}/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": content}],
            "max_tokens": max_output_tokens,
            "temperature": 0.4,
        }

        async with httpx.AsyncClient(timeout=120, trust_env=False) as client:
            resp = await client.post(url, json=payload, headers=headers)
            if resp.status_code != 200:
                raise RuntimeError(f"Gemini API 错误 {resp.status_code}: {resp.text[:400]}")
            data = resp.json()

        choices = data.get("choices", [])
        if not choices:
            raise RuntimeError("Gemini API 返回空 choices")

        message = choices[0].get("message", {})
        return message.get("content", "").strip()

    async def describe_image(self, image_bytes: bytes, prompt: str = "") -> str:
        """单张图片识别，兼容现有图片识别接口。"""
        return await self.describe_frames([image_bytes], prompt=prompt)

    async def chat_text(self, text: str, max_output_tokens: int = 800) -> str:
        """纯文本对话（不带图片），用于视频分段后的总结合并。"""
        if not self.api_key:
            raise RuntimeError("未配置 GEMINI_API_KEY")

        url = f"{self.base_url}/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": text}],
            "max_tokens": max_output_tokens,
            "temperature": 0.4,
        }

        async with httpx.AsyncClient(timeout=120, trust_env=False) as client:
            resp = await client.post(url, json=payload, headers=headers)
            if resp.status_code != 200:
                raise RuntimeError(f"Gemini API 错误 {resp.status_code}: {resp.text[:400]}")
            data = resp.json()

        choices = data.get("choices", [])
        if not choices:
            raise RuntimeError("Gemini API 返回空 choices")
        message = choices[0].get("message", {})
        return message.get("content", "").strip()
