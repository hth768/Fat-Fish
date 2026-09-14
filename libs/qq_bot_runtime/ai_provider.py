# -*- coding: utf-8 -*-
"""统一 AI 供应商抽象层（对齐 N.E.K.O 的 14+ providers 多供应商架构）。

设计目标：「各功能都可以接入不同供应商」+ 故障转移。

核心概念：
    - Provider（供应商）：一个 OpenAI 兼容的 /chat/completions 端点（DeepSeek / Gemini /
      Zhipu(GLM) / OpenAI / Moonshot(Kimi) / Qwen(通义) / Ollama 本地 …）。在 config.AI_PROVIDERS
      注册，只有填了 api_key 的才会被启用。
    - Capability（能力）：一个功能维度，如 chat(日常对话) / reasoning(深度推理) /
      vision(视觉理解) / tools(工具调用)。
    - Routing（路由）：config.AI_CAPABILITY_ROUTING 把每个能力映射到「有序供应商列表」；
      调用时按优先级尝试，某供应商报错会自动故障转移到下一个，全部失败才向上抛错。

统一调用接口：
    llm = get_llm()
    text = await llm.chat(messages, capability="chat")          # 文本
    msg  = await llm.chat(messages, capability="tools", tools=...)  # 带工具，返回消息 dict
    text = await llm.chat(messages, capability="vision", images=[bytes])  # 视觉
    text = await llm.chat(messages, capability="reasoning", think=True)  # 深度推理

视觉与现有 glm_client / gemini_client 的差异：本层做「单张图片」通用路由（最适合路由切换）；
GIF 多帧、视频帧序列等高级视觉仍由各自专业客户端处理（不在此层，避免质量回退）。
"""
import base64
import json
import os
import time
from typing import List, Optional

import time_context  # 统一时间上下文：发消息时把当前时间注入 LLM

import httpx

import config
import telemetry

# ============ 配置中心（对标 N.E.K.O config/api_providers.json） ============
# AI_PROVIDERS / AI_CAPABILITY_ROUTING / AI_VISION_ROUTING / AI_ROLE_ROUTING
# 优先从 ai_providers.json 读取（可编辑中心，改完热重载无需重启）；
# 文件不存在时回落到 config.py 里的内建默认值（零回归）。
# 缓存按文件 mtime 失效，reload_provider_config() 可强制刷新。

_PROVIDER_JSON = getattr(
    config, "AI_PROVIDERS_JSON_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "ai_providers.json"),
)
_cache = {"mtime": None, "data": None}


def _resolve_env(value):
    """递归把字符串里的 ${ENV_VAR} 解析为环境变量值。"""
    if isinstance(value, str):
        if value.startswith("${") and value.endswith("}"):
            return os.environ.get(value[2:-1], "")
        return value
    if isinstance(value, dict):
        return {k: _resolve_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_env(v) for v in value]
    return value


def load_provider_config(reset_cache: bool = False) -> dict:
    """返回合并后的配置中心：providers / capability_routing / vision_routing / role_routing。

    ai_providers.json 作为【覆盖层】存在时生效，且只覆盖写明的键：
      - AI_PROVIDERS：按供应商名逐层合并（可只改某家的 api_key / 某个 model）
      - 各 *_ROUTING：按 entry 合并（可只改某个能力/任务的顺序）
    文件不存在时完全回落 config.py 默认值（零回归）。
    """
    global _cache
    try:
        mtime = os.path.getmtime(_PROVIDER_JSON) if os.path.exists(_PROVIDER_JSON) else 0
    except OSError:
        mtime = 0
    if not reset_cache and _cache["data"] is not None and _cache["mtime"] == mtime:
        return _cache["data"]

    defaults = {
        "providers": dict(getattr(config, "AI_PROVIDERS", {}) or {}),
        "capability_routing": dict(getattr(config, "AI_CAPABILITY_ROUTING", {}) or {}),
        "vision_routing": dict(getattr(config, "AI_VISION_ROUTING", {}) or {}),
        "role_routing": dict(getattr(config, "AI_ROLE_ROUTING", {}) or {}),
    }
    if os.path.exists(_PROVIDER_JSON):
        try:
            with open(_PROVIDER_JSON, "r", encoding="utf-8") as f:
                j = _resolve_env(json.load(f))
            if isinstance(j, dict):
                # AI_PROVIDERS：逐供应商合并
                for name, prov in (j.get("AI_PROVIDERS") or {}).items():
                    base = dict(defaults["providers"].get(name, {}))
                    base.update(prov or {})
                    defaults["providers"][name] = base
                # 路由：按 entry 合并
                _route_map = {
                    "AI_CAPABILITY_ROUTING": "capability_routing",
                    "AI_VISION_ROUTING": "vision_routing",
                    "AI_ROLE_ROUTING": "role_routing",
                }
                for jk, dk in _route_map.items():
                    if j.get(jk) is not None:
                        defaults[dk].update(j[jk])
        except Exception as e:
            print(f"[CONFIG] 读取 {_PROVIDER_JSON} 失败，回落 config.py 默认值: {e}")
    # 规整：保证四个键都有 dict 值
    for k in ("providers", "capability_routing", "vision_routing", "role_routing"):
        if not isinstance(defaults.get(k), dict):
            defaults[k] = {}
    _cache = {"mtime": mtime, "data": defaults}
    return defaults


