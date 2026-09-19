# 功能全量对比：feiyu_app（Fat-Fish）vs D:\testing（Pal-AI-Lab 组织）

生成时间：2026-09-19
对比对象：
- **A 侧** `E:\feiyu_app` —— hth768/Fat-Fish，Python 单机版多平台 Bot 平台（运行实例，含 bridge/webui/plugins）
- **B 侧** `D:\testing` —— Pal-AI-Lab 组织 7 仓库：Cortico（核心框架）+ cortico-world-mc-agent / cortico-world-memory / cortico-world-vtuber（World 扩展）+ Cortina / META-TINA / ThereIsNoApp（TINA 规范与示例）

> 注：B 侧是"Agent 框架生态"，A 侧是"已成型 Bot 产品"。二者赛道不同，对比按**功能维度**横向评优劣，而非同名模块 diff。

---

## 一、功能模块清单

### A 侧 feiyu_app 已实现功能（从 bridge/*.py + plugins/ 实测）

| 功能域 | 模块 | 具体能力 |
|--------|------|----------|
| 多平台接入 | `qq_platform` / `bilibili_platform` / `console_platform` | QQ、B站、控制台三端；bot_manager 统一调度 |
| 插件系统 | `pkg_manager.py` + `plugins_api.py` + 16 个插件包 | 插件扫描/启用/禁用；插件含 manifest.json + plugin.py |
| 构建助手 | `builder_api.py` | 生成 Agent / 生成插件 / 改进 Agent / 改进插件 / 保存 / 工作区读写 / 构建历史 few-shot |
| 长期记忆 | `memory_api.py` | 用户档案(profiles)/备忘(notes)/人格(persona)/反思(reflection)/知识库(knowledge)/对话历史(history)/会话(sessions) |
| 外观定制 | `appearance_api.py` | 主题色、侧边栏折叠、桌面独立窗口 `app_window.py` |
| 模型供应商 | `provider_api.py` | 多模型端点切换 |
| 总结 | `summary_api.py` | 对话摘要 |
| 核心桥接 | `core_bridge.py` / `loop.py` | 事件循环、与 qq_bot 引擎桥接 |
| 侧车能力 | `sidecar_*` 插件 | 记忆/监控/遥测 sidecar |
| 具体插件 | 16 个 | balance_monitor(余额监控)、bilibili_dm/learn(弹幕/学习)、brain_mc_bot/brain_mc_mod/brain_pc/brain_pvz(游戏大脑)、greeting_demo、proactive_speaker(主动发言)、vox_tts(语音合成) 等 |

### B 侧 D:\testing 功能（从 README/AGENTS.md 实测）

| 仓库 | 功能定位 |
|------|----------|
| **Cortico** | Agent 核心框架：Core/Persona/World/Bot 四层；扩展点 = World/Provider/Bot（npm 包）；控制台 `src/web/`；部署隔离（deployments/ 不入库） |
| **cortico-world-mc-agent** | Minecraft 自主生存智能体：mineflayer 身体 + Cortico 人格驱动；21 个工具（mc_goto/dig/attack/craft/place/chest/furnace…）|
| **cortico-world-memory** | 记忆 World：对接 qq_bot 记忆系统 |
| **cortico-world-vtuber** | VTuber 世界接入 |
| **Cortina** | TINA 示例 App（v0.1.0 发布件）|
| **META-TINA** | "做 App 的 App"：把专家领域知识访谈成 TINA 应用 |
| **ThereIsNoApp** | TINA 规范 v0.1 草案（AGENTS.md 工作区格式 + 协议不变量 I1–I7）|

---

## 二、分维度优劣对比

### 维度 1：Agent / 插件 生成能力

| | feiyu_app (A) | Pal-AI-Lab (B) |
|---|---|---|
| 机制 | `builder_api` LLM 直接生成 manifest+code，支持"改进已有"迭代 | Cortico 靠 coding agent 写扩展包；META-TINA 靠访谈式生成 TINA App |
| 迭代闭环 | ✅ 强：generate → save → improve → 工作区读写 → 再 improve（已验证） | ⚠️ 中：agent 写代码，但无内置"改进/版本回退"API |
| 上手成本 | ✅ 低：UI 点选即生成 | ❌ 高：需懂 TS、写 npm 包、配 World/Bot/Provider |
| 优劣 | **A 优**：开箱即用、有 UI、有历史 few-shot | **B 优**：生成物是标准 npm 包，可发布到组织，生态化 |

### 维度 2：记忆系统

| | feiyu_app | Pal-AI-Lab |
|---|---|---|
| 结构 | profiles/notes/persona/reflection/knowledge/history 6 类，REST API 可读写 | Cortico Memory = 目录 + blob handle（Persona 选路径）；cortico-world-memory 对接 qq_bot 记忆 |
| 持久化 | ✅ 落地 JSON，有 memory_api 全文检索 | ⚠️ 部署目录不入库，依赖运行时 |
| 优劣 | **A 优**：开箱即用、结构化、可查询 | **B 优**：架构解耦（Core 只认目录路径，不绑实现）|

