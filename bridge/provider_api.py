# -*- coding: utf-8 -*-
"""模型供应商配置 API：集中常用模型的 API 预设 + 用户自填 API Key + 自定义模型。

支持 OpenAI 兼容 与 Anthropic 兼容（ai_provider 按 provider 的 api_style 分流）。
用户选一个预设（OpenAI / Anthropic / DeepSeek / … / 自定义），填 API Key、选或填模型名，
保存后写入对应槽位（main/vision/role）的 AI_PROVIDERS 并把相关能力路由指到它
（其余带 key 的供应商作故障转移）。

槽位说明：
- main  : 主模型（chat / reasoning）→ 对话、推理
- vision: 视觉模型（vision）→ 单图理解，路由到 ai_provider 的 vision 能力
- role  : 角色模型（role）→ 摘要/判断/记忆等带 role 的调用统一使用的模型
"""
# 常用模型 API 预设（base_url 为该厂商 OpenAI/Anthropic 兼容根地址）
PROVIDER_PRESETS = {
    "openai": {
        "label": "OpenAI",
        "api_style": "openai",
        "base_url": "https://api.openai.com/v1",
        "models": ["gpt-4o", "gpt-4o-mini", "gpt-4-turbo", "o1", "o3-mini", "gpt-5"],
    },
    "anthropic": {
        "label": "Anthropic (Claude)",
        "api_style": "anthropic",
        "base_url": "https://api.anthropic.com",
        "models": ["claude-3-5-sonnet-latest", "claude-3-opus-latest",
                   "claude-3-haiku-latest", "claude-sonnet-4", "claude-opus-4"],
    },
    "deepseek": {
        "label": "DeepSeek",
        "api_style": "openai",
        "base_url": "https://api.deepseek.com",
        "models": ["deepseek-chat", "deepseek-reasoner"],
    },
    "moonshot": {
        "label": "Moonshot (Kimi)",
        "api_style": "openai",
        "base_url": "https://api.moonshot.cn/v1",
        "models": ["moonshot-v1-8k", "moonshot-v1-32k", "mooncake-v1"],
    },
    "qwen": {
        "label": "通义千问 (Qwen)",
        "api_style": "openai",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "models": ["qwen-max", "qwen-plus", "qwen-turbo", "qwen2.5-vl-max", "qwen2.5-72b-instruct"],
    },
    "glm": {
        "label": "智谱 GLM",
        "api_style": "openai",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "models": ["glm-4-plus", "glm-4-air", "glm-4-flash"],
    },
    "custom": {
        "label": "自定义（兼容端点）",
        "api_style": "openai",
        "base_url": "",
        "models": [],
    },
}

# 槽位定义：prov_name 为写入 AI_PROVIDERS 的供应商名（与路由键一一对应）
SLOT_DEFS = {
    "main": {
        "label": "主模型",
        "prov_name": "main",
        "caps": ["chat", "reasoning"],
        "routing_caps": ["chat", "reasoning"],
        "cap_models": lambda m: {"chat": m, "reasoning": m},
        "role_routing": None,
    },
    "vision": {
        "label": "视觉模型",
        "prov_name": "vision",
        "caps": ["vision"],
        "routing_caps": ["vision"],
        "cap_models": lambda m: {"vision": m},
        "role_routing": None,
    },
    "role": {
        "label": "角色模型",
        "prov_name": "role",
        "caps": ["role"],
        "routing_caps": ["role"],
        "cap_models": lambda m: {"role": m},
        "role_routing": lambda m: {"*": {"capability": "role", "model": m}},
    },
}


def list_presets() -> dict:
    return {"presets": PROVIDER_PRESETS}


def slot_label(slot: str) -> str:
    return SLOT_DEFS.get(slot, {}).get("label", slot)


