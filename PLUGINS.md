# 插件协议（Plugin Protocol）

本文是肥鱼单机版 **插件接入规范**，供第三方按统一协议开发插件、适配本机核心。
核心运行时不区分「官方」与「第三方」插件——只要满足下文契约即可被装载。

> 协议范围：`feiyu_standalone` 的 App/bridge 层（可插拔包）与 `libs/qq_bot_runtime` 引擎内建插件。
> 引擎内建插件（Python 类）见 `libs/qq_bot_runtime/plugin_base.py`；应用层可插拔包见本文。

---

## 0. 两层插件模型

| 层 | 形态 | 适用场景 | 改动代码？ |
|---|---|---|---|
| **引擎内建插件** | Python 类（继承 `Plugin`/`PlatformPlugin`/`FeaturePlugin`/`AgentBrain`） | 深度接入核心、需要核心内部对象 | 需改 `libs/qq_bot_runtime` |
| **应用层可插拔包** | `plugins/<name>/` 目录（`manifest.json` + `plugin.py`/资源） | 不改动核心，拷贝即装、卸载即删 | **零代码改动** |

本文重点是**应用层可插拔包协议**（第三方最常用）。引擎内建插件仅在第 6 节给契约摘要。

---

## 1. 包目录结构

每个插件是一个**独立目录**，命名必须为蛇形（snake_case），例如 `plugins/qq_platform/`：

```
plugins/<package_name>/            # 目录名 = 包名（snake_case，唯一）
├── manifest.json                  # 【必需】插件元数据，不执行代码即可列出
├── plugin.py                      # 【可选】包装器（平台/功能/大脑包需要）
├── <资源>                         # 任意：脚本、模型、配置、图标……
└── (sidecar) extra ./script.py    # sidecar 包的启动脚本（由 manifest.sidecar 指向）
```

- 包名必须**全局唯一**；同名会冲突。
- 仓库式分发：把整个目录拷到目标机 `plugins/` 即可；大资源（GB 级）走资源包 NTFS junction 挂载（见第 5 节），不进此目录本体。

---

## 2. `manifest.json` 协议

**必备字段**

| 字段 | 类型 | 说明 |
|---|---|---|
| `name` | string (snake_case) | 包名，等于目录名，全局唯一 |
| `title` | string | 展示名（中文也可） |
| `version` | string (`semver` 风格，如 `1.0.0`) | 插件版本 |
| `kind` | enum | **`platform` / `feature` / `brain` / `sidecar` / `local`** 之一（见第 3 节） |

**可选字段**

| 字段 | 类型 | 说明 |
|---|---|---|
| `description` | string | 一句话说明 |
| `author` | string | 作者 |
| `switch` | string | 对应的 `config.py` 开关名（如 `ENABLE_QQ_PLUGIN`），装载时自动置 `True` |
| `requires` | array<string> | 依赖的**引擎内建**能力/模块名 |
| `pkg_requires` | array<string> | 依赖的**其它插件包名**（装载前校验存在） |
| `group` | string | 归属依赖组（见 `plugins/groups.json`：`core/voice/qq/bilibili/mc/desktop/services`） |
| `entry` | string | 包装器文件名，默认 `plugin.py` |
| `sidecar` | object | `{ "script": "相对路径", "host": "127.0.0.1", "port": int }`，仅 `kind=sidecar` 需要 |
| `optional_requires` | array<string> | 可选依赖；缺失时降级而非报错 |
| `target` | string | 资源包挂载目标运行时目录（如 `mc_bot`、`models`、`venv_vox`），配合资源包使用 |

**校验**：包管理器 `load_all()` 会校验 `kind` 合法、`name` 与目录一致；非法/损坏包会被跳过并告警。

---

## 3. 四类包的接入契约

### 3.1 `kind: "platform"`（平台插件）
把某个聊天/直播平台接入核心。

`plugin.py` 必须提供：

```python
def create_plugin(core) -> "PlatformPlugin":
    from plugin_base import PlatformPlugin
    # 返回你的平台插件实例（已实现 start / ReplyTarget / MessageSender）
    return MyPlatformPlugin(core)
```

平台插件需继承 `PlatformPlugin`，并：
- 声明 `capabilities = {"group": bool, "voice": bool, "image": bool,
  "voice_input": bool, "video_input": bool}`；
