# -*- coding: utf-8 -*-
"""Cortico 扩展包清单：读 `package.json` 的 `cortico` 字段并校验 api 版本。

Cortico 靠三样东西认出一个扩展包：
- `package.json` 里 `keywords` 含 `cortico-world`
- `cortico.kind`（本层只处理 `world`）
- `cortico.api`（必须与本层 API_VERSION 相同，不同即拒绝加载并说明原因）

`main` / `exports["."]` 指向包的入口；TS 包常直接指向 `src/index.ts`（由 tsx 运行）。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .types import API_VERSION

#: 包清单里标识扩展包的关键字
WORLD_KEYWORD = "cortico-world"


@dataclass
class CorticoManifest:
    """一个 cortico-world-* 扩展包的清单。"""
    name: str = ""
    version: str = ""
    description: str = ""
    root: str = ""                 # 包根目录（含 package.json 的目录）
    main: str = ""                 # 入口文件相对路径（可能是 .ts）
    kind: str = ""                 # 'world' | 'provider' | 'bot' | ...
    api: int = 0
    label: str = ""
    console_client: str = ""
    console_style: str = ""
    engines_node: str = ""
    raw: Dict = field(default_factory=dict)

    @property
    def entry(self) -> str:
        """入口文件的绝对路径（可能不存在，例如只声明未安装）。"""
        return os.path.join(self.root, self.main) if self.main else ""

    @property
    def is_world(self) -> bool:
        return self.kind == "world"

    def compatible(self) -> tuple:
        """(是否可加载, 原因)。"""
        if not self.is_world:
            return False, f"不是 world 扩展（kind={self.kind or '未声明'}）"
        if self.api != API_VERSION:
            return False, (f"协议版本不一致：包是 api={self.api or '未声明'}，"
                           f"本层是 api={API_VERSION}")
        return True, ""

    def to_dict(self) -> Dict:
        ok, why = self.compatible()
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "root": self.root,
            "main": self.main,
            "kind": self.kind,
            "api": self.api,
            "label": self.label,
            "consoleClient": self.console_client,
            "loadable": ok,
            "reason": why,
        }


def read_manifest(root: str) -> Optional[CorticoManifest]:
    """读一个目录下的 package.json，不是 cortico 扩展包则返回 None。"""
    path = os.path.join(root, "package.json")
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            pkg = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"[CORTICO] 清单读取失败 {path}: {e}")
        return None
    cort = pkg.get("cortico")
    if not isinstance(cort, dict):
        return None
    keywords = [str(k) for k in (pkg.get("keywords") or [])]
    if WORLD_KEYWORD not in keywords and not cort.get("kind"):
        return None

    main = pkg.get("main") or ""
    if not main:
        exports = pkg.get("exports")
        if isinstance(exports, dict):
            dot = exports.get(".")
            if isinstance(dot, str):
                main = dot
            elif isinstance(dot, dict):
                main = dot.get("default") or dot.get("import") or ""
    engines = pkg.get("engines") or {}
    return CorticoManifest(
        name=str(pkg.get("name") or ""),
        version=str(pkg.get("version") or ""),
        description=str(pkg.get("description") or ""),
        root=root,
        main=str(main or ""),
        kind=str(cort.get("kind") or ""),
        api=int(cort.get("api") or 0),
        label=str(cort.get("label") or pkg.get("name") or ""),
        console_client=str(cort.get("consoleClient") or ""),
        console_style=str(cort.get("consoleStyle") or ""),
        engines_node=str(engines.get("node") or ""),
        raw=pkg,
    )


def discover(roots: List[str], max_depth: int = 2) -> List[CorticoManifest]:
    """在若干根目录里发现 cortico-world-* 包。

    两层扫描：`<root>/*/package.json` 与 `<root>/*/node_modules/*/package.json`。
    """
    found: Dict[str, CorticoManifest] = {}
    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        candidates: List[str] = []
        try:
            for entry in os.listdir(root):
                p = os.path.join(root, entry)
                if os.path.isdir(p):
                    candidates.append(p)
                    nm = os.path.join(p, "node_modules")
                    if os.path.isdir(nm):
                        for sub in os.listdir(nm):
                            sp = os.path.join(nm, sub)
                            if os.path.isdir(sp):
                                candidates.append(sp)
        except OSError as e:
            print(f"[CORTICO] 扫描失败 {root}: {e}")
            continue
        for c in candidates:
            m = read_manifest(c)
            if m is None:
                continue
            prev = found.get(m.name)
            if prev is None or _version_gt(m.version, prev.version):
                found[m.name] = m
    return sorted(found.values(), key=lambda x: x.name)


def _version_gt(a: str, b: str) -> bool:
    def parts(s: str):
        out = []
        for chunk in str(s).split("."):
            digits = "".join(ch for ch in chunk if ch.isdigit())
            out.append(int(digits) if digits else 0)
        return out
    return parts(a) > parts(b)
