# -*- coding: utf-8 -*-
"""World 配置组（JSON Schema 子集）的读写与校验。

对齐 `cortico/src/core/config-schema.ts`：
- 键是 cfg 中的点分路径（`worlds.dungeon.serverUrl`）
- 只替换叶子属性，保留父对象引用供热更新使用

额外支持的展示/语义扩展（与 Dungeon 一致）：
- `x-hot`:  true=改完立即生效（热）；false=需重启 World
- `x-scale`: 展示时数值除以它（如 ms -> s）
- `x-suffix`: 展示后缀

本模块只做**校验与取值**，不决定"改完要不要重启"——那由装配层按 x-hot 判断。
"""
from __future__ import annotations

import copy
from typing import Any, Dict, List, Optional, Tuple

from .types import ConfigGroup

HOT_KEY = "x-hot"
SCALE_KEY = "x-scale"
SUFFIX_KEY = "x-suffix"


def get_by_path(obj: Dict[str, Any], path: str) -> Any:
    """按点分路径取值；取不到返回 None。"""
    cur: Any = obj
    for seg in str(path).split("."):
        if cur is None or not isinstance(cur, dict):
            return None
        cur = cur.get(seg)
    return cur


def set_by_path(obj: Dict[str, Any], path: str, value: Any) -> None:
    """按点分路径写入叶子值，保留沿途父对象引用。"""
    segs = str(path).split(".")
    leaf = segs.pop()
    cur: Dict[str, Any] = obj
    for seg in segs:
        nxt = cur.get(seg)
        if nxt is None or not isinstance(nxt, dict):
            cur[seg] = {}
            nxt = cur[seg]
        cur = nxt
    cur[leaf] = value


def is_hot(prop: Dict[str, Any]) -> bool:
    """该配置项改完是否立即生效（无需重启 World）。缺省 False。"""
    return bool(prop.get(HOT_KEY))


def display_value(prop: Dict[str, Any], value: Any) -> Any:
    """按 x-scale 换算成展示值（不改变存储值）。"""
    scale = prop.get(SCALE_KEY)
    if isinstance(scale, (int, float)) and scale and isinstance(value, (int, float)):
        return value / scale
    return value


def _coerce_number(prop: Dict[str, Any], value: Any) -> Tuple[bool, Any, str]:
    try:
        if isinstance(value, bool):
            return False, value, "布尔值不能当数字"
        num = float(value) if not isinstance(value, (int, float)) else value
        if isinstance(num, float) and num.is_integer() and prop.get("type") == "integer":
            num = int(num)
        if prop.get("type") == "integer" and not float(num).is_integer():
            return False, value, "必须是整数"
    except (TypeError, ValueError):
        return False, value, "不是合法数字"

    minimum = prop.get("minimum")
    maximum = prop.get("maximum")
    if isinstance(minimum, (int, float)) and num < minimum:
        return False, value, f"不能小于 {minimum}"
    if isinstance(maximum, (int, float)) and num > maximum:
        return False, value, f"不能大于 {maximum}"
    multiple = prop.get("multipleOf")
    if isinstance(multiple, (int, float)) and multiple > 0 and num % multiple != 0:
        return False, value, f"必须是 {multiple} 的倍数"
    return True, (int(num) if prop.get("type") == "integer" else num), ""


def validate_value(prop: Dict[str, Any], value: Any) -> Tuple[bool, Any, str]:
    """校验并规范化单个配置值。返回 (是否通过, 规范化后的值, 错误原因)。"""
    ptype = prop.get("type")
    if value is None:
        return bool(prop.get("nullable")), None, ("" if prop.get("nullable") else "不能为空")

    if ptype == "boolean":
        if isinstance(value, bool):
            return True, value, ""
        if isinstance(value, str) and value.lower() in ("true", "false", "1", "0", "yes", "no"):
            return True, value.lower() in ("true", "1", "yes"), ""
        return False, value, "必须是布尔值"

    if ptype in ("integer", "number"):
        return _coerce_number(prop, value)

    if ptype == "string":
        if not isinstance(value, str):
            value = str(value)
        enum = prop.get("enum")
        if isinstance(enum, list) and enum and value not in enum:
            return False, value, f"必须是其中之一: {', '.join(str(x) for x in enum)}"
        max_len = prop.get("maxLength")
        if isinstance(max_len, int) and len(value) > max_len:
            return False, value, f"长度不能超过 {max_len}"
        return True, value, ""

    if ptype == "array":
        if not isinstance(value, list):
            return False, value, "必须是数组"
        return True, value, ""

    return True, value, ""


def group_values(cfg: Dict[str, Any], group: ConfigGroup) -> Dict[str, Any]:
    """取该配置组在当前 cfg 里的全部值。"""
    return {p: get_by_path(cfg, p) for p in group.paths()}


def apply_patch(cfg: Dict[str, Any], group: ConfigGroup,
                values: Dict[str, Any]) -> Tuple[List[str], Dict[str, str]]:
    """把一组值写进 cfg（只替换叶子）。返回 (改动的路径, {路径: 错误原因})。"""
    changed: List[str] = []
    errors: Dict[str, str] = {}
    for path, raw in (values or {}).items():
        prop = group.schema.properties.get(path)
        if prop is None:
            errors[path] = "不在该配置组内"
            continue
        ok, value, why = validate_value(prop, raw)
        if not ok:
            errors[path] = why
            continue
        old = get_by_path(cfg, path)
        if old != value:
            set_by_path(cfg, path, value)
            changed.append(path)
    return changed, errors


def describe(group: ConfigGroup, cfg: Dict[str, Any]) -> Dict[str, Any]:
    """把配置组整理成配置页可直接渲染的形状。"""
    fields = []
    for path, prop in group.schema.properties.items():
        fields.append({
            "path": path,
            "title": str(prop.get("title") or path),
            "description": str(prop.get("description") or ""),
            "type": str(prop.get("type") or "string"),
            "enum": prop.get("enum") or [],
            "minimum": prop.get("minimum"),
            "maximum": prop.get("maximum"),
            "hot": is_hot(prop),
            "scale": prop.get(SCALE_KEY) or 1,
            "suffix": str(prop.get(SUFFIX_KEY) or ""),
            "value": get_by_path(cfg, path),
        })
    return {
        "id": group.id,
        "owner": group.owner,
        "title": group.schema.title,
        "description": group.schema.description,
        "fields": fields,
    }


def defaults_from(groups: List[ConfigGroup]) -> Dict[str, Any]:
    """从配置组里收集 default 值（用于给新 World 补默认配置段）。"""
    out: Dict[str, Any] = {}
    for g in groups:
        for path, prop in g.schema.properties.items():
            if "default" in prop:
                set_by_path(out, path, copy.deepcopy(prop["default"]))
    return out
