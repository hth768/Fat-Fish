# -*- coding: utf-8 -*-
"""统一插件注册表：唯一的「项目有哪些插件/大脑/sidecar、各自版本与依赖」事实来源。

背景：此前注册信息散落在 5 处——`agent_core.register_builtin_plugins()`（平台+功能插件）、
`agent_core.register_builtin_brains()`（大脑）、`launcher._build_specs()`（sidecar）、
各插件自己读 config 开关、`web_plugin._api_plugins()` 里硬编码的开关白名单。
没有版本概念，谁依赖谁、缺了谁会坏，全靠人记。

本模块把上述信息收敛为**声明式清单**（SPECS）：

    ┌─────────────────────────────────────────────────────────┐
    │  plugin_registry.SPECS  （唯一事实来源）                  │
    │    ├── PlatformSpec  平台插件（qq/console/web/bilibili）  │
    │    ├── FeatureSpec   功能插件（余额/主动说话/vox/dm/学习） │
    │    ├── BrainSpec     大脑（chat/mc_mod/mc_bot/pc/pvz）    │
    │    └── SidecarSpec   独立进程 sidecar（memory/monitor/…） │
    └─────────────────────────────────────────────────────────┘
              │                    │                   │
     核心按 enabled 注册       launcher 按 enabled   web 控制台读取清单
     （agent_core）           拉起 sidecar          展示完整状态

## 版本约束

每个条目声明 `version`（自身版本，语义化 x.y.z）与可选 `requires`（对其它条目的版本要求）：

    requires={"core": ">=1.0", "vox_tts": ">=1.0,<2.0"}

- 版本比较见 `version_matches()`，支持 `>=` `>` `<=` `<` `==` `!=` 与 `a.b.*` 通配；
- `core` 是内置的「核心版本」伪条目（`CORE_VERSION`），任何插件都可对它提要求；
- 校验结果分三级：`error`（缺失/版本不满足/循环依赖/顺序冲突，会导致启动失败）、
  `warning`（可选依赖缺失、孤儿 sidecar 等，仅提示）。

## 校验入口

    from plugin_registry import SPECS, validate, summary
    report = validate()          # 返回 {ok, errors, warnings, entries}
    print(summary())             # 人类可读的清单文本（/插件 命令与启动日志共用）

`validate()` 是纯函数式的（只读 config，无副作用），可在启动前、测试里、Web 接口中反复调用。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import config

# 核心自身版本：插件可对 "core" 提版本要求（如要求 >=1.0 的插件能力）
CORE_VERSION = "1.0.0"

# 版本号正则（语义化 x.y.z，允许 x.y 与 x.y.* 通配）
_VER_RE = re.compile(r"^\d+(\.\d+)*(\.[*x])?$")


# ======================================================================
# 版本工具
# ======================================================================
def _parse_version(v: str) -> Tuple[int, ...]:
    """把 '1.2.3' / '1.2' / '1.2.*' 解析成可比较的元组（'*' 段归一为 -1 便于粗匹配）。"""
    parts = []
    for seg in str(v).strip().split("."):
        seg = seg.strip()
        if seg in ("*", "x", "X"):
            parts.append(-1)
        else:
            try:
                parts.append(int(seg))
            except ValueError:
                parts.append(0)
    return tuple(parts)


def _cmp_version(a: str, b: str) -> int:
    """比较两个版本：a>b 返回 1，a<b 返回 -1，相等 0。按段对齐后逐段比较。"""
    pa, pb = _parse_version(a), _parse_version(b)
    n = max(len(pa), len(pb))
    pa = pa + (0,) * (n - len(pa))
    pb = pb + (0,) * (n - len(pb))
    for x, y in zip(pa, pb):
        if x == y:
            continue
        # 通配段（-1）视为「匹配任意」，不参与大小判定
        if x == -1 or y == -1:
            continue
        return 1 if x > y else -1
    return 0


def version_matches(actual: str, spec: str) -> bool:
    """判断 actual 是否满足 spec 约束。

    spec 支持逗号分隔的多条件（AND 语义）：
        ">=1.0"  ">=1.0,<2.0"  "==1.2.3"  "!=1.0.0"  "1.2.*"
    空 spec 视为无约束，恒 True。
    """
    if not spec:
        return True
    for cond in str(spec).split(","):
        cond = cond.strip()
        if not cond:
            continue
        m = re.match(r"^(>=|<=|==|!=|>|<|=)?\s*(.+)$", cond)
        if not m:
            return False
        op = m.group(1) or "=="
        if op == "=":
            op = "=="
        target = m.group(2).strip()
        if target in ("*", ""):
            continue
        # 通配写法 "1.2.*"：逐段前缀比较
        if "*" in target or target.lower().endswith("x"):
            pre = target.replace("*", "").replace("x", "").replace("X", "").rstrip(".")
            return actual == pre or actual.startswith(pre + ".")
        c = _cmp_version(actual, target)
        ok = {
            ">=": c >= 0, ">": c > 0, "<=": c <= 0,
            "<": c < 0, "==": c == 0, "!=": c != 0,
        }.get(op, False)
        if not ok:
            return False
    return True


# ======================================================================
# 条目声明
# ======================================================================
@dataclass
class EntrySpec:
    """所有条目的共同字段。"""

    name: str                              # 唯一标识（与 Plugin.name / Brain.name 一致）
    title: str = ""                        # 人类可读名称
    kind: str = "feature"                  # platform / feature / brain / sidecar
    version: str = "1.0.0"                 # 自身版本
    module: str = ""                       # 承载实现的模块名（供 import 定位）
    cls: str = ""                          # 类名（brain / plugin 用）
    switch: str = ""                       # 启用开关（config 属性名）；空=无条件启用
    default_on: bool = False               # switch 缺失时的默认值
    requires: Dict[str, str] = field(default_factory=dict)   # 依赖名 -> 版本约束
    optional_requires: List[str] = field(default_factory=list)  # 软依赖（缺失仅告警）
    order: int = 0                         # 启动顺序（越小越先）；sidecar / 插件通用
    auto_start: bool = False               # 是否随核心自启（大脑 / sidecar 语境）
    extra: Dict = field(default_factory=dict)   # kind 专属字段

    @property
    def ref(self) -> str:
        return f"{self.kind}:{self.name}"

    def enabled(self) -> bool:
        """按开关判断是否启用（无开关则无条件启用）。"""
        if not self.switch:
            return True
        return bool(getattr(config, self.switch, self.default_on))


# ======================================================================
# 清单（唯一事实来源）
# ======================================================================
SPECS: List[EntrySpec] = [
    # ---------------- 平台插件（接入聊天平台）----------------
    EntrySpec(
        name="qq", title="QQ 平台插件", kind="platform", version="1.0.0",
        module="qq_plugin", cls="QQPlugin",
        switch="ENABLE_QQ_PLUGIN", default_on=True, order=10,
        extra={"capabilities": ["group", "voice", "image", "voice_input", "video_input"]},
    ),
    EntrySpec(
        name="console", title="控制台平台插件", kind="platform", version="1.0.0",
        module="console_plugin", cls="ConsolePlugin",
        switch="ENABLE_CONSOLE_PLUGIN", default_on=False, order=11,
        extra={"capabilities": []},
    ),
    EntrySpec(
        name="bilibili", title="B 站直播平台插件", kind="platform", version="1.0.0",
        module="bilibili_plugin", cls="BilibiliPlugin",
        switch="ENABLE_BILIBILI_PLUGIN", default_on=False, order=12,
        optional_requires=["vox_tts"],   # 无 TTS 仍可文字弹幕回复
        extra={"capabilities": ["group"]},
    ),
    EntrySpec(
        name="web", title="Web 控制台平台插件", kind="platform", version="1.0.0",
        module="web_plugin", cls="WebPlugin",
        switch="ENABLE_WEB_PLUGIN", default_on=False, order=13,
        extra={"capabilities": ["group", "voice", "image"]},
    ),

    # ---------------- 功能插件（核心后台能力）----------------
    EntrySpec(
        name="balance_monitor", title="余额监控", kind="feature", version="1.0.0",
        module="agent_core", cls="BalanceMonitorPlugin",
        switch="ENABLE_BALANCE_MONITOR", default_on=False, order=20,
    ),
    EntrySpec(
        name="proactive_speaker", title="主动说话调度器", kind="feature", version="1.0.0",
        module="agent_core", cls="ProactiveSpeakerPlugin",
        switch="ENABLE_PROACTIVE_SPEAKER", default_on=False, order=21,
    ),
    EntrySpec(
        name="vox_tts", title="本地 VoxCPM TTS 管理", kind="feature", version="1.0.0",
        module="tts_vox", cls="VoxTTSPlugin",
        switch="ENABLE_TTS_SERVER", default_on=False, order=22,
        extra={"sidecar": "tts"},
    ),
    EntrySpec(
        name="bilibili_dm", title="B 站私信自动回复", kind="feature", version="1.0.0",
        module="bili_dm", cls="BilibiliDmPlugin",
        switch="ENABLE_BILIBILI_DM", default_on=False, order=23,
        requires={"bilibili": ">=1.0"},   # 复用 B 站会话/风控层
    ),
    EntrySpec(
        name="bilibili_learn_schedule", title="B 站每日自主学习", kind="feature", version="1.0.0",
        module="bili_learn_scheduler", cls="BilibiliLearnScheduler",
        switch="ENABLE_BILIBILI_LEARN_SCHEDULE", default_on=False, order=24,
        requires={"bilibili": ">=1.0"},
        optional_requires=["bilibili_dm"],   # 分享走 DM；缺了则不分享
    ),

    # ---------------- 大脑（注册进 AgentCore.brains）----------------
    # 仅 chat 为内建；mc_mod/mc_bot/pc/pvz 大脑已随插件分发（brain_* 插件包
    # 自带大脑类与实现），由插件装载（create_brain）时注册，不再列入内建清单。
    EntrySpec(
        name="chat", title="聊天大脑", kind="brain", version="1.0.0",
        module="agent_core", cls="ChatBrain", auto_start=True, order=30,
        extra={"form": "message"},
    ),

    # ---------------- sidecar（独立进程）----------------
    EntrySpec(
        name="memory", title="记忆 RPC 服务", kind="sidecar", version="1.0.0",
        module="memory_server", cls="",
        switch="ENABLE_MEMORY_SERVER", default_on=False, order=0,
        extra={"script": "memory_server.py", "host_switch": "MEMORY_SERVER_HOST",
               "port_switch": "MEMORY_SERVER_PORT", "port_default": 8766, "required": True},
    ),
    EntrySpec(
        name="monitor", title="监控 / 可观测性服务", kind="sidecar", version="1.0.0",
        module="monitor_server", cls="",
        switch="ENABLE_MONITOR", default_on=False, order=1,
        requires={"memory": ">=1.0"},
        extra={"script": "monitor_server.py", "host_switch": "MONITOR_HOST",
               "port_switch": "MONITOR_PORT", "port_default": 8770, "required": False},
    ),
    EntrySpec(
        name="tts", title="本地 TTS 推理服务", kind="sidecar", version="1.0.0",
        module="vox_tts_server", cls="",
        switch="ENABLE_TTS_SERVER", default_on=False, order=2,
        extra={"script": "vox_tts_server.py", "host": "127.0.0.1", "port": 8765,
               "required": False, "python_switch": "VOX_TTS_PYTHON",
               "python_default": "venv_vox/Scripts/python.exe",
               "feature": "vox_tts"},
    ),
    EntrySpec(
        name="telemetry", title="遥测服务", kind="sidecar", version="1.0.0",
        module="telemetry_server", cls="",
        switch="ENABLE_TELEMETRY_SERVER", default_on=False, order=3,
        extra={"script": "telemetry_server.py", "host_switch": "TELEMETRY_SERVER_HOST",
               "port_switch": "TELEMETRY_SERVER_PORT", "port_default": 8771,
               "required": False},
    ),
]

# 按 name 索引（name 在全局唯一；sidecar 与插件同名场景本清单内不存在）
_BY_NAME: Dict[str, EntrySpec] = {s.name: s for s in SPECS}


def get_spec(name: str) -> Optional[EntrySpec]:
    return _BY_NAME.get(name)


def by_kind(kind: str) -> List[EntrySpec]:
    return [s for s in SPECS if s.kind == kind]


def enabled_specs(kind: Optional[str] = None) -> List[EntrySpec]:
    """按开关过滤后、按 order 升序的条目列表。"""
    items = [s for s in SPECS if (kind is None or s.kind == kind) and s.enabled()]
    return sorted(items, key=lambda s: (s.order, s.name))


# ======================================================================
# 校验
# ======================================================================
@dataclass
class Issue:
    level: str        # error / warning
    name: str
    message: str

    def render(self) -> str:
        # 用纯 ASCII 标记：GBK 控制台无法编码 ✗ 等符号，会在 /插件 输出时抛 UnicodeEncodeError
        tag = "ERR" if self.level == "error" else "WARN"
        return f"[{tag}] {self.name}: {self.message}"


def _version_table() -> Dict[str, str]:
    """当前参与校验的「名字 -> 实际版本」表（含 core 伪条目）。"""
    table = {"core": CORE_VERSION}
    for s in SPECS:
        table[s.name] = s.version
    return table


def validate() -> Dict:
    """校验整个清单，返回 {ok, errors, warnings, entries, counts}。

    检查项：
      1. 版本约束：enabled 的条目对其 requires 逐条核对名字是否存在、版本是否满足；
      2. 软依赖：optional_requires 缺失只告警（不阻塞）；
      3. 循环依赖：requires 图上做拓扑检测；
      4. 顺序冲突：被依赖项 order 必须 < 依赖者 order（同 kind 内生效，跨 kind 跳过）；
      5. 孤儿 sidecar：feature 声明了 sidecar 但清单里没有对应 sidecar 条目；
      6. 模块可导入性：不在此处做（可能触发重依赖），交由启动流程按需暴露。
    """
    errors: List[Issue] = []
    warnings: List[Issue] = []
    table = _version_table()
    active = enabled_specs()

    # 1/2. 依赖与版本
    for spec in active:
        for dep, spec_req in (spec.requires or {}).items():
            if dep not in table:
                errors.append(Issue("error", spec.name,
                                    f"依赖不存在的条目「{dep}」（requirements={spec_req}）"))
                continue
            dep_spec = _BY_NAME.get(dep)
            if dep_spec is not None and not dep_spec.enabled() and dep_spec.switch:
                warnings.append(Issue("warning", spec.name,
                                      f"依赖「{dep}」当前被 {dep_spec.switch}=False 关闭"))
            if not version_matches(table[dep], spec_req):
                errors.append(Issue("error", spec.name,
                                    f"依赖「{dep}」版本不满足：需要 {spec_req}，实际 {table[dep]}"))
        for dep in (spec.optional_requires or []):
            if dep not in table:
                warnings.append(Issue("warning", spec.name, f"可选依赖「{dep}」不在清单中"))
            elif not (_BY_NAME[dep].enabled()):
                warnings.append(Issue("warning", spec.name, f"可选依赖「{dep}」未启用，相关能力将降级"))

    # 3. 循环依赖（对全量清单检查：即便当前关闭，也提前暴露「同时开启就坏」的隐患）
    all_names = {s.name for s in SPECS}
    graph = {s.name: [d for d in (s.requires or {}) if d in all_names] for s in SPECS}

    WHITE, GRAY, BLACK = 0, 1, 2
    color = {n: WHITE for n in graph}

    def _dfs(node: str, path: List[str]) -> bool:
        color[node] = GRAY
        for nxt in graph.get(node, []):
            if color.get(nxt) == GRAY:
                cyc = " -> ".join(path + [nxt])
                errors.append(Issue("error", node, f"检测到循环依赖：{cyc}"))
                return True
            if color.get(nxt) == WHITE and _dfs(nxt, path + [nxt]):
                return True
        color[node] = BLACK
        return False

    for n in list(graph):
        if color[n] == WHITE:
            _dfs(n, [n])

    # 4. 顺序冲突（同样对全量清单检查，避免「开启后才踩坑」）
    for spec in SPECS:
        for dep in (spec.requires or {}):
            dep_spec = _BY_NAME.get(dep)
            if dep_spec is None:
                continue
            if dep_spec.kind == spec.kind and dep_spec.order >= spec.order:
                errors.append(Issue("error", spec.name,
                                    f"顺序冲突：依赖「{dep}」order={dep_spec.order} "
                                    f"不早于自身 order={spec.order}"))

    # 5. 孤儿 sidecar 引用
    for spec in active:
        ref = (spec.extra or {}).get("sidecar")
        if ref and ref not in _BY_NAME:
            warnings.append(Issue("warning", spec.name, f"声明的 sidecar「{ref}」不在清单中"))

    counts: Dict[str, int] = {}
    for s in SPECS:
        counts[s.kind] = counts.get(s.kind, 0) + 1

    return {
        "ok": not errors,
        "errors": [i.render() for i in errors],
        "warnings": [i.render() for i in warnings],
        "counts": counts,
        "total": len(SPECS),
        "enabled": len(active),
        "core_version": CORE_VERSION,
    }


def summary() -> str:
    """生成人类可读清单（启动日志 / /插件 命令 / Web 面板共用）。"""
    rep = validate()
    lines = [f"插件注册表 v{rep['core_version']} "
             f"（共 {rep['total']} 项，启用 {rep['enabled']} 项）"]
    kind_title = {"platform": "平台插件", "feature": "功能插件",
                  "brain": "大脑", "sidecar": "sidecar 服务"}
    for kind in ("platform", "feature", "brain", "sidecar"):
        items = by_kind(kind)
        if not items:
            continue
        lines.append(f"  [{kind_title.get(kind, kind)}]")
        for s in sorted(items, key=lambda x: x.order):
            state = "启用" if s.enabled() else "关闭"
            dep = ""
            if s.requires:
                dep = "  依赖: " + ", ".join(f"{k}{v}" for k, v in s.requires.items())
            opt = ""
            if s.optional_requires:
                opt = "  [软] " + ", ".join(s.optional_requires)
            lines.append(f"    - {s.name} v{s.version} ({s.title}) [{state}]{dep}{opt}")
    if rep["errors"]:
        lines.append("  校验错误:")
        lines += ["    " + e for e in rep["errors"]]
    if rep["warnings"]:
        lines.append("  校验告警:")
        lines += ["    " + w for w in rep["warnings"]]
    return "\n".join(lines)


def to_dict() -> List[Dict]:
    """结构化清单（供 Web /api/plugins 使用）。"""
    out = []
    for s in sorted(SPECS, key=lambda x: (x.kind, x.order, x.name)):
        out.append({
            "name": s.name,
            "title": s.title,
            "kind": s.kind,
            "version": s.version,
            "module": s.module,
            "cls": s.cls,
            "switch": s.switch,
            "enabled": s.enabled(),
            "requires": dict(s.requires or {}),
            "optional_requires": list(s.optional_requires or []),
            "order": s.order,
            "extra": dict(s.extra or {}),
        })
    return out


if __name__ == "__main__":   # python plugin_registry.py：打印清单 + 校验结果
    print(summary())