def reload_provider_config() -> dict:
    """运行时热重载整个配置中心（改完 ai_providers.json 后调用）。"""
    cfg = load_provider_config(reset_cache=True)
    try:
        get_llm().reload()
    except Exception as e:
        print(f"[CONFIG] reload LLM 失败: {e}")
    try:
        get_vision().reload()
    except Exception as e:
        print(f"[CONFIG] reload Vision 失败: {e}")
    return cfg


class ProviderError(Exception):
    """所有供应商均不可用。"""


class UnifiedLLM:
    def __init__(self):
        cfg = load_provider_config()
        # 只保留配置了 api_key 的供应商
        self.providers = {
            name: p
            for name, p in cfg["providers"].items()
            if p.get("api_key")
        }
        self.routing = cfg["capability_routing"]
        # 遥测：每个供应商累计 调用次数/成功/失败/延迟/Token/最近错误
        # （对齐 N.E.K.O 的 monitor/telemetry：可观测性，不影响主流程）
        # 内存快照用于实时看板；持久累计由 telemetry 模块落盘（opt-out 友好）。
        self.stats = {
            name: {
                "calls": 0, "ok": 0, "fail": 0,
                "latency_ms": 0, "tokens_prompt": 0,
                "tokens_completion": 0, "last_error": None,
            }
            for name in self.providers
        }

    # ---- 路由解析 ----
    def enabled_for(self, capability: str) -> List[str]:
        """返回能支撑某能力、且已配置 key 的供应商（按优先级排序）。"""
        names = self.routing.get(capability, [])
        return [
            n for n in names
            if n in self.providers
            and capability in self.providers[n].get("capabilities", [])
        ]

    def reload(self):
        """运行时热重载配置中心（ai_providers.json / config.py 默认值）。"""
        cfg = load_provider_config(reset_cache=True)
        self.providers = {
            name: p
            for name, p in cfg["providers"].items()
            if p.get("api_key")
        }
        self.routing = cfg["capability_routing"]
        # 为新出现的供应商补建遥测槽位（保留既有累计）
        for name in self.providers:
            self.stats.setdefault(name, {
                "calls": 0, "ok": 0, "fail": 0, "latency_ms": 0,
                "tokens_prompt": 0, "tokens_completion": 0, "last_error": None,
            })
        # 移除已删除供应商的槽位
        for name in list(self.stats.keys()):
            if name not in self.providers:
                self.stats.pop(name, None)

    def _model_for(self, prov: dict, capability: str, model: Optional[str],
                   role: Optional[str] = None) -> str:
        """模型解析优先级：
        显式 model > 供应商内联角色模型(models[role]) > 能力模型(models[capability])
        > chat 模型 > default_model。

        （① 角色模型内联到供应商条目：每个 provider 的 models 可含角色名，
           被选中时即作为该角色模型；换供应商即换角色模型，无需改全局
           AI_ROLE_ROUTING。当全局 role_routing 未指定 model 时回落到此处。）
        """
        if model:
            return model
        models = prov.get("models", {}) or {}
        if role and role in models:
            return models[role]
        return models.get(capability) or models.get("chat") or prov["default_model"]

    @staticmethod
    def _inject_images(messages: list, images: List[bytes]) -> list:
        """把图片注入到最后一条 user 消息的 content（OpenAI image_url 格式）。"""
        new_messages = [dict(m) for m in messages]
        last = new_messages[-1]
        parts = []
        if isinstance(last.get("content"), str):
            parts.append({"type": "text", "text": last["content"]})
        for img in images:
            b64 = base64.b64encode(img).decode("utf-8")
            parts.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
            })
        new_messages[-1] = {"role": last["role"], "content": parts}
        return new_messages

    # ---- 请求体构造（可单测，不触网）----
    def _build_payload(self, prov, messages, capability, model, think,
                       tools, role) -> dict:
        # ② 方言 extra_body 配置化（泛化原 think_param）：
        #   extra_body 始终随请求发送；think_body 仅 think=True 时发送；
        #   二者合并进请求体顶层（直接 HTTP POST）。未配 think_body 时退化为旧
        #   think_param 开关，保证 config.py 默认 deepseek 行为零回归。
        extra = dict(prov.get("extra_body") or {})
        if think:
            tb = prov.get("think_body")
            if tb is not None:
                extra.update(tb)
            elif prov.get("think_param"):
                extra["thinking"] = {"type": "enabled"}
        else:
            if prov.get("think_param") and "think_body" not in prov:
                extra["thinking"] = {"type": "disabled"}
        payload = {
            "model": self._model_for(prov, capability, model, role),
            "messages": messages,
            "stream": False,
        }
        if tools:
            payload["tools"] = tools
        if extra:
            payload.update(extra)
        return payload

    # ---- 单次调用 ----
    async def _call_once(self, name, prov, messages, capability, model,
                         think, tools, images, timeout,
                         role: Optional[str] = None) -> object:
        if images:
            messages = self._inject_images(messages, images)

        payload = self._build_payload(prov, messages, capability, model,
                                      think, tools, role)

        headers = {
            "Authorization": f"Bearer {prov['api_key']}",
            "Content-Type": "application/json",
        }
        t0 = time.perf_counter()
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            resp = await client.post(
                prov["base_url"].rstrip("/") + "/chat/completions",
                json=payload, headers=headers,
            )
            if resp.status_code != 200:
                raise ProviderError(f"{name} {resp.status_code}: {resp.text[:400]}")
            data = resp.json()
        dt_ms = (time.perf_counter() - t0) * 1000

        # 遥测：成功指标（延迟 + Token 用量，OpenAI 兼容 usage 字段）
        st = self.stats.get(name)
        usage = data.get("usage") or {}
        tp = int(usage.get("prompt_tokens") or 0)
        tc = int(usage.get("completion_tokens") or 0)
        if st is not None:
            st["ok"] += 1
            st["latency_ms"] += dt_ms
            st["tokens_prompt"] += tp
            st["tokens_completion"] += tc
        # 持久累计（opt-out 友好：关闭 ENABLE_TELEMETRY 时 record_* 直接返回）
        telemetry.record_ok("providers", name, dt_ms, tp, tc)

        msg = data["choices"][0]["message"]
        if tools:
            # 归一化：空 content/空 tool_calls 处理，与 deepseek_client 行为一致
            if msg.get("content") is None:
                msg["content"] = ""
            if not msg.get("tool_calls"):
                msg.pop("tool_calls", None)
            msg.pop("reasoning_content", None)
            return msg
        return msg["content"]

    # ---- 对外主入口（含故障转移）----
    async def chat(self, messages: list, capability: str = "chat",
                   model: Optional[str] = None, think: bool = False,
                   tools: Optional[list] = None, images: Optional[List[bytes]] = None,
                   timeout: int = 300, role: Optional[str] = None) -> object:
        # 角色路由（对齐 N.E.K.O 的模型粒度配置：摘要/情感/记忆/判断各用不同模型）。
        # role 在 capability 之上再细化：可覆盖 capability 与 model，缺省则回退到参数。
        cap, mdl = capability, model
        if role:
            rr = load_provider_config().get("role_routing", {}).get(role)
            if rr:
                cap = rr.get("capability", cap)
                mdl = rr.get("model") or mdl
        providers = self.enabled_for(cap)
        if not providers:
            raise ProviderError(f"无可用供应商支撑能力「{cap}」(role={role})"
                                f"（请检查 config.AI_PROVIDERS / AI_CAPABILITY_ROUTING）")
        # 注入当前时间：让模型在发消息时感知时间（覆盖 QQ 聊天/记忆/判断等所有路径）
        messages = time_context.inject_time(messages)
        last_err = None
        for name in providers:
            st = self.stats.get(name)
            if st is not None:
                st["calls"] += 1
            telemetry.record_call("providers", name)
            try:
                return await self._call_once(
                    name, self.providers[name], messages, cap,
                    mdl, think, tools, images, timeout, role,
                )
            except Exception as e:
                if st is not None:
                    st["fail"] += 1
                    st["last_error"] = str(e)[:200]
                telemetry.record_fail("providers", name, e)
                last_err = e
                print(f"[LLM] 供应商「{name}」调用失败(能力={cap}, role={role}): {e}，尝试下一个")
        raise ProviderError(f"所有供应商均失败(能力={cap}, role={role}): {last_err}")

    # 便捷封装：带工具调用（返回 assistant message dict）
    async def chat_with_tools(self, messages: list, tools: list,
                              capability: str = "tools", model=None,
                              think: bool = False, timeout: int = 300) -> dict:
        return await self.chat(
            messages, capability=capability, model=model, think=think,
            tools=tools, timeout=timeout,
        )

    # 便捷封装：单张图片视觉理解（路由到 vision 能力）
    async def describe_image(self, image_bytes: bytes, prompt: str = "",
                             capability: str = "vision", model=None,
                             timeout: int = 120) -> str:
        text = prompt or "请详细描述这张图片的画面内容、主体、动作与文字。用中文回答。"
        return await self.chat(
            [{"role": "user", "content": text}],
            capability=capability, model=model, images=[image_bytes],
            timeout=timeout,
        )


