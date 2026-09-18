# 肥鱼单机版 · 肥鱼娘 App（Feiyu Standalone）

> 肥鱼娘智能体桌面控制台。后端为 Python（本机 HTTP 服务 + 智能体核心），前端为 `pywebview` / Edge App 窗口承载的原生 JS/CSS 控制台（`webui/`）。所有能力（聊天、记忆、总结、插件、外观、配置、MC、B 站、电脑操控……）均在**本机单进程**内运行，数据留在本机，无需公网。

- 仓库地址：`hth768/Fat-Fish`
- 包管理器：无（便携自举 `venv` / 捆绑 Python）；智能体核心复用 `libs/qq_bot_runtime`（qq_bot 运行时）

---

## 速览

> 一页纸概览见 **[OVERVIEW.md](./OVERVIEW.md)**（核心能力 + 运行方式）。

## 目录

- [架构](#架构)
  - [桌面 App 分层](#桌面-app-分层)
  - [智能体核心：以核心为脑，平台即插件](#智能体核心以核心为脑平台即插件)
- [仓库结构](#仓库结构)
- [快速开始](#快速开始)
  - [便携版（推荐，Windows）](#便携版推荐windows)
  - [从源码运行（开发者）](#从源码运行开发者)
- [配置](#配置)
- [功能模块](#功能模块)
  - [聊天大脑](#聊天大脑)
  - [记忆子系统](#记忆子系统)
  - [总结](#总结)
  - [插件系统](#插件系统)
  - [构建助手（对话式 Agent）](#构建助手对话式-agent)
  - [外观](#外观)
  - [模型注册表（LLM 供应商）](#模型注册表llm-供应商)
- [插件协议（第三方接入规范）](#插件协议第三方接入规范)
- [MC 功能（模组世界 + 原版世界）](#mc-功能模组世界--原版世界)
- [安卓版](#安卓版)
- [安全边界与合规](#安全边界与合规)
- [测试 / 回归](#测试--回归)
- [发布与安装](#发布与安装)
- [许可证](#许可证)
- [常见问题](#常见问题)

---

## 架构

### 桌面 App 分层

```
┌──────────────────────────────────────────────────────┐
│  桌面窗口：APP 独立窗口（pywebview / Edge App）           │
│     → webui（浏览器）→ 命令行（三级降级）                  │
│  └── webui/（原生 JS/CSS 控制台，localhost 访问）         │
└────────────────────────┬─────────────────────────────┘
                         │ HTTP REST + SSE（仅监听 127.0.0.1）
┌────────────────────────▼─────────────────────────────┐
│  server.py：静态前端托管 + REST API + SSE 事件流          │
│  bridge/：API 路由 + 窗口 + 核心循环（CoreBridge）         │
│  libs/qq_bot_runtime：qq_bot 智能体运行时（bot 引擎）       │
└─────────────────────────────────────────────────────┘
```

- **前端** `webui/index.html · app.js · styles.css`：侧栏含 仪表盘 / 聊天 / 记忆 / 总结 / 插件 / 配置 / 构建助手 / 外观（「AI 供应商（模型管理）」是配置页内的面板）。
- **后端 HTTP** `server.py`：仅本机监听，托管静态前端并暴露 REST API 与 SSE 实时事件流（`SO_EXCLUSIVEADDRUSE` 保证单实例）。
- **智能体核心**：App 以库方式调用 `libs/qq_bot_runtime` 的核心，运行时零源码改动。

### 智能体核心：以核心为脑，平台即插件

qq_bot 运行时在 2026-09 重构为「**智能体核心 + 插件系统**」：QQ 聊天降级为一个平台插件，B 站、直播、Telegram 等可后续接入，核心零改动。同月加「**核心大脑**」层：`AgentCore.brains` 大脑注册表统一管理所有智能体大脑的生命周期、状态与求助事件通道，新 agent 实现 `AgentBrain` + 一行注册即接入。

```
                       ┌──────────────────────────────────┐
                       │            AgentCore             │
                       │   chat: ChatService 聊天大脑      │
                       │   plugins: PluginManager          │
                       │   bus: AgentEventBus 事件总线      │
                       │   brains: BrainManager 注册表      │
                       └────────┬────────────────┬────────┘
            InboundMessage      │                │  MessageSender / 事件
            （平台→核心）        │                │  （核心→平台主动推送）
  ┌─────────────────────────▼──┐    ┌────────▼──────────────────┐
  │ 平台插件 PlatformPlugin      │    │ 功能插件 FeaturePlugin      │
  │  qq_plugin / console_plugin  │    │  余额监控 / 主动说话 / 定时   │
  │  bilibili_plugin（直播）      │    └───────────────────────────┘
  └─────────────────────────────┘
```

**核心文件职责**（位于 `libs/qq_bot_runtime/`）：

| 文件 | 角色 |
|---|---|
| `main.py` | 启动入口：装配核心 + 按配置/参数加载平台插件 |
| `agent_core.py` | **核心**：生命周期、插件注册、事件转发、大脑注册表 |
| `brain_base.py` | **大脑框架**：`AgentBrain` 接口 / `BrainManager` 注册表 / `brain.event` 统一事件 |
| `chat_service.py` | **聊天大脑**：命令分发、记忆、图片/语音/视频、意图识别、MC 注入、LLM 调用；平台无关 |
| `memory_server.py` / `memory_client.py` | **sidecar（RPC）**：向量记忆等重负载独立进程，掉线自动降级进程内调用 |
| `service_host.py` | **sidecar 基础设施**：通用本地 RPC 骨架（HTTP + asyncio），不 import 业务模块 |
| `monitor_server.py` / `monitor_client.py` | **监控 sidecar**：可观测性进程，聚合各 sidecar 健康度 |
| `ai_provider.py` | **多供应商抽象**：`capability` → 有序供应商列表，调用时故障转移（chat/reasoning/vision/tools）；支持思考强度档位、Anthropic 兼容、配置覆盖层热重载 |
| `agent_ctx.py` | **多智能体隔离**：基于 `contextvars` 维护「当前智能体」上下文，隔离各 agent 的记忆/身份，避免串台 |
| `plugin_base.py` | 插件系统：`Plugin` / `PlatformPlugin` / `FeaturePlugin` / `PluginManager` |
| `message_bus.py` | 消息契约：`InboundMessage` / `ReplyTarget` / `MessageSender` / 事件总线 |
| `qq_plugin.py` | QQ 平台插件：NapCat WebSocket、OneBot 协议、SILK 语音解码、私聊聚合 |
| `bilibili_plugin.py` / `bili_*` | B 站直播平台：主播(streamer)/观众(viewer) 两模式、弹幕 WS、字幕服务、推流 |
| `mc_bot_brain.py` / `mc_*.py` | MC 功能大脑：自主代理/导航/合成（与聊天平台无关，被 `chat_service` 调用） |
| `pc_agent.py` / `pc_control.py` | **电脑操控大脑**：截屏→视觉理解→工具调用→键鼠执行，四层护栏 + 急停 |
| `pvz_agent.py` / `pvz_*.py` | **PVZ 游戏大脑**：实时对抗自主循环 + 复盘 tips |
| `knowledge_store.py` / `self_knowledge.py` | 通用知识库 / 自我知识管理 |
| `identity.py` | QQ↔MC 游戏名双向确认绑定 |
| `emotion.py` | AI 情绪模块（心情状态 `emotion_data.json`） |

详细架构见 `libs/qq_bot_runtime/ARCHITECTURE.md` 与 `ARCHITECTURE_TREE.md`。

---

## 仓库结构

```
feiyu_standalone/
├── app.py                 # 肥鱼娘 App 入口：解析 qq_bot 运行时 → 启动核心桥与 HTTP 服务 → 打开桌面窗口
├── server.py              # 本机 HTTP 服务：静态前端 + REST API + SSE
├── start_app.bat          # 便携启动脚本（自举 venv / 捆绑 Python）
├── bridge/                # 后端桥接层
│   ├── core_bridge.py     # 核心桥：串联 App 与智能体
│   ├── app_window.py      # 桌面窗口（pywebview / Edge App → webui → 命令行 三级降级）
│   ├── loop.py            # 事件循环
│   ├── appearance_api.py  # 外观 API
│   ├── config_api.py      # 配置 API
│   ├── memory_api.py      # 记忆 API
│   ├── plugins_api.py     # 插件 API（含分组 / 市场 / 扩展设置）
│   ├── summary_api.py     # 总结 API
│   ├── builder_api.py     # 构建助手 API：对话式 Agent（工具循环 / 权限 / 审批 / 联网 / 工作区）
│   ├── pkg_manager.py     # 插件包管理（manifest 校验 / 依赖 / sidecar）
│   └── sidecar_runner.py  # 旁路进程运行器（日志落盘 + 端口就绪等待）
├── webui/                 # 原生 JS/CSS 前端控制台
│   ├── index.html
│   ├── app.js
│   └── styles.css
├── libs/qq_bot_runtime/   # 捆绑版 qq_bot 运行时（bot 引擎 + 捆绑 Python + venv + 各类资源）
│   ├── agent_core.py / chat_service.py / main.py / bot.py …   # 智能体核心
│   ├── mc_bot/            # Mineflayer 纯净版 MC 桥（bridge.js + mc_bot_brain.py）+ README
│   ├── mc_mod/feiyuapi/   # FeiyuAPI 模组源码（Gradle + NeoForge 1.21.1）
│   ├── webui/             # 运行时自带 WebUI（与根 webui/ 对应）
│   ├── runtime/           # 捆绑 Python 解释器与标准库
│   ├── 音色试听/ emojis/ data/ video_tmp/ voice_tmp/ …        # 资源与缓存
│   ├── ARCHITECTURE.md / ARCHITECTURE_TREE.md                 # 智能体架构文档
│   └── ai_providers.example.json / config.py / requirements*.txt
├── plugins/
│   ├── groups.json        # 插件依赖分组定义（见「插件系统」）
│   └── greeting_demo/     # 本地插件示例包（官方 UI 事件通道范本，见 PLUGINS.md）
├── feiyu-android/         # 肥鱼娘安卓版（Kotlin + Compose，独立工程）
├── dist/                  # 发布产物（见「发布与安装」；不在 git 内，走 GitHub Releases）
└── data/                  # 运行数据（用户隐私，git 忽略）
```

> ⚠️ `dist/`、`libs/.../runtime/`、`data/`、`__pycache__/`、缓存、安卓工程等已被 `.gitignore` 忽略，不入库。

---

## 快速开始

### 便携版（推荐，Windows）

直接双击 `start_app.bat`。脚本会：

1. 自举 `libs/qq_bot_runtime/venv/pyvenv.cfg`，把 Python `home` 重写为包内捆绑解释器（幂等）；
2. 设置 `FEIYU_QQ_BOT` 指向 `libs/qq_bot_runtime`，并加载包内 ffmpeg / silk 工具；
3. 以 `python app.py --with-core` 启动，自动打开桌面窗口。

窗口关闭即退出。

### 从源码运行（开发者）

```powershell
# 让 App 复用本仓库捆绑的 qq_bot 运行时（默认即指向 libs/qq_bot_runtime）
$env:FEIYU_QQ_BOT = "libs/qq_bot_runtime"
python app.py --with-core
```

运行时位置解析优先级：`$env:FEIYU_QQ_BOT` → `f:/feiyu_app/runtime/qq_bot` → `f:/qq_bot`（上述默认路径已满足，一般无需改动）。

---

## 配置

| 配置方式 | 文件 / 变量 | 说明 |
|---|---|---|
| 运行时配置覆盖层 | `app_settings.json` | 以 `setattr` 注入 `config` 模块；App 模式默认关闭外部功能 |
| 运行时位置 | 环境变量 `FEIYU_QQ_BOT` | 指向含 `config.py` 的 qq_bot 运行时目录 |
| LLM 供应商 | 模型注册表（侧栏「AI 供应商（模型管理）」）+ `app_settings.json` | 命名模型按 capability 路由 + 故障转移；不再写死 DeepSeek |
| B 站账号 | `SESSDATA` 等 | 风控层共享；填入后启用 B 站直播/私信/学习 |
| 功能开关 | `config.py` 内 `ENABLE_*` | 如 `ENABLE_MEMORY_SERVER`（向量记忆 RPC）、`ENABLE_MONITOR`（监控 sidecar）、`ENABLE_PVZ_BRAIN`（PVZ 大脑） |

---

## 功能模块

### 聊天大脑

`chat_service.py` 平台无关的聊天大脑：命令分发、记忆注入、图片/语音/视频理解、意图识别（如 `/电脑做`、`/mc`、`/总结`）、MC 指令注入、DeepSeek 调用。通过 `ai_provider` 的统一接口对接多 LLM，支持文本/视觉/工具/thinking 能力。

### 记忆子系统

分四类、主体区分严谨（用户 vs AI 肥鱼娘），避免写反/串台：

- **用户画像** `long_term_memory.py`：事实 facts，策略「新覆盖旧」（`merge_profile_facts`）。
- **AI 人格记忆** `persona_memory.py`：肥鱼娘与用户相处方式，策略「旧事实优先」——冲突的新增特征被否定（`reconcile_persona_traits`）。
- **对话反思** `reflection_memory.py`：按 `user_id` 键入（空 `user_id` 为全局共享），提炼交互规则。
- **重要备忘** `important_notes.py`：一次性重要信息。

> **运行期防串台**：记忆主体区分（用户 vs 肥鱼娘）之外，`chat_service` 在**每次模型调用前**给每条 `user` 消息加 `[说话人]` 前缀（`emotion.resolve_display_name`，空则回退 user_id / 频道标识 / "用户"），并在 `user_id` 缺失时于记忆注入处显式标注「当前对话对象」身份锚点——即使身份缺失，也不会把其它会话/来源的记忆混入。配合 `agent_ctx.py` 的多智能体隔离，杜绝跨会话串台。

向量记忆 `vector_memory.py` 经 `memory_server` sidecar 加速（embedding 模型加载、向量检索），开启 `ENABLE_MEMORY_SERVER` 走 RPC，掉线自动降级进程内调用。

### 总结

对话 / 会话总结生成，由 `summary_api.py` 暴露给前端。

### 插件系统

插件分**平台插件**（对接某个聊天/直播平台）与**功能插件**（核心增强）。依赖分组定义在 `plugins/groups.json`：

| 分组 | 标题 | 含包 | 依赖 |
|---|---|---|---|
| `core` | 核心引擎（必需） | `console_platform` `balance_monitor` `proactive_speaker` `greeting_demo` | 内置不可卸载 |
| `voice` | 本地语音 | `vox_tts` | `libs/voice_pack` 大件（9.7GB）+ N 卡；无 N 卡自动降级 GLM 云端 |
| `qq` | QQ 平台 | `qq_platform` | 目标机装有 QQ NT + NapCat（NapCat 在应急包 P3_NapCat） |
| `bilibili` | B 站全家 | `bilibili_platform` `bilibili_dm` `bilibili_learn` | 共享 SESSDATA 与风控层 |
| `mc` | Minecraft | `brain_mc_mod` `brain_mc_bot` | 模组世界需 feiyuapi mod / 原版需 mineflayer |
| `desktop` | 桌面与游戏 | `brain_pc` `brain_pvz` | PvZ 需 `PlantsVsZombies.exe` 放 `pvz_games/` |
| `services` | 后台服务 | `sidecar_memory` `sidecar_monitor` `sidecar_telemetry` | sidecar 子进程 |

包管理见 `bridge/pkg_manager.py`；插件包的 `manifest.json` 会在**装载前静态校验**（必填字段、`kind` 合法性、包名与目录一致、依赖字段、`schema_version` 兼容性），坏包不再静默消失（规则见 [`PLUGINS.md`](./PLUGINS.md)）。

### 构建助手（对话式 Agent）

侧栏「构建助手」是一个**像 Codex / WorkBuddy 那样边聊边改**的代码 / 构建 Agent：用自然语言说要做什么，它自己读代码、改文件、查语法、生成或改进智能体与插件，全程在对话框里可见。

```
┌─────────────┬──────────────────────────────┬─────────────┐
│ 工作区/历史/高级│  对话流                      │ 文件编辑器   │
│ 文件树       │  · 你的消息                  │ 读取 → diff  │
│ 点目录进入    │  · 💭 思考过程（可折叠）       │ → 保存      │
│ 点文件开编辑  │  · 工具卡片（参数 / diff / 结果）│ （写前自动备份）│
│ 行内「＋参考」 │  · 产物草稿卡片（可编辑→保存） │             │
│             │ ──────────────────────────── │             │
│             │ [待确认操作] [参考文件]        │             │
│             │ [输入框 ……………] [发送]       │             │
│             │ 模型 / 思考 / 访问权限 / 工作区 │             │
└─────────────┴──────────────────────────────┴─────────────┘
```

左栏可「« 收回 / ☰ 拉出」折叠（收起后**输入框仍贴底**，状态持久化）。

**工具循环**：后端把 21 项能力包装成 LLM 工具（列目录 / 读 / 写 / diff / 搜索 / 语法检查 / 列智能体 / 列插件 / 查历史 / 生成·改进·保存智能体 / 生成·改进·保存插件 / 查设置 / 联网 5 件套），由模型自主决定调用顺序，最多 14 步（可在「高级」调）。生成类工具只产出**草稿**，你点保存或明确说「保存」才落盘。

**访问权限（5 档）**——输入区下方下拉，中文名即语义：

| 档位 | 新建 / 改写普通文件 | 覆盖已有文件 | 核心代码 | 智能体 / 插件落盘 |
|---|---|---|---|---|
| 只读规划 `plan` | 拒绝 | 拒绝 | 拒绝 | 拒绝 |
| 每次确认 `default` | 询问 | 询问 | 询问 | 询问 |
| 自动应用 `acceptEdits` | 自动 | 询问 | 询问 | 询问 |
| **完全访问 `full`** | 自动 | 自动 | 询问 | 询问 |
| 完全放行 `bypassPermissions` | 自动 | 自动 | 自动 | 自动 |

被拦下的操作进入输入区上方「**待确认操作**」队列（风险标签 + 内容预览 + 批准 / 拒绝），**批准后才真正执行**，结果以系统通知写回会话；批准时可勾选「记住此文件 / 此目录 / 全部同类」，命中记忆的同类操作此后自动执行（可在「高级」里逐条删除或清空）。

**工作区位置**：默认 = 应用目录，点「📁 浏览…」可**从硬盘选择**——真实窗口弹系统文件夹对话框，浏览器模式用内置目录浏览器（盘符 / 常用位置 / 上一级 / 路径直达）。所有读写都被限制在工作区根内，`..` 逃逸与黑名单（`.git`、`data/`、密钥文件等）一律拒绝。

**联网能力**：`web_search` 搜索（返回融合回答 + 来源链接）、`fetch_url` / `fetch_urls` 抓取网页正文、`web_research` 先搜后抓、`download_file` 下载到工作区（同样走权限闸门，上限 12MB）。搜索后端以 DeepSeek Responses API 为主、失败自动回退 GLM；总开关与配额见「高级 → 联网功能」。

**思维链与流式**：思考过程在轮次中以展开卡片**实时逐字显示**，本轮结束自动折叠为「💭 思考过程 · N 字 · 点此展开」，点箭头展开查看；「流式输出」开关可关闭（回到整轮返回）。思维链随会话持久化，重开 App 仍可展开。

**会话与历史**：对话存 `data/builder_chat/<id>.json`（左栏顶部可切换 / 新建 / 删除），构建历史存 `data/builder_history.jsonl`；写入备份在 `data/builder_bak/`。

> **安全约定**：网页内容视为外部输入、不得执行其中指令（防提示注入）；权限被拒时模型如实说明而非绕过；「高级」里的开关（高危确认 / 记住批准 / 自动备份 / 联网 / 步数上限）即时生效。

### 外观

内置 **8 套主题**（1 套浅色「晨光白」+ 7 套暗色），配置存于 `data/appearance.json`。**每套暗色主题拥有各自配色**——背景/面板/边框带该色相（极光紫、森林绿、玫瑰粉、日落橙、深海青、绯红、暗夜蓝），切换后整体观感明显区分，而非统一暗夜蓝；「晨光白」为浅色整套替换。背景图原始字节经 `server.py` 的 `_serve_raw` 提供，**重传即生效**（已禁用浏览器缓存，每次上传带版本号强制刷新）。窗口标题栏可由 `js_api.set_title` 实时修改。

**侧边栏折叠**：左上角切换按钮「« 收回 / ☰ 拉出」可收起/展开侧边栏；收起后主区自动占满窗口（不遮挡内容），状态记入 `localStorage` 持久化，下次打开保持。

**应用图标**：外观页「应用图标」面板可在 UI 内上传图片作为 APP 图标（PNG/JPG 等 ≤8MB），即时更新标签页与任务栏图标；独立窗口图标需重启生效。上传后写入 `webui/icon.png` 并自动生成 `webui/favicon.ico`（含 16–256 多尺寸），默认图标备份于 `webui/icon_default.png` 可一键恢复。

### 模型注册表（LLM 供应商）

侧栏「**AI 供应商（模型管理）**」是统一的命名模型注册表，取代原先分散在配置页的供应商配置（配置页已不再含「AI 供应商」栏目）：

- **保存命名模型**：名称、厂商、接口类型、Base URL、API Key、模型名、能力（chat / reasoning / vision / role），写入 `app_settings.json` 覆盖层并热重载 `ai_provider`。
- **设置中切换默认**：每个能力（capability）的路由可置顶某模型作为默认（`/api/providers/activate` → `set_default`）。
- **构建助手实时切换**：构建助手 / 插件生成时通过 `provider` 参数实时指定供应商（不改全局路由），后端 `ai_provider.chat(provider=...)` 直通。
- **删除 / 编辑**：`/api/providers/delete`（`delete_model`）、`/api/providers`（`save_model`）。
- **思考强度**：构建助手内「模型」与「思考强度」为独立控件，思考强度分 **低 / 中 / 高** 三档，经 `_think_level` 归一化后下发 thinking 参数（high 档对 Anthropic 放大 think budget）。

---

## 插件协议（第三方接入规范）

> **第三方开发插件**：完整接入规范见仓库根目录 **[`PLUGINS.md`](./PLUGINS.md)**——包目录与 `manifest.json` 字段（含 `schema_version`、`pkg_requires`、`config_schema`）、装载前校验规则与 error/warning 语义、`platform` / `feature` / `brain` / `sidecar` / `local` 五类契约、消息与事件接口、**官方 UI 事件通道 `core.app_bridge`**、最小模板。

要点速记：

- **往 App 界面推事件**（插件显示消息 / 图片 / 语音 / 状态）：`bridge = getattr(core, "app_bridge", None)` → `bridge.push(session, {"type": "message", "text": ...})`；`push` 线程安全、`session=None` 为全局广播，**不要**用 `sys.modules` 取应用层内部对象。
- **manifest 校验**（装载前、不执行代码）：必填 `name`（必须等于目录名）/ `title` / `version` / `kind`；`kind ∈ platform|feature|brain|sidecar|local`；`schema_version` 当前为 **2**（缺失按 1 告警放行、高于本机则拒绝装载）；校验不过的包以 `kind: "invalid"` 列在插件页并附原因，不再静默消失。
- **依赖**：`requires`（引擎内建能力）/ `pkg_requires`（其它插件包，缺失则拒绝装载）/ `optional_requires`（软依赖，缺失仅告警）。
- **sidecar 包**：`manifest.sidecar = {script, host, port}`，启动后等待端口就绪；子进程输出落盘 `<qq_bot>/logs/sidecar_<name>.log`，`status()` 带 `log` 与 `exit_code`，便于排障。
- **参考实现**：`plugins/greeting_demo/`（自包含、走官方通道、带配置表单与热生效）。

---

## MC 功能（模组世界 + 原版世界）

MC 有**两套不同的身体/协议**，不要混用。

### 模组世界（FeiyuAPI 模组，必须插件）

模组版依赖自建的 **FeiyuAPI 模组**，大脑通过它暴露的本地接口读玩家/世界状态、下发指令；未加载时会提示「游戏没开或 FeiyuAPI 模组没加载」。

- **模组文件**：`feiyuapi-1.0.0.jar`（NeoForge 1.21.1）。
- **随包附带**：`mc_pack/mods/feiyuapi-1.0.0.jar`，放进游戏实例的 `mods/` 目录（游戏须为 NeoForge 1.21.1，且与模组编译版本一致）。
- **源码**：`mc_mod/feiyuapi/`（Gradle + `net.neoforged.moddev`；Minecraft `1.21.1` / NeoForge `21.1.248` / Java 21）。重构建：
  ```powershell
  cd libs/qq_bot_runtime/mc_mod/feiyuapi
  ./gradlew build   # 产物 build/libs/feiyuapi-1.0.0.jar
  ```
- 大脑在原版摸索出的技巧经共享技巧库沉淀，模组版每轮注入「相关经验」复用；技能步骤按 `env` 过滤，跨环境只提示做法、不硬跑。

### 原版世界（Mineflayer 纯净版试验场）

无 mod 的纯净 LAN 世界，给 LLM 一个流畅身体（真寻路/原生挖掘/合成）。详见 `libs/qq_bot_runtime/mc_bot/README.md`：

```powershell
cd libs/qq_bot_runtime/mc_bot
npm install                       # 首次装依赖（Node v24 验证）
$env:MC_HOST="127.0.0.1"; $env:MC_PORT="25565"; $env:MC_USER="feiyu_bot"
node bridge.js                   # 游戏内「对局域网开放」后填入端口
```
桥动作层（`/state` 观察、`/cmd` 动作）已含自保反射 + 看门狗、只读一轮一答、阶梯式下挖找矿、视线校验、设施账本、地标分桶等机制；大脑在 `mc_bot_brain.py`（`python mc_bot_run.py` 启动），共用 Python 侧记忆/技巧资产。

---

## 安卓版

`feiyu-android/`（Kotlin + Jetpack Compose 原生 App）把主包 + 最基础聊天核心移植到手机：聊天核心在端侧直连 DeepSeek / Gemini / GLM / OpenAI 兼容接口，附带本地 TF-IDF 向量记忆（RAG）。构建见该目录 `feiyu-android/README.md`（Android Studio 打开 → `./gradlew assembleDebug` 生成 `app-debug.apk`，minSdk 26 / Android 8.0+）。本机无需 Python 运行时。

---

## 安全边界与合规

这一节说明「这个程序会动什么、不会动什么」，便于在把本机交给它之前做判断。

**权限模型（构建助手）**：五档访问权限（只读规划 / 每次确认 / 自动应用 / 完全访问 / 完全放行）见[上文](#构建助手对话式-agent)。所有写入都限制在**工作区根目录**内，越界（`..`）、黑名单（`.git` / `data/` / `ai_providers.json` / `user_profiles.json`）一律拒绝；覆盖已有文件、改核心代码、插件落盘属于高危操作，默认进「待确认」队列等人工批准，写入前自动备份到 `data/builder_bak/`。构建助手的工具调用受同一套闸门约束，不因是"AI 自己发起"而放宽。

**网络暴露面**：HTTP 服务仅监听 `127.0.0.1`，并用 `SO_EXCLUSIVEADDRUSE` 保证单实例；不对局域网/公网开放，不做端口转发。模型调用、联网搜索由本机主动出站发起。

**数据与密钥**：密钥只放在 `libs/qq_bot_runtime/ai_providers.json`（覆盖层，已在 `.gitignore` 内，不进版本控制），`config.py` 不写死真实 Key。聊天记录、记忆档案、用户画像等均落在本机 JSON（`data/` 等），不上传第三方；清除即删除本机文件。

**本机高权限能力**：电脑操控（截屏 + 键鼠）、MC 世界控制、语音合成等会操作本机窗口与输入，属于高权限动作，建议只在明确需要时启用对应插件；这些能力不经过远端服务，能力范围即所在机器的当前用户权限。

**第三方平台使用**：QQ 接入（NapCat / OneBot）与 B 站相关功能属于第三方平台的**自动化使用**，涉及各家服务条款与风控策略；本项目只使用公开/匿名接口（例如 B 站匿名 `buvid`），不实现绕过登录、验证码或风控的机制。请自行评估账号风险并遵守平台条款，因使用导致的账号限制由使用者承担。

**无担保声明**：本项目是单人维护的开源作品，以 [MIT 许可](#许可证)发布，按「现状」提供、不承诺商用级稳定性；请勿直接用于需要合规审计、数据留存要求或无人值守的生产环境。

---

## 测试 / 回归

**引擎层**：`libs/qq_bot_runtime/tests/`（标准库 `unittest`，`run.py` 一键发现并运行），覆盖配置中心（覆盖层合并 / `${ENV}` 解析 / 零回归回落 / 热重载）、启动器 sidecar 管理、记忆浏览、插件注册表、遥测服务、Web 插件 API 等。

```powershell
# 用装有完整依赖的 Python（cv2 / torch / transformers / Pillow 等）运行；当前 90 项全过
python libs/qq_bot_runtime/tests/run.py
```

**App 层**：`tests/`（同样用标准库 `unittest`，无需安装 pytest），覆盖构建助手的工具契约（数量/名称/schema 形状，防止误删工具）、权限闸门（5 档 × 新建/覆盖/核心代码/落盘/下载）、批准记忆范围（文件/目录/全部，开关失效）、路径安全（越界/黑名单/白名单/自定义工作区）、上下文文件白名单与根目录文档可读、manifest 校验（合法 + 8 类问题包）、sidecar 就绪探测等。

```powershell
tests\run_tests.bat            # 双击亦可，自动挑一个可用解释器
# 或
python -m unittest discover -s tests -v      # 当前 43 项全过
```

> 视觉/向量等重依赖模块需在完整依赖环境下测试；若仅用捆绑精简 Python，相关用例可能无法完整加载。CI / 本机验证均建议使用带重依赖的 venv。

---

## 发布与安装

发布产物在 `dist/`，通过 **GitHub Releases** 作为附件分发（`dist/` 不在 git 内，因其单文件普遍 ≥100MB）。`dist/SHA256SUMS.txt` 记录各包 sha256，下载后用于校验完整性。分卷产物（`.part1` …）需先合并为整 zip 再校验。

| 包 | 内容 | sha256（合并目标） |
|---|---|---|
| `feiyu_core`（分卷 `feiyu_core.part1/2/3`） | 主程序核心：App + 后端 + qq_bot 运行时 + 捆绑 Python/venv | `c8e79e11800e6cd7ce13bae3bff526b0ab41280b448bad8bc1662516009bf211` |
| `mc_pack.zip` | MC 模组世界（feiyuapi jar + mc_mod 源码）+ 原版 mineflayer 桥 | `0e5871abf9ef49c703ba3bac3b7f41ec7a3fe46542db43adf6f33fd4528a2715` |
| `napcat_pack.zip` | NapCat（QQ 接入）配置 / 应急包 P3_NapCat | `c9355d17aab4b0694525e88a388452dda7f33cdba0f14528ffdfe043e24d9e8d` |
| `plugins_pack.zip` | 插件包（按 groups 分组） | `a8ffcf138829b23758cd5be801f9c6e05680607b2eda30aabf3e221d0b391973` |
| `tools_pack.zip` | 工具（ffmpeg / silk 编解码等） | `077393fdadf6615324475ec62c312279b74dcb53484cc87888e19a025ead8e1e` |
| `vl_pack_7z`（分卷 `part1-4`） | 视觉语言模型相关大件 | 合并后校验（`SHA256SUMS.txt`） |
| `voice_pack_7z`（分卷 `part1-3`） | VoxCPM2 本地 TTS 模型（9.7GB，需 N 卡） | 合并后校验（`SHA256SUMS.txt`） |

**安装**：下载所需 release 包与 `SHA256SUMS.txt` → 合并分卷（`copy /b *.part* merged.zip`）→ 用 `SHA256SUMS.txt` 校验 → 解压合并 → 运行其中 `start_app.bat`。基础运行仅需 `feiyu_core` + `tools_pack`；按需叠加 `mc_pack`（MC）、`napcat_pack`（QQ）、`plugins_pack`（插件）、`voice_pack`（本地语音，需 N 卡）、`vl_pack`（视觉语言）。

---

## 常见问题

- **窗口打不开**：启动窗口为三级降级 —— 优先 APP 独立窗口（pywebview → Edge `--app`），不可用则退回 webui（系统默认浏览器），仍失败则仅起 HTTP 服务 + 命令行（Ctrl+C 退出）。可用 `--browser` 强制 webui、`--no-window` 强制命令行。
- **窗口打不开 / 端口占用**：HTTP 服务仅本机监听且 `SO_EXCLUSIVEADDRUSE` 保证单实例；确认没有另一份已在运行。
- **QQ 接入提示缺 NapCat**：装好 QQ NT 客户端，并按 `napcat_pack` 应急包配置 NapCat（OneBot）。
- **本地语音无声音 / 报错**：`voice` 组需 `voice_pack`（9.7GB）+ N 卡；无 N 卡会自动降级 GLM 云端 TTS。
- **MC 模组版提示「FeiyuAPI 模组没加载」**：确认把 `feiyuapi-1.0.0.jar` 放进游戏实例 `mods/`，且游戏为 NeoForge 1.21.1。
- **记忆写反 / 串台**：记忆子系统已做主体区分（用户 vs 肥鱼娘），人格记忆采用旧事实优先策略；若异常请反馈。
- **构建助手一直要确认 / 改不动文件**：由「访问权限」档位决定——「每次确认」所有写入都需批准，「完全访问」只对核心代码与落盘提问；被拦下的操作在输入框上方「待确认操作」里批准即可，也可勾选「记住此文件 / 此目录 / 全部同类」减少重复询问（在「高级」里可清除记忆）。
- **构建助手报「无可用供应商」或工具调用失败**：该模型需支持工具调用（tools）。换用支持工具调用的模型，或把权限设为「只读规划」让它只给方案不动手。
- **构建助手联网搜索失败**：「高级 → 联网功能」是否开启；搜索需要 DeepSeek（Responses API）或 GLM 密钥之一，缺失时工具会返回明确原因。
- **改了 `webui/` 但界面没变**：静态资源已带 `Cache-Control: no-store`；若仍为旧界面，重启实例（`app.py` 重启会自动重开窗口）。

**开发与运维约定**

- 中文提交信息以 UTF-8 写入文件并经 `git commit -F` 提交，避免终端编码乱码。
- 网络受限环境下 GitHub 443 直连可能不通；可改用 SSH 协议（`git@github.com:hth768/Fat-Fish.git`）。
- 打包大件走 GitHub Releases 附件，不进 git；`dist/` 在 `.gitignore` 内。

---

## 许可证

本项目以 **MIT License** 发布，全文见 [`LICENSE`](./LICENSE)：

- ✅ 可自由使用、修改、商用、再分发，也可闭源集成；
- ✅ 唯一要求：保留版权声明与许可声明（在副本或实质性部分中附上本许可全文）；
- ⚠️ 软件按「现状」提供，不含任何明示或默示担保；作者不对使用后果负责。

第三方组件（NapCat、Mineflayer、ffmpeg、各模型与数据集等）各自遵循其原始许可，
不在本许可覆盖范围内；`libs/`、`voice_pack`、`vl_pack` 等打包产物内如含第三方文件，
请以各自 `LICENSE` 为准。引用本项目的名字、图标或角色形象用于宣传时，请注明来源。
