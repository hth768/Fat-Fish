# -*- coding: utf-8 -*-
"""环境提示词模板渲染（对齐 Cortico `role='envPrompt'` 的 promptDocs）。

模板语法（与 Dungeon 的 ENV_PROMPT.md 一致）：
    {{变量名}}                 用变量值替换；变量缺失时留空
    {{变量名 | 缺省值}}        变量缺失或为空时使用缺省值

变量来自 `World.envPromptVars()`，键为点分名（如 `dungeon.worldName`）。
"""
from __future__ import annotations

import os
import re
from typing import Dict, Optional

_VAR_RE = re.compile(r"\{\{\s*([A-Za-z0-9_.\-]+)\s*(?:\|\s*([^{}]*?))?\s*\}\}")


def render(template: str, variables: Optional[Dict[str, str]]) -> str:
    """渲染模板。未知变量替换为空串或其 `|` 后的缺省值。"""
    if not template:
        return ""
    variables = variables or {}

    def repl(m: "re.Match") -> str:
        name = m.group(1)
        default = (m.group(2) or "").strip()
        value = variables.get(name)
        if value is None or value == "":
            return default
        return str(value)

    return _VAR_RE.sub(repl, template)


def render_file(path: str, variables: Optional[Dict[str, str]] = None) -> str:
    """读模板文件并渲染。文件不存在时返回空串（与 Cortico「文件不存在时读作空」一致）。"""
    if not path or not os.path.isfile(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return render(f.read(), variables)
    except OSError as e:
        print(f"[CORTICO] 环境提示词读取失败 {path}: {e}")
        return ""


def declared_vars(template: str) -> list:
    """列出模板里用到的变量名（用于校验与 World 提供的变量是否一致）。"""
    return [m.group(1) for m in _VAR_RE.finditer(template or "")]
