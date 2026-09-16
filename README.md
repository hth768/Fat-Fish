# 肥鱼单机版 · 肥鱼娘 App（Feiyu Standalone）

肥鱼娘智能体桌面控制台。后端为 Python（本机 HTTP 服务 + 智能体核心），前端为 `pywebview` / Edge App 窗口承载的原生 JS/CSS 控制台（`webui/`）。所有能力（聊天、记忆、总结、插件、外观、配置）均在**本机单进程**内运行，数据留在本机，无需公网。

仓库地址：`hth768/Fat-Fish`

## 架构

```
┌─────────────────────────────────────────────┐
│  桌面窗口（pywebview / Edge App / 浏览器三级降级）│
│  └── webui/（原生 JS/CSS 控制台，localhost 访问）  │
└───────────────────┬─────────────────────────┘
                    │ HTTP REST + SSE（仅监听 127.0.0.1）
┌───────────────────┴─────────────────────────┐
│  server.py：静态前端托管 + REST API + SSE 事件流   │
│  bridge/：API 路由 + 窗口 + 核心循环（CoreBridge）   │
│  libs/qq_bot_runtime：qq_bot 智能体运行时（bot 引擎） │
└─────────────────────────────────────────────┘
```

- **前端**：`webui/index.html · app.js · styles.css`，侧栏含 仪表盘 / 聊天 / 记忆 / 总结 / 插件 / 配置 / 外观。
- **后端 HTTP**：`server.py` 仅本机监听，托管静态前端并暴露 REST API 与 SSE 实时事件流。
- **智能体核心**：复用 `libs/qq_bot_runtime`（即 qq_bot 运行时）的智能体核心，App 以库方式调用，运行时零源码改动。

## 目录结构

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
├── libs/qq_bot_runtime/   # 捆绑版 qq_bot 运行时（含 venv、捆绑 Python、bot 引擎、mc 模块）
├── plugins/               # 插件配置（如 groups.json）
├── dist/                  # 发布产物（见「发布与安装」；不在 git 内，走 GitHub Releases）
└── data/                  # 运行数据（用户隐私，git 忽略）
```

> **两份 bot 引擎**：捆绑版 `libs/qq_bot_runtime`（随仓库）与独立版 `e:\qq_bot`（仓库外）是同一套代码的两份副本。改 bot 逻辑须两份同步，否则运行版与独立版行为会不一致。

## 快速开始

### 便携版（推荐，Windows）
直接双击 `start_app.bat`。脚本会：
1. 自举 `libs/qq_bot_runtime/venv/pyvenv.cfg`，把 Python `home` 重写为包内捆绑解释器（幂等）；
2. 设置 `FEIYU_QQ_BOT` 指向 `libs/qq_bot_runtime`，并加载包内 ffmpeg / silk 工具；
3. 以 `python app.py --with-core` 启动，自动打开桌面窗口。

### 从源码运行（开发者）
```powershell
# 让 App 复用某个 qq_bot 运行时（不指定则用默认优先级探测）
$env:FEIYU_QQ_BOT = "e:\qq_bot"      # 或任意含 config.py 的 qq_bot 目录
python app.py --with-core
```
运行时位置解析优先级：`$env:FEIYU_QQ_BOT` → `f:/feiyu_app/runtime/qq_bot` → `f:/qq_bot`。

窗口关闭即退出。HTTP 服务仅本机可访问（`SO_EXCLUSIVEADDRUSE` 保证单实例）。

## 功能模块

- **聊天**：与肥鱼娘智能体对话，接驳 qq_bot 核心。
- **记忆**：用户画像（事实 facts）、AI 人格记忆、对话反思、重要备忘——主体区分严谨（用户 vs AI 肥鱼娘），人格记忆采用「旧事实优先」策略。
- **总结**：对话 / 会话总结生成。
- **插件**：插件市场架构，包管理见 `bridge/pkg_manager.py`，配置见 `plugins/`。
- **配置**：运行时配置覆盖层 `app_settings.json`，以 `setattr` 注入 `config` 模块（App 模式默认关闭外部功能）。
- **外观**：内置 8 套主题，配置存于 `data/appearance.json`；背景图原始字节经 `server.py` 的 `_serve_raw` 提供；窗口标题栏可由 `js_api.set_title` 实时修改。

## MC 功能（模组世界）

模组版 MC 依赖自建的 **FeiyuAPI 模组**（NeoForge 1.21.1，模组世界必须插件）：

- **纯净原版**：`libs/qq_bot_runtime/mc_bot/`（Mineflayer 桥 `bridge.js` + 大脑 `mc_bot_brain.py`），无 mod 的原版 LAN 世界试验。
- **模组世界**：`libs/qq_bot_runtime/mc_mod/feiyuapi/`（Gradle + `net.neoforged.moddev`；Minecraft 1.21.1 / NeoForge 21.1.248 / Java 21）。构建：
  ```powershell
  cd libs/qq_bot_runtime/mc_mod/feiyuapi
  ./gradlew build     # 产物 build/libs/feiyuapi-1.0.0.jar
  ```
  把 `feiyuapi-1.0.0.jar` 放进游戏实例的 `mods/` 目录即可（游戏须为 NeoForge 1.21.1）。
- 详见 `libs/qq_bot_runtime/mc_bot/README.md`。

## 发布与安装

- 发布产物在 `dist/`（`mc_pack.zip`、`plugins_pack.zip`、`tools_pack.zip`、`napcat_pack.zip`、以及分卷 `feiyu_core.*` / `vl_pack_7z.*` / `voice_pack_7z.*` 等），通过 **GitHub Releases** 作为附件分发（`dist/` 不在 git 内，因其单文件普遍 ≥100MB）。
- `dist/SHA256SUMS.txt` 记录各包 sha256，下载后用于校验完整性。
- 安装：下载对应 release 包与校验和，合并分卷后解压，运行其中 `start_app.bat`。

## 注意事项

- `data/`、`*.log`、`__pycache__/`、运行时、缓存、安卓工程等已被 `.gitignore` 忽略，不入库。
- 中文提交信息以 UTF-8 写入文件并经 `git commit -F` 提交，避免终端编码乱码。
- 网络受限环境下 GitHub 443 直连可能不通；可改用 SSH 协议（`git@github.com:hth768/Fat-Fish.git`）。
