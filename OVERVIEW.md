# 肥鱼单机版 · 速览（Overview）

> 一页纸看懂 Feiyu Standalone。完整文档见 [README.md](./README.md)。

肥鱼娘智能体桌面控制台（Feiyu Standalone）：后端 Python（本机 HTTP + 智能体核心），前端 `pywebview`/Edge 窗口承载的原生 JS/CSS 控制台（`webui/`）。**所有能力在本机单进程内运行，数据留本机，无需公网。**

- **核心架构**：「智能体核心 + 插件系统」，QQ / B 站等平台即插件；`AgentCore.brains` 统一注册与管理各大脑生命周期与事件通道。
- **聊天大脑** `chat_service.py`：命令分发、记忆注入、图片/语音/视频理解、意图识别、MC 注入，经 `ai_provider` 对接多 LLM（含 thinking 档位）。
- **记忆子系统**：用户画像 / AI 人格 / 对话反思 / 重要备忘，主体区分严谨 + 运行期防串台。
- **插件系统**：平台插件（QQ / B 站）与功能插件（余额监控 / 主动说话 / 定时），按 `plugins/groups.json` 分组；`manifest.json` 装载前静态校验（`schema_version` 2、`name` 必须等于目录名，坏包以 `kind:invalid` 列出而非静默消失），依赖分硬（`requires` / `pkg_requires`）软（`optional_requires`）。
- **构建助手（对话式 Agent）**：像 Codex / WorkBuddy 那样**边聊边改**——21 个工具的工具循环（读写源码 / diff / 语法检查 / 生成与改进智能体·插件）；**5 档访问权限**（只读规划 / 每次确认 / 自动应用 / 完全访问 / 完全放行）+ 高危操作（覆盖、核心代码、落盘）进「待确认」队列、批准可记忆；工作区可**从硬盘选择**；支持**联网**搜索 / 抓取 / 下载、**思维链**展示（本轮结束自动折叠、点箭头展开）与**流式输出**（可开关）；左栏可折叠且收起后输入框仍贴底。
- **插件协议**：第三方插件经**官方 UI 事件通道** `core.app_bridge` 往界面推消息 / 图片 / 语音 / 状态（线程安全、无需 hack 内部对象）；sidecar 包输出落盘 `logs/`、启动等待端口就绪。规范见 [`PLUGINS.md`](./PLUGINS.md)，范本见 `plugins/greeting_demo/`。
- **外观自定义**：8 套主题（暗色各自配色、1 套浅色）、背景图（重传即生效）、应用图标、窗口标题；侧边栏可「« 收回 / ☰ 拉出」折叠并持久化。
- **MC 功能**：模组世界（FeiyuAPI 模组）+ 原版世界（Mineflayer），两套独立身体/协议。
- **安卓版** `feiyu-android/`：Kotlin + Compose 端侧精简移植，本地 TF-IDF 向量记忆。
- **安全与测试**：HTTP 仅监听 `127.0.0.1` 单实例、密钥只在本机 `ai_providers.json`（不入库）、写入工作区外/黑名单一律拒绝、危险操作需人工批准；测试分两层 —— 引擎 `libs/qq_bot_runtime/tests/`（90 项）+ App `tests/`（43 项，`tests\run_tests.bat` 一键跑）。边界与第三方平台使用说明见 README「[安全边界与合规](./README.md#安全边界与合规)」。
- **许可**：**MIT License**（[`LICENSE`](./LICENSE)）—— 可自由使用 / 修改 / 商用 / 再分发，保留版权与许可声明即可。第三方组件（NapCat、Mineflayer、各模型等）遵循各自许可。

## 运行

- 便携版：双击 `start_app.bat`。
- 源码：`python app.py --with-core`（设 `FEIYU_QQ_BOT` 指向 qq_bot 运行时）。
- 运行数据（`data/`、缓存、`dist/`、`.gitignore` 项）不入库。

## 仓库地址

`hth768/Fat-Fish` —— 包管理器：无（便携自举 `venv` / 捆绑 Python）；智能体核心复用 `libs/qq_bot_runtime`（qq_bot 运行时）。
