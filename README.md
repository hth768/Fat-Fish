# 肥鱼单机版 · 肥鱼娘 App（Feiyu Standalone）

> 肥鱼娘智能体桌面控制台。后端为 Python（本机 HTTP 服务 + 智能体核心），前端为 `pywebview` / Edge App 窗口承载的原生 JS/CSS 控制台（`webui/`）。所有能力（聊天、记忆、总结、插件、外观、配置、MC、B 站、电脑操控……）均在**本机单进程**内运行，数据留在本机，无需公网。

- 仓库地址：`hth768/Fat-Fish`
- 包管理器：无（便携自举 `venv` / 捆绑 Python）；智能体核心复用 `libs/qq_bot_runtime`（qq_bot 运行时）

---

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
  - [外观](#外观)
- [插件协议（第三方接入规范）](#插件协议第三方接入规范)
- [MC 功能（模组世界 + 原版世界）](#mc-功能模组世界--原版世界)
- [安卓版](#安卓版)
- [发布与安装](#发布与安装)
- [常见问题](#常见问题)

---

## 架构

### 桌面 App 分层

```
┌──────────────────────────────────────────────────────┐
│  桌面窗口（pywebview / Edge App / 浏览器三级降级）        │
│  └── webui/（原生 JS/CSS 控制台，localhost 访问）         │
└────────────────────────┬─────────────────────────────┘
                         │ HTTP REST + SSE（仅监听 127.0.0.1）
┌────────────────────────▼─────────────────────────────┐
│  server.py：静态前端托管 + REST API + SSE 事件流          │
│  bridge/：API 路由 + 窗口 + 核心循环（CoreBridge）         │
│  libs/qq_bot_runtime：qq_bot 智能体运行时（bot 引擎）       │
└─────────────────────────────────────────────────────┘
```

- **前端** `webui/index.html · app.js · styles.css`：侧栏含 仪表盘 / 聊天 / 记忆 / 总结 / 插件 / 配置 / 外观。
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
| `ai_provider.py` | **多供应商抽象**：`capability` → 有序供应商列表，调用时故障转移（chat/reasoning/vision/tools） |
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
│   ├── app_window.py      # 桌面窗口（pywebview / Edge / 浏览器降级）
│   ├── loop.py            # 事件循环
│   ├── appearance_api.py  # 外观 API
│   ├── config_api.py      # 配置 API
│   ├── memory_api.py      # 记忆 API
│   ├── plugins_api.py     # 插件 API
│   ├── summary_api.py     # 总结 API
│   ├── pkg_manager.py     # 插件包管理
│   └── sidecar_runner.py  # 旁路进程运行器
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
│   └── groups.json        # 插件依赖分组定义（见「插件系统」）
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
| LLM 供应商 | `ai_providers.example.json` → `ai_providers.json` | 多供应商（DeepSeek / Gemini / GLM / 自定义）按 capability 路由 + 故障转移 |
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

包管理见 `bridge/pkg_manager.py`。

## 插件协议（第三方接入规范）

> **第三方开发插件**：完整接入规范（包目录、`manifest.json` 字段、`platform`/`feature`/`brain`/`sidecar`/`local` 四类契约、消息/事件接口、最小模板）见仓库根目录 **[`PLUGINS.md`](./PLUGINS.md)**。

### 外观

内置 **8 套主题**，配置存于 `data/appearance.json`；背景图原始字节经 `server.py` 的 `_serve_raw` 提供；窗口标题栏可由 `js_api.set_title` 实时修改。

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

- **窗口打不开 / 端口占用**：HTTP 服务仅本机监听且 `SO_EXCLUSIVEADDRUSE` 保证单实例；确认没有另一份已在运行。
- **QQ 接入提示缺 NapCat**：装好 QQ NT 客户端，并按 `napcat_pack` 应急包配置 NapCat（OneBot）。
- **本地语音无声音 / 报错**：`voice` 组需 `voice_pack`（9.7GB）+ N 卡；无 N 卡会自动降级 GLM 云端 TTS。
- **MC 模组版提示「FeiyuAPI 模组没加载」**：确认把 `feiyuapi-1.0.0.jar` 放进游戏实例 `mods/`，且游戏为 NeoForge 1.21.1。
- **记忆写反 / 串台**：记忆子系统已做主体区分（用户 vs 肥鱼娘），人格记忆采用旧事实优先策略；若异常请反馈。

**开发与运维约定**

- 中文提交信息以 UTF-8 写入文件并经 `git commit -F` 提交，避免终端编码乱码。
- 网络受限环境下 GitHub 443 直连可能不通；可改用 SSH 协议（`git@github.com:hth768/Fat-Fish.git`）。
- 打包大件走 GitHub Releases 附件，不进 git；`dist/` 在 `.gitignore` 内。