- `start()` 中：建立平台连接，把平台消息转成 `InboundMessage`，**注入全局 sender**（`set_sender(MySender())`），再调用 `await core.chat.handle_message(msg, reply)`；
- 实现 `ReplyTarget`（回复上下文，至少 `reply()`，按需 `reply_voice`/`fetch_image`/`fetch_video`）；
- 实现 `MessageSender`（主动推送：`send_private`/`send_group`/`send_voice_*`）。

> 最简参考实现见 `libs/qq_bot_runtime/console_plugin.py`（约 100 行）。

### 3.2 `kind: "feature"`（功能插件）
核心后台能力（余额监控、主动说话、定时任务等）。

```python
def create_plugin(core) -> "FeaturePlugin":
    from plugin_base import FeaturePlugin
    return MyFeaturePlugin(core)
```

只需实现 `start()` / `stop()`，通过 `core` 访问聊天、事件总线、记忆等服务。
启用状态写入覆盖层 `pkg_plugins[<name>] = True`。

### 3.3 `kind: "brain"`（大脑插件）
一个会自己思考/回应的智能体单元（自主循环、外部驱动等）。

```python
def create_brain(core) -> "AgentBrain":
    from brain_base import AgentBrain
    return MyBrain(core)
```

大脑继承 `AgentBrain`，设置 `name`/`title`/`kind`（`chat`|`autonomous`|`external`）/
`description`/`auto_start_on_core`，实现 `start()`/`stop()`/`status()`。
通过 `await brain_event(core, name, kind, text)` 上报事件（`help`/`notice`/`state`），
核心会转发求助、平台/功能插件可订阅 `brain.event` 总线事件。

### 3.4 `kind: "sidecar"`（旁路子进程插件）
把独立进程服务（向量记忆、监控、遥测等）作为可插拔包运行。

- `manifest.sidecar = {"script": "run.py", "host": "127.0.0.1", "port": 8765}`；
- 无需 `plugin.py` 包装器。包管理器按 `script`（相对 qq_bot 根目录）用当前解释器 spawn 子进程，
  负责健康检查与终止（`bridge/sidecar_runner.py` 的 `SidecarProcess`）；
- 子进程通过本地 RPC（HTTP + asyncio，见 `service_host.py`）与核心通信。

### 3.5 `kind: "local"`（本地包）
自包含实现，不包装 qq_bot 模块，纯资源/脚本型插件。无需 `create_*` 函数。

---

## 4. 核心契约：消息与事件

平台插件把自家协议转换为以下**平台无关**契约（来自 `libs/qq_bot_runtime/message_bus.py`）：

### 4.1 `InboundMessage`（收到的一条消息）
必填：`platform`(str) / `channel_type`(`"private"`|`"group"`) / `channel_id`(str) /
`user_id`(str) / `text`(str) / `mentioned`(bool)。
可填：`user_name` / `message_id` / `image_refs` / `audio_wav`(已解码 wav) / `has_video` /
`video_ref` / `quoted_text` / `quoted_image_refs` / `quoted_sender` / `quoted_self` / `raw`(平台原始事件，逃生舱口)。

> 媒体以「引用(ref)」携带，由平台经 `ReplyTarget.fetch_*` 按需取回，避免无谓下载。

### 4.2 `ReplyTarget`（一次会话的回复上下文）
每条消息对应一个实例，平台必须实现：
- `async reply(text)` —— 必填；
- `async reply_voice(wav_path)` / `async fetch_image(ref)->bytes` / `async fetch_video(ref)->str` —— 按能力选实现，不支持可降级或抛异常。
- `send_target() -> "private:123" | "group:456"` —— 主动发送的目标格式。

### 4.3 `MessageSender`（核心→平台主动推送）
- 必填：`send_private(user_id, text)` / `send_group(group_id, text)`；
- 可选：`send_voice_private` / `send_voice_group`（默认丢弃并打印告警）；
- 通用：`send_text(target, text)` / `send_voice(target, wav_path)`（`target` 形如 `private:123`）。
- 平台在 `start()` 中 `set_sender(实例)`，核心模块经 `get_sender()` 获取后调用。

### 4.4 事件总线 `AgentEventBus`
- `bus.on(event_type, async_handler)` 订阅，`bus.emit(event_type, data)` 发布；
- 大脑统一事件名 = `"brain.event"`，`data = {"source", "kind", "text", "ts"}`，`kind ∈ help|notice|state`；
- 平台/功能插件应订阅 `brain.event` 以呈现大脑求助/播报。

---

## 5. 资源包与挂载协议

超大资源（模型/语音包/MC 包）**不进插件目录本体**，而是作为独立资源包，运行时以 **NTFS junction** 挂载到 qq_bot 运行时目录：

