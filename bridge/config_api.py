# -*- coding: utf-8 -*-
"""配置页 API：按 Schema 输出当前值（含掩码），保存时写覆盖层并立即生效。"""
try:
    from .. import settings_store
except ImportError:  # bridge 作为顶层包导入时
    import settings_store


def _current(key):
    import config as qq_config
    return getattr(qq_config, key, None)


def config_view() -> dict:
    sections = []
    for sec in settings_store.CONFIG_SCHEMA:
        items = []
        for it in sec["items"]:
            v = _current(it["key"])
            t = it["type"]
            view = {"key": it["key"], "type": t, "label": it["label"]}
            if t == "secret":
                view["value"] = settings_store.mask_secret(v) if v else ""
                view["masked"] = bool(v)
            elif t == "json":
                view["value"] = json_text(v)
            elif v is None:
                view["value"] = ""
            else:
                view["value"] = v
            items.append(view)
        sections.append({"section": sec["section"], "hint": sec.get("hint", ""), "items": items})
    return {"sections": sections, "app": settings_store.get_app_settings()}


def json_text(v):
    import json
    try:
        return json.dumps(v, ensure_ascii=False, indent=2)
    except Exception:
        return str(v)


def coerce(it, raw):
    """按 Schema 类型转换前端提交的值。"""
    t = it["type"]
    if t == "bool":
        if isinstance(raw, str):
            return raw.strip().lower() in ("1", "true", "on", "是", "yes")
        return bool(raw)
    if t == "int":
        return int(float(str(raw).strip() or 0))
    if t == "float":
        return float(str(raw).strip() or 0)
    if t == "json":
        import json
        s = str(raw).strip()
        if not s:
            return None
        return json.loads(s)
    return str(raw) if raw is not None else ""


def config_save(values: dict) -> dict:
    """保存一批配置：类型转换 -> 覆盖层 -> 运行时生效 -> 供应商热重载。"""
    schema_items = {}
    for sec in settings_store.CONFIG_SCHEMA:
        for it in sec["items"]:
            schema_items[it["key"]] = it

    to_apply = {}
    errors = []
    for key, raw in (values or {}).items():
        it = schema_items.get(key)
        if it is None:
            errors.append(f"未知配置项: {key}")
            continue
        try:
            v = coerce(it, raw)
        except Exception as e:
            errors.append(f"{key}: {e!r}")
            continue
        # 掩码原样提交 = 不修改
        if it["type"] == "secret" and settings_store.is_masked(v):
            continue
        to_apply[key] = v

    if to_apply:
        settings_store.set_values(to_apply)
        for k, v in to_apply.items():
            settings_store.apply_value(k, v)
        # 供应商键 -> 热重载 ai_provider
        provider_changed = [k for k in to_apply if k in settings_store.PROVIDER_KEYS]
        reload_result = None
        if provider_changed:
            try:
                from ai_provider import reload_provider_config
                cfg = reload_provider_config()
                reload_result = {"providers": sorted((cfg.get("providers") or {}).keys())}
            except Exception as e:
                reload_result = {"error": repr(e)}
        return {"ok": not errors, "applied": sorted(to_apply.keys()),
                "errors": errors, "provider_reload": reload_result}
    return {"ok": not errors, "applied": [], "errors": errors}


def save_app_settings(patch: dict) -> dict:
    return {"ok": True, "app": settings_store.set_app_settings(patch or {})}