# ============ 统一视觉层（对齐 N.E.K.O：多供应商视觉 + 故障转移） ============
# 视觉与主对话不同：GIF 动画、表情包情绪/描述、视频多帧等是各厂商「专用能力」，
# 不能简单走 OpenAI 兼容 /chat/completions。因此本层把 GLM / Gemini 客户端
# 当作「驱动」，按视觉任务类型路由，支持按 config 改顺序与故障转移，并带遥测。
# 调用方只依赖 UnifiedVision 的方法，不再直接 new GLMClient / GeminiClient。

_DEFAULT_VISION_ROUTING = {
    "image":   ["glm", "gemini"],   # 单张图片
    "gif":     ["glm"],             # GIF 动画（抽多帧）
    "emoji":   ["glm"],             # 表情包画面描述
    "emotion": ["glm"],             # 表情包情绪识别
    "frames":  ["gemini", "glm"],   # 多帧 / 视频理解
    "text":    ["gemini", "glm"],   # 视觉相关文本总结（chat_text）
}

# 驱动别名 -> (模块名, 类名, 凭据可用性判断)
_VISION_DRIVER_FACTORIES = {
    "glm":    ("glm_client", "GLMClient",
               lambda: bool(getattr(config, "DEEPSEEK_API_KEY", ""))),
    "gemini": ("gemini_client", "GeminiClient",
               lambda: bool(getattr(config, "GEMINI_API_KEY", ""))),
}


