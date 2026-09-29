# -*- coding: utf-8 -*-
"""Cortico `api=5` 扩展契约的 Python 镜像（纯数据，零第三方依赖）。

对照 TS 源：
    cortico/src/core/types.ts    World / WorldHost / ToolDef / EventEnvelope / ConfigGroup
    cortico/src/world.ts         WorldDefinition / WorldContext / WorldSection

只做结构镜像与必要的运行时语义（游标、标签、触发方式），不复制 Core 的实现。
与 TS 侧的扩展字段（如 dungeon 回执里的 `failed` / `next`）以宽松字段承载，向前兼容。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict
from typing import Any, Awaitable, Callable, Dict, List, Literal, Optional, Sequence

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: 本层实现的 Cortico 扩展 API 版本。包清单 `cortico.api` 不等于它时拒绝加载。
API_VERSION = 5

EventOrigin = Literal["external", "internal"]
EventTag = Literal["snapshot", "speak"]
ContextDelivery = Literal["deliver", "archive-only"]

#: 投递触发方式。与 TS `TriggerMode` 一一对应：
#: preempt=立即投递整批并请求抢占在途轮；flush=立即投递积压；
#: debounce=按静默/批龄合批；piggyback=只入队，随下一批一起投递。
TriggerMode = Literal["preempt", "flush", "debounce", "piggyback"]
TRIGGER_MODES: Sequence[str] = ("preempt", "flush", "debounce", "piggyback")

#: 工具分类标签（不发给模型，供装配与上下文处理用）。
ToolTag = Literal["read", "write", "speak", "act", "flow", "snapshot"]

ConfigOwner = str  # 'core' | 'persona' | 'world:<id>' | 'provider:<id>'


def now_iso(tz_offset_hours: Optional[float] = None) -> str:
    """ISO 8601 带时区偏移的时间戳（本地时区）。与 TS `nowIso()` 对齐。"""
    if tz_offset_hours is None:
        t = time.time()
        if time.localtime(t).tm_isdst and time.daylight:
            offset = -time.altzone
        else:
            offset = -time.timezone
    else:
        offset = int(tz_offset_hours * 3600)
    sign = "+" if offset >= 0 else "-"
    offset = abs(offset)
    hh, mm = offset // 3600, (offset % 3600) // 60
    return time.strftime(f"%Y-%m-%dT%H:%M:%S{sign}{hh:02d}:{mm:02d}", time.localtime(t))


# ---------------------------------------------------------------------------
# 事件
# ---------------------------------------------------------------------------

@dataclass
class BlobRef:
    """已保存附件的引用。feiyu 侧暂不支持二进制附件，保留结构以对齐协议。"""
    handle: str
    mime: str
    name: str = ""
    fallback_text: str = ""


@dataclass
class EventEnvelope:
    """持久化事件记录。cursor 是落库时分配的全局递增序号。

    Core 不改写正文；事件库 cursor 不属于正文，平台消息 id 由 World 写在 meta 里。
    """
    type: str
    ts: str
    source: str
    text: str
    cursor: int = 0
    run: str = ""
    origin: EventOrigin = "external"
    tags: List[str] = field(default_factory=list)
    context_delivery: ContextDelivery = "deliver"
    sender_key: str = ""
    meta: Dict[str, Any] = field(default_factory=dict)
    blobs: List[BlobRef] = field(default_factory=list)
    ephemeral: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "cursor": self.cursor,
            "run": self.run,
            "type": self.type,
            "ts": self.ts,
            "source": self.source,
            "origin": self.origin,
            "tags": list(self.tags),
            "contextDelivery": self.context_delivery,
            "text": self.text,
            "senderKey": self.sender_key,
            "meta": dict(self.meta),
            "ephemeral": self.ephemeral,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "EventEnvelope":
        return cls(
            cursor=int(d.get("cursor") or 0),
            run=str(d.get("run") or ""),
            type=str(d.get("type") or ""),
            ts=str(d.get("ts") or ""),
            source=str(d.get("source") or ""),
            origin=str(d.get("origin") or "external"),
            tags=list(d.get("tags") or []),
            context_delivery=str(d.get("contextDelivery") or "deliver"),
            text=str(d.get("text") or ""),
            sender_key=str(d.get("senderKey") or ""),
            meta=dict(d.get("meta") or {}),
            ephemeral=bool(d.get("ephemeral")),
        )


@dataclass
class PushOptions:
    """`pushEvent` 的投递选项。"""
    deliver: bool = True
    trigger: Optional[TriggerMode] = None


@dataclass
class DeferredEventSpec:
    """延迟到投递时渲染的事件（render 读取现有状态，不做长时间采样）。"""
    type: str
    source: str
    origin: EventOrigin = "external"
    sender_key: str = ""
    meta: Dict[str, Any] = field(default_factory=dict)
    tags: List[str] = field(default_factory=list)
    render: Optional[Callable[[], Any]] = None


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

@dataclass
class ToolSchema:
    """发给模型的工具声明（OpenAI function calling 形状由装配层转换）。"""
    name: str
    description: str
    parameters: Dict[str, Any] = field(default_factory=dict)

    def to_openai(self) -> Dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters or {"type": "object", "properties": {}},
            },
        }


@dataclass
class ToolOutcome:
    """工具结果。字符串返回值等价于 `{"text": ...}`。

    `failed` / `next` 是 Dungeon 等 World 回执里的扩展字段，原样透传给模型上下文。
    """
    text: str
    failed: bool = False
    next: List[str] = field(default_factory=list)
    data: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def coerce(cls, value: Any) -> "ToolOutcome":
        if isinstance(value, ToolOutcome):
            return value
        if isinstance(value, dict):
            return cls(
                text=str(value.get("text", "")),
                failed=bool(value.get("failed", False)),
                next=[str(x) for x in (value.get("next") or [])],
                data={k: v for k, v in value.items() if k not in ("text", "failed", "next")},
            )
        return cls(text=str(value))

    def render(self) -> str:
        """渲染成给模型的回执正文（含「接下来可以」列表，与 Dungeon 一致）。"""
        out = self.text
        if self.next:
            out += "\n\n接下来可以：\n" + "\n".join(f"- {x}" for x in self.next)
        return out


@dataclass
class ToolCallContext:
    """工具调用的上下文（对齐 TS `ToolCallContext`，用 asyncio.Event 代替 AbortSignal）。"""
    role: str = "main"
    call_id: str = ""
    round: int = 0
    world_id: str = ""
    cancelled: Any = None      # asyncio.Event，set() 表示宿主放弃本次调用
    log: Any = None            # 可选 logger

    @property
    def aborted(self) -> bool:
        ev = self.cancelled
        try:
            return bool(ev is not None and ev.is_set())
        except Exception:
            return False


#: 工具处理函数签名：(args, ctx) -> str | ToolOutcome | dict
ToolHandler = Callable[[Dict[str, Any], ToolCallContext],
                       Any]


@dataclass
class ToolDef(ToolSchema):
    """带处理函数的工具定义。

    barrier_after: 模型读到结果后才可继续；同一条输出中排在它后面的调用会被跳过。
    ends_turn:     工具完成后结束本次唤醒。
    """
    tags: List[str] = field(default_factory=list)
    barrier_after: bool = False
    ends_turn: bool = False
    handler: Optional[ToolHandler] = None

    def is_async_handler(self) -> bool:
        import inspect
        return bool(self.handler) and inspect.iscoroutinefunction(self.handler)


# ---------------------------------------------------------------------------
# 配置组
# ---------------------------------------------------------------------------

@dataclass
class ConfigGroupSchema:
    """配置组 schema：键是 cfg 中的点分路径（如 `worlds.dungeon.serverUrl`）。"""
    type: str = "object"
    title: str = ""
    description: str = ""
    properties: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def coerce(cls, raw: Dict[str, Any]) -> "ConfigGroupSchema":
        return cls(
            type=str(raw.get("type") or "object"),
            title=str(raw.get("title") or ""),
            description=str(raw.get("description") or ""),
            properties={str(k): dict(v) for k, v in (raw.get("properties") or {}).items()
                        if isinstance(v, dict)},
        )


@dataclass
class ConfigGroup:
    """World 暴露给控制台/配置页的一组可编辑配置。"""
    id: str
    owner: ConfigOwner = ""
    schema: ConfigGroupSchema = field(default_factory=ConfigGroupSchema)

    @classmethod
    def coerce(cls, raw: Dict[str, Any]) -> "ConfigGroup":
        return cls(
            id=str(raw.get("id") or ""),
            owner=str(raw.get("owner") or ""),
            schema=ConfigGroupSchema.coerce(raw.get("schema") or {}),
        )

    def paths(self) -> List[str]:
        return list(self.schema.properties.keys())


# ---------------------------------------------------------------------------
# 控制台声明
# ---------------------------------------------------------------------------

@dataclass
class WorldLamp:
    """World 自报的组件可用状态。state: online/loading/error/offline。"""
    label: str = ""
    state: str = "offline"
    hint: str = ""


@dataclass
class PromptVarDecl:
    name: str = ""
    description: str = ""


@dataclass
class PromptDocDecl:
    """提示词模板声明。`role='envPrompt'` 的模板用 `envPromptVars()` 渲染后进入 system 前缀。"""
    key: str = ""
    title: str = ""
    description: str = ""
    path: str = ""
    deployment_path: str = ""
    role: str = ""            # 'envPrompt' | 'prefix' | ''
    vars: List[PromptVarDecl] = field(default_factory=list)

    @classmethod
    def coerce(cls, raw: Dict[str, Any]) -> "PromptDocDecl":
        return cls(
            key=str(raw.get("key") or ""),
            title=str(raw.get("title") or ""),
            description=str(raw.get("description") or ""),
            path=str(raw.get("path") or ""),
            deployment_path=str(raw.get("deploymentPath") or ""),
            role=str(raw.get("role") or ""),
            vars=[PromptVarDecl(str(v.get("name", "")), str(v.get("description", "")))
                  for v in (raw.get("vars") or []) if isinstance(v, dict)],
        )


@dataclass
class StoragePart:
    """World 的可清除存储项（列在控制台数据页签）。stat/clear 由实现方提供。"""
    key: str = ""
    label: str = ""
    kind: str = "disk"        # 'disk' | 'memory'
    location: str = ""
    danger: bool = False
    note: str = ""
    order: int = 0
    stat: Optional[Callable[[], str]] = None
    clear: Optional[Callable[[], Any]] = None


@dataclass
class WorldConsoleDecl:
    """World 给控制台的声明（按界面语言生成）。"""
    label: str = ""
    lamps: List[WorldLamp] = field(default_factory=list)
    badges: List[Dict[str, Any]] = field(default_factory=list)
    panels: List[Dict[str, Any]] = field(default_factory=list)
    prompt_docs: List[PromptDocDecl] = field(default_factory=list)
    config: List[ConfigGroup] = field(default_factory=list)
    links: List[Dict[str, Any]] = field(default_factory=list)
    # invoke(panel, method, args) -> Any；stream 由 Node 桥内部处理，不对 Python 暴露
    invoke: Optional[Callable[[str, str, List[Any]], Any]] = None

    @classmethod
    def coerce(cls, raw: Dict[str, Any]) -> "WorldConsoleDecl":
        return cls(
            label=str(raw.get("label") or ""),
            lamps=[WorldLamp(str(x.get("label", "")), str(x.get("state", "offline")),
                             str(x.get("hint", "")))
                   for x in (raw.get("lamps") or []) if isinstance(x, dict)],
            badges=[dict(x) for x in (raw.get("badges") or []) if isinstance(x, dict)],
            panels=[dict(x) for x in (raw.get("panels") or []) if isinstance(x, dict)],
            prompt_docs=[PromptDocDecl.coerce(x) for x in (raw.get("promptDocs") or [])
                         if isinstance(x, dict)],
            config=[ConfigGroup.coerce(x) for x in (raw.get("config") or []) if isinstance(x, dict)],
            links=[dict(x) for x in (raw.get("links") or []) if isinstance(x, dict)],
        )


# ---------------------------------------------------------------------------
# 宿主能力
# ---------------------------------------------------------------------------

@dataclass
class ModelFacts:
    """当前模型的能力。渲染层据 MIME 支持决定是否附加二进制内容。"""
    _model: str = ""
    _accepts: Sequence[str] = ("image/png", "image/jpeg", "image/gif", "image/webp")
    _context_window: Optional[int] = None

    def model(self) -> str:
        return self._model

    def accepts(self, mime: str) -> bool:
        return mime in self._accepts

    def context_window(self) -> Optional[int]:
        return self._context_window


@dataclass
class LLMUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    def as_dict(self) -> Dict[str, int]:
        return asdict(self)


class EventStoreReader:
    """事件库只读接口（World 通过 host.store 查询历史）。"""

    def since(self, cursor: int, limit: int = 200) -> List[EventEnvelope]:
        raise NotImplementedError

    def latest(self) -> int:
        raise NotImplementedError

    def count(self) -> int:
        raise NotImplementedError


class WorldHost:
    """World 可用的宿主接口（对齐 TS `WorldHost`）。

    必选：push_event / store / drain_pending_events / model_facts / report_usage
    可选：push_deferred / push_candidate / blob / llm_stalls / cognition / is_paused
    """

    # ---- 必选 ----
    async def push_event(self, e: EventEnvelope, opts: Optional[PushOptions] = None) -> EventEnvelope:
        raise NotImplementedError

    @property
    def store(self) -> EventStoreReader:
        raise NotImplementedError

    async def drain_pending_events(self,
                                   filter_fn: Callable[[EventEnvelope], bool]) -> List[EventEnvelope]:
        raise NotImplementedError

    @property
    def model_facts(self) -> ModelFacts:
        raise NotImplementedError

    def report_usage(self, usage: LLMUsage, **kwargs: Any) -> None:
        raise NotImplementedError

    # ---- 可选 ----
    def push_deferred(self, spec: DeferredEventSpec, trigger: Optional[TriggerMode] = None) -> None:
        raise NotImplementedError

    def blob(self, handle: str) -> Optional[Any]:
        return None

    async def llm_stalls(self, within_ms: int) -> int:
        return 0

    def is_paused(self) -> bool:
        return False

    def log(self, level: str, message: str, **data: Any) -> None:
        print(f"[WORLD][{level}] {message}" + (f" {data}" if data else ""))


# ---------------------------------------------------------------------------
# World 契约
# ---------------------------------------------------------------------------

@dataclass
class WorldSection:
    """World 配置段的最小形状：必须有 enabled。"""
    enabled: bool = False


class World:
    """World 契约（对齐 TS `World`）。Python 实现方继承或鸭子类型实现即可。

    - env_prompt_vars(): 环境模板变量；返回 None 时省略该 World 的环境段。
    - tools(): 工具定义（声明 + handler）。
    - console(language): 控制台声明（可选）。
    - start(host) / stop(): 生命周期。
    """

    id: str = ""

    def env_prompt_vars(self) -> Optional[Dict[str, str]]:
        return {}

    def tools(self) -> List[ToolDef]:
        return []

    def console(self, language: str = "zh") -> Optional[WorldConsoleDecl]:
        return None

    async def start(self, host: WorldHost) -> None:
        return None

    async def stop(self) -> None:
        return None

    def on_turn_ended(self) -> None:
        return None

    def on_handoff_ended(self) -> None:
        return None


@dataclass
class WorldContext:
    """装配层提供给 World 构造函数的上下文（对齐 TS `WorldContext`）。"""
    id: str = ""
    cfg: Dict[str, Any] = field(default_factory=dict)
    timezone: str = ""
    bot_name: str = ""
    bot_dir: str = ""
    package_dir: str = ""
    data_dir: str = ""
    repo_root: str = ""
    secret: Callable[[str], str] = lambda name: ""
    store_secret: Callable[[str, str], None] = lambda name, value: None
    persist: Callable[[Dict[str, Any]], None] = lambda patch: None
    restart: Callable[[], Awaitable[None]] = None  # type: ignore[assignment]

    async def do_restart(self) -> None:
        if self.restart is not None:
            await self.restart()


@dataclass
class WorldDefinition:
    """World 定义（对齐 TS `WorldDefinition`）。

    defaults(): 每次返回新对象；enabled 默认 False，由部署配置启用。
    preflight(): 激活前置检查，抛错即不能激活，错误信息原样给操作者。
    create(ctx): 构造实例。
    """
    id: str
    label: str = ""
    defaults: Callable[[], Dict[str, Any]] = lambda: {"enabled": False}
    preflight: Optional[Callable[[WorldContext], None]] = None
    create: Optional[Callable[[WorldContext], World]] = None


#: World 声明：字符串是 World id，对象形式带 label/reason（本地缺实现也允许）。
WorldDeclaration = Any