- 插件 `manifest.target` 指定挂载目标（如 `mc_bot`/`models`/`venv_vox`/`hf_cache`）；
- 管理器在 `rt/<target>` 创建 junction 指向资源包实际路径；卸载时仅删除 junction，不删实体；
- 多包依赖同一资源时共用一个 junction（安装计数/引用）。

---

## 6. 引擎内建插件契约（摘要，供深度接入）

位于 `libs/qq_bot_runtime/plugin_base.py`，通过 `plugin_registry.SPECS` 声明式注册：

| 基类 | 关键成员 |
|---|---|
| `Plugin` | `__init__(core)` / `start()` / `stop()` / `status()` / `name` |
| `PlatformPlugin(Plugin)` | `capabilities` 字典 / `start()` 注入 sender |
| `FeaturePlugin(Plugin)` | 后台能力 |
| `AgentBrain(Plugin)` | `title`/`kind`/`auto_start_on_core`/`status()` |

`plugin_registry.SPECS` 每条含：`version` / `requires`（版本约束）/ `optional_requires` /
`switch`（config 开关）/ `order`（启动顺序）；`validate()` 校验循环依赖、版本、顺序、孤儿 sidecar。

---

## 7. 最小可运行模板

### 平台插件：`plugins/my_platform/manifest.json`
```json
{
  "name": "my_platform",
  "title": "我的平台",
  "version": "1.0.0",
  "kind": "platform",
  "description": "把某平台接入肥鱼核心",
  "switch": "ENABLE_MY_PLATFORM",
  "group": "core"
}
```

### 平台插件：`plugins/my_platform/plugin.py`
```python
from plugin_base import PlatformPlugin
from message_bus import InboundMessage, MessageSender, ReplyTarget, set_sender

class MySender(MessageSender):
    async def send_private(self, user_id, text): ...
    async def send_group(self, group_id, text): ...

class MyReplyTarget(ReplyTarget):
    async def reply(self, text): ...

class MyPlatformPlugin(PlatformPlugin):
    name = "my_platform"
    platform = "my_platform"
    capabilities = {"group": True, "voice": False, "image": False,
                    "voice_input": False, "video_input": False}

    async def start(self):
        set_sender(MySender())
        # 建立平台连接，收到消息后：
        #   msg = InboundMessage(platform=self.platform, channel_type="private",
        #                        channel_id=uid, user_id=uid, text=t, mentioned=True)
        #   await self.core.chat.handle_message(msg, MyReplyTarget(self, msg))
        await super().start()

def create_plugin(core):
    return MyPlatformPlugin(core)
```

### 功能插件：`plugins/my_feature/plugin.py`
```python
from plugin_base import FeaturePlugin

class MyFeaturePlugin(FeaturePlugin):
    name = "my_feature"
    async def start(self):
        # 用 self.core 访问 chat / bus / memory 等
        await super().start()

def create_plugin(core):
    return MyFeaturePlugin(core)
```

### 大脑插件：`plugins/my_brain/plugin.py`
```python
from brain_base import AgentBrain, brain_event

class MyBrain(AgentBrain):
    name = "my_brain"
    title = "我的智能体"
    kind = "autonomous"
    description = "自主循环示例"
    async def start(self):
        # 观察→决策→执行循环；遇到困难：
        #   await brain_event(self.core, self.name, "help", "主人帮帮我")
        await super().start()

def create_brain(core):
    return MyBrain(core)
```

### sidecar 插件：`plugins/sidecar_demo/manifest.json`
```json
{
  "name": "sidecar_demo",
  "title": "演示旁路服务",
  "version": "1.0.0",
  "kind": "sidecar",
  "sidecar": { "script": "run.py", "host": "127.0.0.1", "port": 8765 }
}
```

---

## 8. 装载与命令行

| 操作 | 入口 |
|---|---|
| 装载全部插件包 | `bridge/pkg_manager.py:load_all()` |
| 列出/启用/禁用/扫描/全盘寻插件 | `bridge/plugins_api.py` 暴露的 HTTP `/api/plugins/*` |
| 启用状态持久化 | 覆盖层 `pkg_plugins: { 包名: bool }`（settings_store） |
| 依赖分组批量启停 | `plugins/groups.json` |

插件被装载后，引擎内建插件经 `agent_core.register_builtin_plugins()` 注册，应用层包经 `pkg_manager` 注册到 `core.plugins` / `core.brains`。
