# -*- coding: utf-8 -*-
"""联网搜索 + 网页解析工具。

- 实时搜索：用 GLM 的 web_search 工具（glm-4 系列支持），零额外成本
- 链接解析：抓取网页正文，交给 DeepSeek 总结
"""
import re

import httpx

import config


# ---------------------------------------------------------------
# 一、实时搜索（DeepSeek Responses API 的 web_search 工具）
# ---------------------------------------------------------------
async def search_web(query: str) -> str:
    """用 DeepSeek 联网搜索并返回融合后的回答。

    使用 deepseek-v4-flash + Responses API 的 web_search 工具。
    """
    url = f"{config.DEEPSEEK_BASE_URL.rstrip('/')}/responses"
    headers = {
        "Authorization": f"Bearer {config.DEEPSEEK_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": "deepseek-v4-flash",
        "input": query,
        "tools": [{"type": "web_search"}],
        "tool_choice": "auto",
    }
    async with httpx.AsyncClient(timeout=180) as client:
        resp = await client.post(url, json=payload, headers=headers)
        if resp.status_code != 200:
            raise RuntimeError(f"搜索失败 {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        text = extract_output_text(data)
        if not text:
            raise RuntimeError("搜索未返回内容")
        return text


def extract_output_text(data: dict) -> str:
    """从 DeepSeek Responses API 响应中提取最终回答文本。"""
    # 方式1：直接 output_text 字段
    if data.get("output_text"):
        return data["output_text"]
    # 方式2：遍历 output 数组，找 message 类型的 content
    for item in data.get("output", []):
        if item.get("type") == "message":
            for c in item.get("content", []):
                if c.get("type") in ("output_text", "text"):
                    return c.get("text", "")
    return ""


# ---------------------------------------------------------------
# 二、网页链接解析
# ---------------------------------------------------------------
URL_RE = re.compile(r'https?://[^\s<>"\']+')


def extract_urls(text: str) -> list:
    """从文本中提取所有 URL。"""
    return URL_RE.findall(text)


async def fetch_webpage(url: str) -> str:
    """抓取网页并提取正文文本（简单版，去除 HTML 标签）。"""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    }
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        resp = await client.get(url, headers=headers)
        if resp.status_code != 200:
            raise RuntimeError(f"抓取网页失败 {resp.status_code}")
        html = resp.text

    return html_to_text(html)


def html_to_text(html: str) -> str:
    """把 HTML 转成纯文本（去脚本、样式、标签）。"""
    # 去掉 script/style 内容
    html = re.sub(r'(?is)<(script|style|noscript)[^>]*>.*?</\1>', ' ', html)
    # 去掉标签，换成空格/换行
    html = re.sub(r'(?is)<br\s*/?>', '\n', html)
    html = re.sub(r'(?is)</p>', '\n', html)
    html = re.sub(r'(?is)<[^>]+>', ' ', html)
    # 解码常见实体
    html = html.replace('&nbsp;', ' ').replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>').replace('&quot;', '"')
    # 压缩空白
    html = re.sub(r'[ \t]+', ' ', html)
    html = re.sub(r'\n\s*\n+', '\n\n', html)
    return html.strip()
