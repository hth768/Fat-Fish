# -*- coding: utf-8 -*-
"""GLM-4V 视觉识别客户端封装（智谱 AI，OpenAI 兼容接口）。"""
import base64
import io

import httpx

import config


def detect_image_type(data: bytes) -> str:
    """根据文件头魔数判断图片类型。返回 'jpeg'/'png'/'gif'/'webp'/未知。"""
    if data[:2] == b'\xff\xd8':
        return "jpeg"
    if data[:8] == b'\x89PNG\r\n\x1a\n':
        return "png"
    if data[:6] in (b'GIF87a', b'GIF89a'):
        return "gif"
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return "webp"
    return "unknown"


def gif_first_frame_to_jpeg(gif_bytes: bytes) -> bytes:
    """把 GIF 第一帧提取出来，转成 JPEG 字节。需要 PIL。"""
    from PIL import Image
    img = Image.open(io.BytesIO(gif_bytes))
    img.seek(0)  # 第一帧
    # 转 RGB（GIF 可能是调色板模式）
    img = img.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


def gif_extract_frames(gif_bytes: bytes, max_frames: int = 4) -> list:
    """从 GIF 中均匀抽取多帧，返回 [jpeg_bytes, ...]。

    max_frames: 最多抽取多少帧。动画越长，帧数越多。
    """
    from PIL import Image
    img = Image.open(io.BytesIO(gif_bytes))
    total = getattr(img, "n_frames", 1)
    if total <= 1:
        # 静态 GIF，只返回第一帧
        return [gif_first_frame_to_jpeg(gif_bytes)]

    # 计算要抽取的帧索引（均匀采样，含首帧和末帧）
    n = min(max_frames, total)
    if n == 1:
        indices = [0]
    else:
        indices = sorted({int(i * (total - 1) / (n - 1)) for i in range(n)})

    frames = []
    for idx in indices:
        img.seek(idx)
        frame = img.convert("RGB")
        buf = io.BytesIO()
        frame.save(buf, format="JPEG", quality=85)
        frames.append(buf.getvalue())
    return frames