class UnifiedVision:
    def __init__(self):
        self.drivers = {}          # 别名 -> 客户端实例
        for alias, (mod, cls, has_key) in _VISION_DRIVER_FACTORIES.items():
            if not has_key():
                continue
            try:
                m = __import__(mod, fromlist=[cls])
                self.drivers[alias] = getattr(m, cls)()
            except Exception as e:
                print(f"[VISION] 驱动 {alias} 初始化失败: {e}")
        cfg = load_provider_config()
        self.routing = cfg["vision_routing"]
        self.role_routing = cfg["role_routing"]
        self.stats = {
            a: {"calls": 0, "ok": 0, "fail": 0, "latency_ms": 0, "last_error": None}
            for a in self.drivers
        }

    def reload(self):
        """运行时热重载视觉路由（ai_providers.json / config.py 默认值）。"""
        cfg = load_provider_config(reset_cache=True)
        self.routing = cfg["vision_routing"]
        self.role_routing = cfg["role_routing"]
        for a in self.drivers:
            self.stats.setdefault(a, {"calls": 0, "ok": 0, "fail": 0,
                                      "latency_ms": 0, "last_error": None})

    async def _dispatch(self, task: str, method: str, *args, **kwargs):
        order = self.routing.get(task, list(self.drivers.keys()))
        last = None
        for alias in order:
            drv = self.drivers.get(alias)
            if drv is None:
                continue
            fn = getattr(drv, method, None)
            if fn is None:
                continue
            st = self.stats.get(alias)
            if st:
                st["calls"] += 1
            telemetry.record_call("vision", alias)
            t0 = time.perf_counter()
            try:
                res = await fn(*args, **kwargs)
                if st:
                    st["ok"] += 1
                    st["latency_ms"] += (time.perf_counter() - t0) * 1000
                telemetry.record_ok("vision", alias, (time.perf_counter() - t0) * 1000)
                return res
            except Exception as e:
                if st:
                    st["fail"] += 1
                    st["last_error"] = str(e)[:200]
                telemetry.record_fail("vision", alias, e)
                last = e
                print(f"[VISION] 驱动 {alias} 任务={task} 失败: {e}，尝试下一个")
        raise ProviderError(f"所有视觉驱动均失败(任务={task}): {last}")

    async def describe_image(self, image_bytes: bytes, prompt: str = "") -> str:
        return await self._dispatch("image", "describe_image", image_bytes, prompt)

    async def describe_gif_animation(self, image_bytes: bytes) -> str:
        return await self._dispatch("gif", "describe_gif_animation", image_bytes)

    async def recognize_emotion(self, image_bytes: bytes) -> str:
        return await self._dispatch("emotion", "recognize_emotion", image_bytes)

    async def describe_emoji(self, image_bytes: bytes) -> str:
        return await self._dispatch("emoji", "describe_emoji", image_bytes)

    async def describe_frames(self, frames, prompt: str = "",
                              max_output_tokens: int = 512) -> str:
        return await self._dispatch("frames", "describe_frames", frames, prompt,
                                    max_output_tokens=max_output_tokens)

    async def chat_text(self, text: str, max_output_tokens: int = 800) -> str:
        return await self._dispatch("text", "chat_text", text,
                                    max_output_tokens=max_output_tokens)


