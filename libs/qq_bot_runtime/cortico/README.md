# Cortico 协议兼容层（api=5）

让 feiyu（Python）与 Cortico（TypeScript）**双架构兼容**：两边用同一套 World 扩展契约。

## 它做什么

| 能力 | 说明 |
|------|------|
| 协议层 | `types.py` 是 Cortico `api=5` 契约的 Python 镜像：World / WorldHost / ToolDef / EventEnvelope / ConfigGroup / WorldConsoleDecl |
| 装配层 | `registry.py` 对齐 `cortico/src/world.ts` 的 `WorldAssembly`：挂载/停用/重启、工具撞名拒绝、配置持久化 |
| 运行时桥 | `node_bridge.py` + `node/` 在 Node 子进程里真跑 `cortico-world-*` 的 TS 代码，宿主能力回调给 Python |
| 驱动 | `brain.py` 世界大脑：事件合批唤醒 → 模型 → 工具 → 输出；`tool_loop.py` 是工具循环 |
| 接入 | `plugin.py` 是功能插件（已进 `plugin_registry`）；聊天主线可注入 World 的环境提示词 |

契约来源（对照阅读）：
- `D:\testing\Cortico\src\core\types.ts` —— World / WorldHost / ToolDef / EventEnvelope
- `D:\testing\Cortico\src\world.ts` —— WorldDefinition / WorldContext / WorldAssembly

## 跑起来（以 cortico-world-dungeon 为例）

`config.py`：

```python
CORTICO_ENABLED = True
CORTICO_PACKAGE_ROOTS = [r"D:\testing\Cortico\extensions\node_modules"]
CORTICO_CORE_ROOT = r"D:\testing\Cortico"          # 解析包里的 cortico/... 裸导入
CORTICO_WORLDS = {"cortico-world-dungeon": {"enabled": True, "serverUrl": "http://127.0.0.1:8787"}}
```

前置：Node >= 22、可用的 `tsx`（跑 TS 源码用；纯 JS 包可把 `CORTICO_NODE_LOADER` 置空）。

启动后：`已挂载: cortico-world-dungeon`，模型可调用 `dungeon_*` 九个工具；
World 推来的事件进 `brain.event` 通道，`CORTICO_REPORT_TO_OWNER=True` 时另私聊主人。

## 写自己的 Python World（按 api=5 契约）

```python
from cortico.loader import register_python_world
from cortico.types import World, WorldDefinition, ToolDef, EventEnvelope

class MyWorld(World):
    id = "myworld"
    async def start(self, host):
        await host.push_event(EventEnvelope(type="my.hello", ts="...", source="myworld", text="就绪"))
    async def stop(self): ...
    def tools(self): return [ToolDef(name="my_echo", description="回声",
                                     parameters={"type": "object", "properties": {}},
                                     tags=["read"], handler=lambda a, c: f"echo: {a}")]
    def env_prompt_vars(self): return {"my.name": "我的世界"}
    def console(self, language="zh"): return None

register_python_world(WorldDefinition(id="myworld", label="我的世界",
                                      defaults=lambda: {"enabled": True},
                                      create=lambda ctx: MyWorld()))
```

## 命令与接口

- 状态：`cortico.plugin.status()`（面板可读的完整状态）
- 挂载：`plugin.activate(id)` / `deactivate(id)` / `restart(id)`
- 配置：`plugin.config_view()` 渲染、`plugin.save_config(group_id, values)` 保存（按 `x-hot` 提示是否需重启）
- 环境提示词：`plugin.env_prompt_segments()`（聊天主线注入用）

## 已知边界

- TS World 只在 Node 子进程里跑，宿主能力覆盖 `pushEvent / 持久化 / 密钥 / 日志`；
  `pushCandidate`、`outputTap`、`console.stream` 等高级能力未实现（需要时再补）。
- `cortico.api != 5` 的包（如 mc-agent/vtuber 的 api=3）会被识别但拒绝加载，原因写进 `missing`。