class GLMClient:
    def __init__(self, api_key: str = None, base_url: str = None, model: str = None):
        # 凭据/模型/端点统一从外部（ai_providers.json 覆盖层）注入；
        # 仅当未传时才回落 config 默认值，避免把视觉模型写死到某个供应商。
        self.api_key = api_key or getattr(config, "DEEPSEEK_API_KEY", "")
        self.base_url = (base_url or getattr(config, "DEEPSEEK_BASE_URL",
                                            "https://api.deepseek.com")).rstrip("/")
        self.model = model or getattr(config, "VISION_MODEL", "deepseek-flash")

    async def describe_image(self, image_bytes: bytes, prompt: str = "") -> str:
        """识别图片内容。

        image_bytes: 图片二进制数据
        prompt: 用户附加的文字（如"这张图里有什么？"），可为空
        """
        if prompt.strip():
            text = prompt.strip()
        else:
            text = (
                "请非常详细地描述这张图片，包括：1) 画面主体（人物/动物/物体，长什么样，"
                "颜色、大小、位置）；2) 主体正在做的动作或表情；3) 背景环境；"
                "4) 图中出现的任何文字或配字（逐字读出）；5) 整体氛围和色调。"
                "请用自然流畅的语言描述，尽量具体。"
            )
        return await self._vision(image_bytes, text)

    async def recognize_emotion(self, image_bytes: bytes) -> str:
        """识别图片表达的情绪（用于表情包收集）。

        返回一个简短的情绪词，如：开心、生气、无语、委屈、得意、卖萌、惊讶、无奈 等。
        """
        prompt = "这是一张表情包图片。请用 1-3 个词概括它表达的情绪（如：开心、生气、无语、委屈、得意、卖萌、惊讶、无奈、白眼、点赞等）。只回答情绪词，不要其他内容。"
        result = await self._vision(image_bytes, prompt)
        return result.strip()

    async def describe_emoji(self, image_bytes: bytes) -> str:
        """详细描述表情包的画面内容（用于表情包收集时的 desc 字段）。"""
        prompt = (
            "这是一张表情包图片。请详细描述画面内容，包括："
            "1) 主体是什么（人物/动物/表情等，长什么样）；"
            "2) 主体在做什么动作、什么表情；"
            "3) 图片上的文字或配字（如有，请逐字写出）；"
            "4) 想表达的情绪或梗。"
            "控制在 50 字以内，用一句话描述清楚。"
        )
        result = await self._vision(image_bytes, prompt)
        return result.strip()

    async def describe_gif_animation(self, image_bytes: bytes) -> str:
        """理解 GIF 动画：抽多帧逐帧识别，返回动画过程描述。

        返回如"一只猫从左边走到右边，然后坐下"这样的动画过程描述。
        """
        frames = gif_extract_frames(image_bytes, max_frames=4)
        if len(frames) <= 1:
            # 不是动画，按普通图片描述
            return await self.describe_image(image_bytes)

        # 逐帧识别
        frame_descs = []
        for i, frame in enumerate(frames):
            prompt = (
                f"这是动画的第 {i+1}/{len(frames)} 帧。请描述这一帧的画面，"
                "包括：主体是谁、在做什么动作、表情神态、以及画面上的文字。"
            )
            try:
                desc = await self._vision(frame, prompt)
                frame_descs.append(f"第{i+1}帧：{desc.strip()}")
            except Exception as e:
                print(f"[WARN] GIF 第{i+1}帧识别失败: {e}")
                frame_descs.append(f"第{i+1}帧：（识别失败）")

        # 汇总成动画过程描述
        combined = "\n".join(frame_descs)
        return f"[GIF动画过程]\n{combined}"

    async def _post_vision(self, content: list, max_output_tokens: int = 0) -> str:
        """向视觉接口发送通用 content（多图 / 单图 / 纯文本均可）。"""
        url = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": content,
                }
            ],
            "stream": False,
        }
        if max_output_tokens and max_output_tokens > 0:
            payload["max_tokens"] = max_output_tokens
        async with httpx.AsyncClient(timeout=120, trust_env=False) as client:
            resp = await client.post(url, json=payload, headers=headers)
            if resp.status_code != 200:
                raise RuntimeError(f"视觉识别 API 错误 {resp.status_code}: {resp.text[:300]}")
            data = resp.json()
            return data["choices"][0]["message"]["content"]

    async def _vision(self, image_bytes: bytes, text: str) -> str:
        """内部通用视觉请求（单张图片）。

        自动处理图片格式：GIF 提取第一帧转 JPEG，其余格式直接 base64。
        """
        # 预处理：GIF 提取第一帧，统一转成静态图
        img_type = detect_image_type(image_bytes)
        mime = "image/jpeg"
        try:
            if img_type == "gif":
                image_bytes = gif_first_frame_to_jpeg(image_bytes)
                mime = "image/jpeg"
            elif img_type == "png":
                mime = "image/png"
            elif img_type == "webp":
                mime = "image/webp"
            else:
                mime = "image/jpeg"
        except Exception as e:
            # GIF 提取失败则原样传（可能仍是 JPEG）
            print(f"[WARN] 图片预处理失败，按原样发送: {e}")

        b64 = base64.b64encode(image_bytes).decode("utf-8")
        data_url = f"data:{mime};base64,{b64}"

        # 构造视觉请求内容：GLM-4V 要求 image_url 为 data URI 格式，
        # 且必须至少包含一个 text 段，否则报"未正常接收到prompt参数"
        content = [
            {"type": "image_url", "image_url": {"url": data_url}},
            {"type": "text", "text": text},
        ]
        return await self._post_vision(content)

    async def describe_frames(self, frames, prompt: str = "",
                             max_output_tokens: int = 512) -> str:
        """理解多帧（视频）画面。frames: list[bytes]，每项为单帧 JPEG 等图片。

        视频理解链路（video_processor.describe_video）在本地 Qwen-VL 不可用时会
        回退到云端视觉，而云端 frames 路由只挂了 glm/gemini——gemini 无 key、
        glm 此前未实现该方法，导致视频帧无人处理。这里把多帧作为多张图片一次性
        发给视觉模型（OpenAI 兼容接口支持多 image_url），从而让已配好的
        DeepSeek 视觉模型（config.VISION_MODEL）也能理解视频。
        """
        if not frames:
            return "（没有可识别的视频帧）"
        # 单段帧数过多会把视觉模型（如 deepseek-flash）拖垮或超时。
        # 均匀抽稀到最多 16 帧，兼顾信息量与请求稳定性。
        max_frames_once = 16
        if len(frames) > max_frames_once:
            step = len(frames) / max_frames_once
            kept = [frames[int(i * step)] for i in range(max_frames_once)]
            frames = kept
        text = prompt or (
            "这是一段视频抽取出的若干帧画面（按时间顺序）。请综合这些画面用中文描述"
            "视频内容：1) 视频里发生了什么、主体是谁、在做什么动作；2) 场景与背景；"
            "3) 画面中出现的任何文字或字幕（逐字读出）；4) 整体想表达的意思。尽量具体。"
        )
        content = []
        for fb in frames:
            it = detect_image_type(fb)
            mime = {
                "jpeg": "image/jpeg", "png": "image/png",
                "gif": "image/gif", "webp": "image/webp",
            }.get(it, "image/jpeg")
            b64 = base64.b64encode(fb).decode("utf-8")
            content.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})
        content.append({"type": "text", "text": text})
        return await self._post_vision(content, max_output_tokens=max_output_tokens)

    async def chat_text(self, text: str, max_output_tokens: int = 800) -> str:
        """纯文本总结（视觉相关文本任务兜底，如云端视频理解二次汇总）。"""
        return await self._post_vision(
            [{"type": "text", "text": text}], max_output_tokens=max_output_tokens)