_vision: Optional[UnifiedVision] = None


def get_vision() -> UnifiedVision:
    """统一视觉层全局单例。"""
    global _vision
    if _vision is None:
        _vision = UnifiedVision()
    return _vision


def vision_stats() -> dict:
    """返回各视觉驱动累计遥测快照（可序列化，供 monitor 展示）。

    以 telemetry 持久累计为准（重启不丢），并用内存里的 last_error 覆盖展示。
    """
    try:
        v = get_vision()
    except Exception:
        v = None
    snap = telemetry.snapshot("vision")
    if v is None:
        return snap
    for alias, st in v.stats.items():
        if alias in snap:
            snap[alias]["last_error"] = st["last_error"] or snap[alias]["last_error"]
    return snap


_llm: Optional[UnifiedLLM] = None


def get_llm() -> UnifiedLLM:
    """全局单例。修改 config 后进程重启生效。"""
    global _llm
    if _llm is None:
        _llm = UnifiedLLM()
    return _llm


def provider_stats() -> dict:
    """返回各供应商累计遥测快照（可序列化，供 monitor 展示）。

    以 telemetry 持久累计为准（重启不丢），并用内存里的 last_error 覆盖展示。
    """
    try:
        llm = get_llm()
    except Exception:
        llm = None
    snap = telemetry.snapshot("providers")
    if llm is None:
        return snap
    # 把内存里的实时 last_error 合并进快照（持久层也记录，但内存更即时）
    for name, st in llm.stats.items():
        if name in snap:
            snap[name]["last_error"] = st["last_error"] or snap[name]["last_error"]
    return snap
