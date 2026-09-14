# -*- coding: utf-8 -*-
"""DeepSeek 客户端封装（OpenAI 兼容接口）。"""
import httpx

import config


class DeepSeekClient:
    def __init__(self):
        self.api_key = config.DEEPSEEK_API_KEY
        self.base_url = config.DEEPSEEK_BASE_URL.rstrip("/")
        self.model = config.DEEPSEEK_MODEL

    async def chat(self, messages: list[dict], model: str = None, think: bool = False) -> str:
        """messages 为 [{"role": "system"/"user"/"assistant", "content": "..."}]

        model 可指定，默认用 config 里的 DEEPSEEK_MODEL。
        think=False 时关闭思考（thinking: disabled），回复更快更口语化；
        think=True 时开启深度思考（用于 /思考 等需要推理的场景）。
        """
        use_model = model or self.model
        url = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": use_model,
            "messages": messages,
            "stream": False,
        }
        # 控制思考：日常对话关闭思考（更自然、更快），需要推理时开启
        if think:
            payload["thinking"] = {"type": "enabled"}
        else:
            payload["thinking"] = {"type": "disabled"}
        async with httpx.AsyncClient(timeout=300, trust_env=False) as client:
            resp = await client.post(url, json=payload, headers=headers)
            if resp.status_code != 200:
                raise RuntimeError(f"DeepSeek API 错误 {resp.status_code}: {resp.text[:300]}")
            data = resp.json()
            return data["choices"][0]["message"]["content"]

    async def chat_with_tools(self, messages: list[dict], tools: list[dict] = None,
                              model: str = None, think: bool = False) -> dict:
        """带工具调用的对话（OpenAI 兼容 function calling）。

        messages 遵循 OpenAI 格式：assistant 消息可含 tool_calls，
        工具结果用 {"role": "tool", "tool_call_id": ..., "content": "..."}。
        返回完整的 assistant message dict（含 content 和/或 tool_calls），
        便于原样追加回 messages 继续多轮工具循环。
        """
        use_model = model or self.model
        url = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": use_model,
            "messages": messages,
            "stream": False,
        }
        if tools:
            payload["tools"] = tools
        if think:
            payload["thinking"] = {"type": "enabled"}
        else:
            payload["thinking"] = {"type": "disabled"}
        async with httpx.AsyncClient(timeout=300, trust_env=False) as client:
            resp = await client.post(url, json=payload, headers=headers)
            if resp.status_code != 200:
                raise RuntimeError(f"DeepSeek API 错误 {resp.status_code}: {resp.text[:300]}")
            data = resp.json()
            msg = data["choices"][0]["message"]
            # 归一化：content 缺省为空串；空 tool_calls 必须剥掉——
            # 协议要求带 tool_calls 字段就必须至少 1 项，空数组会被 API 400 拒绝
            if msg.get("content") is None:
                msg["content"] = ""
            if not msg.get("tool_calls"):
                msg.pop("tool_calls", None)
            # 思考模式的 reasoning_content 不允许回传进下一轮请求，剥掉
            msg.pop("reasoning_content", None)
            return msg