def get_provider(slot: str) -> dict:
    if slot not in SLOT_DEFS:
        slot = "main"
    import ai_provider
    sd = SLOT_DEFS[slot]
    cfg = ai_provider.load_provider_config()
    provs = cfg["providers"]
    prov = provs.get(sd["prov_name"])
    cur = None
    if prov:
        mkey = sd["caps"][0]
        model = (prov.get("models") or {}).get(mkey) or prov.get("default_model") or ""
        cur = {
            "preset": prov.get("preset", ""),
            "api_style": prov.get("api_style", "openai"),
            "base_url": prov.get("base_url", ""),
            "model": model,
            "has_key": bool(prov.get("api_key")),
            "api_key": "****" if prov.get("api_key") else "",
        }
    return {"presets": PROVIDER_PRESETS, "current": cur,
            "available": sorted(provs.keys()), "slot": slot}


def save_provider(slot: str, payload: dict) -> dict:
    import ai_provider
    from settings_store import set_values, apply_value, is_masked

    if slot not in SLOT_DEFS:
        return {"ok": False, "error": "未知模型槽位: %s" % slot}
    sd = SLOT_DEFS[slot]

    preset = (payload.get("preset") or "").strip()
    api_key = (payload.get("api_key") or "").strip()
    base_url = (payload.get("base_url") or "").strip()
    model = (payload.get("model") or "").strip() or (payload.get("custom_model") or "").strip()
    api_style = (payload.get("api_style") or "").strip()

    pinfo = PROVIDER_PRESETS.get(preset, {})
    if not api_style:
        api_style = pinfo.get("api_style", "openai")
    if not base_url:
        base_url = pinfo.get("base_url", "")

    cfg = ai_provider.load_provider_config()
    existing = cfg["providers"].get(sd["prov_name"], {})
    # 前端若只回传掩码（未改动），保留原密钥
    if is_masked(api_key) and existing.get("api_key"):
        api_key = existing["api_key"]
    if not api_key:
        return {"ok": False, "error": "请填写 API Key"}
    if not base_url:
        return {"ok": False, "error": "请填写 Base URL"}
    if not model:
        return {"ok": False, "error": "请选择或填写模型名"}

    provider = {
        "api_key": api_key,
        "base_url": base_url,
        "default_model": model,
        "api_style": api_style or "openai",
        "models": sd["cap_models"](model),
        "capabilities": list(sd["caps"]),
        "preset": preset,
    }
    # 合并其余供应商（保留 deepseek/glm 等），本槽位供应商为用户所选
    providers = dict(cfg["providers"])
    providers[sd["prov_name"]] = provider

    # 仅重建本槽位相关能力的路由；其余能力（vision/role/tools…）原样保留
    cap = dict(cfg["capability_routing"])
    for rc in sd["routing_caps"]:
        others = [n for n in providers
                  if n != sd["prov_name"]
                  and rc in (providers[n].get("capabilities") or [])
                  and providers[n].get("api_key")]
        cap[rc] = [sd["prov_name"]] + others

    role_routing = dict(cfg["role_routing"])
    if sd["role_routing"] is not None:
        role_routing.update(sd["role_routing"](model))

    set_values({
        "AI_PROVIDERS": providers,
        "AI_CAPABILITY_ROUTING": cap,
        "AI_ROLE_ROUTING": role_routing,
    })
    apply_value("AI_PROVIDERS", providers)
    apply_value("AI_CAPABILITY_ROUTING", cap)
    apply_value("AI_ROLE_ROUTING", role_routing)

    reload_ok = True
    reload_err = None
    try:
        ai_provider.reload_provider_config()
    except Exception as e:
        reload_ok = False
        reload_err = repr(e)
    return {"ok": True, "provider": sd["prov_name"], "model": model,
            "reload": reload_ok, "reload_err": reload_err,
            "routing": cap.get(sd["routing_caps"][0])}


# ---- 向后兼容别名（仅覆盖主模型槽位）----
def get_main() -> dict:
    return get_provider("main")


def save_main(payload: dict) -> dict:
    return save_provider("main", payload)
