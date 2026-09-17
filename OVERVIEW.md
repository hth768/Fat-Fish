# 肥鱼单机版 · 速览（Overview）

> 一页纸看懂 Feiyu Standalone。完整文档见 [README.md](./README.md)。

肥鱼娘智能体桌面控制台（Feiyu Standalone）：后端 Python（本机 HTTP + 智能体核心），前端 `pywebview`/Edge 窗口承载的原生 JS/CSS 控制台（`webui/`）。**所有能力在本机单进程内运行，数据留本机，无需公网。**

- **核心架构**：「智能体核心 + 插件系统」，QQ / B 站等平台即插件；`AgentCore.brains` 统一注册与管理各大脑生命周期与事件通道。
- **聊天大脑** `chat_service.py`：命令分发、记忆注入、图片/语音/视频理解、意图识别、MC 注入，经 `ai_provider` 对接多 LLM（含 thinking 档位）。
- **记忆子系统**：用户画像 / AI 人格 / 对话反思 / 重要备忘，主体区分严谨 + 运行期防串台。
- **插件系统**：平台插件（QQ / B 站）与功能插件（余额监控 / 主动说话 / 定时），按 `plugins/groups.json` 分组。
- **外观自定义**：8 套主题（暗色各自配色、1 套浅色）、背景图（重传即生效）、应用图标、窗口标题；侧边栏可「« 收回 / ☰ 拉出」折叠并持久化。
- **MC 功能**：模组世界（FeiyuAPI 模组）+ 原版世界（Mineflayer），两套独立身体/协议。
- **安卓版** `feiyu-android/`：Kotlin + Compose 端侧精简移植，本地 TF-IDF 向量记忆。

## 运行

- 便携版：双击 `start_app.bat`。
- 源码：`python app.py --with-core`（设 `FEIYU_QQ_BOT` 指向 qq_bot 运行时）。
- 运行数据（`data/`、缓存、`dist/`、`.gitignore` 项）不入库。

## 仓库地址

`hth768/Fat-Fish` —— 包管理器：无（便携自举 `venv` / 捆绑 Python）；智能体核心复用 `libs/qq_bot_runtime`（qq_bot 运行时）。