### 维度 3：多平台 / 世界接入

| | feiyu_app | Pal-AI-Lab |
|---|---|---|
| 接入面 | QQ / B站 / 控制台（即时通讯向）| Minecraft / VTuber /（可扩 World）|
| 扩展性 | 插件包模式，加 platform 插件即可 | World 扩展点，npm 包即插即用 |
| 优劣 | **A 优**：已有 3 端 + 16 插件，直接跑 | **B 优**：World 抽象更通用（游戏/虚拟人都能接），不止 IM |

### 维度 4：模型供应商管理

| | feiyu_app | Pal-AI-Lab |
|---|---|---|
| 机制 | `provider_api.py` 多端点切换 | `src/providers/` ProviderModule 扩展点 |
| 优劣 | 持平：A 有 UI 切换；B 有扩展点解耦 |

### 维度 5：人机协作 / 可观测性

| | feiyu_app | Pal-AI-Lab |
|---|---|---|
| 协作 | 主动发言插件、侧边栏、外观定制 | AGENTS.md 协议、控制台 `src/web/`、TINA"访谈式" |
| 优劣 | **A 优**：产品化 UI、用户可直接用 | **B 优**：协议化（I1–I7 不变量）、适合 agent 自治 |

### 维度 6：工程成熟度 / 可维护性

| | feiyu_app | Pal-AI-Lab |
|---|---|---|
| 类型系统 | ❌ Python 动态，无编译期检查 | ✅ TypeScript，CI 跑在 Node≥22 |
| 测试 | ❌ 未见测试套件 | ✅ Cortico 有 CI workflow |
| 部署隔离 | ⚠️ 运行态污染（data/ 曾混入仓库）| ✅ deployments/ 明确不入库 |
| 优劣 | **B 优**：TS + CI + 部署隔离，工程规范更强 | — |

### 维度 7：生态 / 组织化

| | feiyu_app | Pal-AI-Lab |
|---|---|---|
| 组织 | 个人单仓库 | 7 仓库组织，扩展包命名规范（cortico-world-/cortico-provider-/cortico-bot-）|
| 优劣 | **B 优**：组织化、可组合、可发布 | — |

---

## 三、总结评分（5 分制，按功能维度）

| 维度 | feiyu_app (A) | Pal-AI-Lab (B) | 胜方 |
|------|:---:|:---:|:---:|
| Agent/插件生成（开箱即用）| 5 | 2 | A |
| 记忆系统（结构化/可查）| 5 | 3 | A |
| 多平台接入（已跑通）| 4 | 3 | A |
| 世界抽象（通用性）| 2 | 5 | B |
| 模型供应商 | 3 | 3 | 平 |
| 人机协作 UI | 5 | 2 | A |
| 工程成熟度（TS/CI/隔离）| 2 | 5 | B |
| 生态组织化 | 2 | 5 | B |
| **合计** | **28** | **28** | **平** |

---

## 四、核心结论

1. **赛道不同，各擅胜场**：
   - feiyu_app 是**面向终用户的 Bot 产品**——开箱即用、有 UI、有 16 个现成插件、有完整记忆与构建助手。优势在"易用性"与"功能完整度"。
   - Pal-AI-Lab 是**面向开发者的 Agent 框架生态**——TypeScript + 扩展点 + 组织化。优势在"架构优雅"与"可扩展性"。

2. **feiyu_app 的明显短板**（应借鉴 B 侧）：
   - 无类型系统 / 无 CI / 部署态与代码态未隔离（data/ 污染曾入仓库）
   - 插件无标准包规范，难组织化发布
   - 世界/平台抽象弱（只接 IM，游戏/虚拟人需硬写插件）

3. **Pal-AI-Lab 的明显短板**（应借鉴 A 侧）：
   - 无开箱即用的生成 UI，"改进/回退"无内置 API
   - 记忆系统未产品化（无查询 API、依赖运行时）
   - 两个 World 仓库（mc-agent/memory）**尚未提交**，有丢失风险

4. **互补建议**：若要让 feiyu 接 Minecraft/VTuber，可直接复用 `cortico-world-*` 的 mineflayer 身体层；若要让 Cortico 有"对话式生成 Bot"能力，可参考 feiyu `builder_api` 的 generate→improve→工作区读写闭环。

---

## 五、附：本次未覆盖项

- B 侧 `cortico-world-vtuber` / `Cortina` / `META-TINA` 内部实现未逐文件读（仅 README/AGENTS 层面）
- A 侧 `feiyu_app` 运行时行为未跑（仅静态 API 面分析）
- 性能、并发、成本未测（无运行数据）
