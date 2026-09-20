# 历史 Bug 与调试记录（DEBUG）

> 本文件从 README 中拆分出来，专门记录**历史 Bug 的排查过程、根因与修复**，便于回归时对照。
> 不涉及功能设计说明（见 [README.md](./README.md) / [OVERVIEW.md](./OVERVIEW.md)）。
> 提交哈希基于 `hth768/Fat-Fish`；CI 运行平台为 GitHub Actions（Ubuntu + Python 3.12）。

---

## 1. CI 平台耦合断言失败（test_quiet_and_audit）

- **现象**：GitHub Actions `run #1` ~ `run #12` 全红，卡在 `Run App unit tests`（exit code 1）。本机 Windows + Python 3.14 全过，云端 Ubuntu + Python 3.12 失败。
- **排查**：本机无法直接打开 GitHub 页面（网络受限 HTTP=000），改用 GitHub API 逐级定位：
  - `actions/runs` → `actions/runs/<id>/jobs` → 找到失败步骤在单测阶段；
  - 在 `ci.yml` 增加「Post failure log as commit comment」步骤（`actions/github-script` 调 `github.rest.repos.createCommitComment`，需 `permissions: contents: write`），把真实 traceback 回贴到提交评论，便于国内访问不便时查看。
- **根因**：`tests/test_quiet_and_audit.py` 中 `test_convert_produces_valid_python_and_keeps_indent` 断言写死字面量 `degrade("m.py:`。该测试验证 `audit_silent_except.convert()` 把调用位置标准化为「路径:行号 限定名」。`convert()` 在 Linux 上把调用位置标准化为 `/tmp/tmpXXX/m.py:`（带路径前缀），而 Windows 恰好为 `m.py:` —— 故本地通过、云端失败，是**平台耦合断言**。
- **修复**：改为路径无关断言（只校验转义后的双引号、行号、限定名、上下文），提交 `639fd52`：
  ```python
  # 修复前（平台耦合）
  self.assertIn('            degrade("m.py:', out)       # 体缩进 +4
  # 修复后（路径无关）
  self.assertIn('            degrade("', out)            # 体缩进 +4 且使用双引号
  self.assertIn(":7", out)                               # 调用处行号
  self.assertIn("C.m", out)                              # 限定名
  self.assertIn("as e", out)
  ```
- **结果**：`run #13`（`639fd52`）success，CI 转绿。附带改进：失败时自动发 commit comment。

---

## 2. 引擎测试 mock 签名不匹配（test_routing）

- **现象**：完整回归时引擎层 `python tests/run.py` 报 1 个 ERROR：
  `test_role_routing_overrides_model_and_capability` → `fake_call_once() got an unexpected keyword argument 'keep_reasoning'`。
- **排查**：`git diff HEAD --stat` 为空，确认工作树与 `0abd3ed` 一致（改动均早于 BOT Self Coding 工作）。定位到 `ai_provider.py` 的 `chat()` 在「构建助手思维链」特性里向 `_call_once` 透传 `keep_reasoning`（及 `stream`/`on_delta`）关键字参数，但 `test_routing.py` 的 `fake_call_once` mock 仍是旧的位置参数签名。
- **根因**：既有 bug —— `ai_provider.py` 新增 `keep_reasoning` 关键字参数（提交 `a855966`「构建助手展示思维链」）时，未同步更新 `test_routing.py` 的测试 mock 签名，导致 `TypeError`。与本次 BOT Self Coding 改动无关（本次只动 `self_coding.py` / `chat_service.py` / `config.py` / `qq_plugin.py` / `core_bridge.py` / `app.py`）。
- **修复**：给测试 mock 加 `**kwargs`（向前兼容），提交 `5731863`：
  ```python
  # 修复前
  async def fake_call_once(self2, name, prov, messages, capability, model,
                           think, tools, images, timeout, role=None):
  # 修复后
  async def fake_call_once(self2, name, prov, messages, capability, model,
                           think, tools, images, timeout, role=None, **kwargs):
  ```
- **结果**：引擎层 90 项全过，App 层 59 项全过，无其余回归。

---

## 3. BOT Self Coding：旧 CodeBuddy CLI 自我编程移除

- **背景**：原本「自我编程」依赖外部 `codebuddy_cli.py`（CodeBuddy CLI 进程），由 `CODEBUDDY_*` 配置块与受保护核心文件清单驱动。
- **变更**：演进为 **BOT Self Coding** —— 用户开启 `BOT_SELF_CODING_ENABLED` 后，智能体直接复用内置构建助手（`bridge/builder_api.py`）构建/改进自己，并把受阻/新需求作为 Issue 提给用户（默认自动执行、产物默认自动装载）。旧的 `codebuddy_cli.py` 及 `CODEBUDDY_*` 配置块已**彻底删除**（提交 `0abd3ed`）。
- **相关回归**：因旧 CLI 移除，`qq_plugin.py` 中旧的 `[代码修改确认] ... /同意修改` 文案改为指向 `/同意issue`（`/拒绝修改` → `/拒绝issue`），避免用户收到无法识别的命令。
- **注意**：`qq_plugin.py` 顶部 `import websockets` 为可选联网依赖；本机未装该第三方包时 `import qq_plugin` 会报 `ModuleNotFoundError: No module named 'websockets'`，与本变更无关（仅一处纯字符串替换，已确认语法通过）。装上 `websockets` 即可正常导入。

---

## 4. 更早历史 Bug（来自 git 记录）

> 以下为本次拆分前已修复、散落在提交历史里的真实问题，按排查价值摘录根因与修复。
> 提交哈希均基于 `hth768/Fat-Fish`。

### 4.1 构建助手读不到自己的主文件 + 来源链接提取静默失败
- **提交**：`ab383da`（新增 App 层测试套件，测试即时抓出）
- **现象**：构建助手上下文里大量 `hf_cache/models/site-packages` 缓存 JSON，真正要读的 `builder_api.py` 反被挤掉；正文 URL 提取经常为空，模型只能"凭空引用"。
- **根因**：
  1. `CONTEXT_SKIP_DIRS` 未排除模型缓存目录，成千上万缓存 JSON 占满 `MAX_CONTEXT_FILES` 名额；且单文件 48KB 上限 < `builder_api.py`（约 126KB），被静默排除。
  2. 来源链接提取依赖 `web_tools`，该模块在纯 App 环境不可用（缺依赖）时静默返回空。
- **修复**：补充跳过目录、单文件上限提到 160KB；新增内置正则兜底（web_tools 优先，缺失时用内置实现）。实测上下文文件从 149 个噪声 → 干净 46 个，`builder_api.py` 正常列入。

### 4.2 控制台日志刷屏（manifest 告警 / buvid 提示 / pywebview 弃用）
- **提交**：`cddf082`
- **现象**：启动日志被同一批告警刷十几屏。
- **根因（三处）**：
  1. `pkg_manager.scan_packages()` 被高频调用（轮询/刷新），告警每次逐包 print → 15 包 × 每次轮询 = 刷屏；
  2. `bili_api` 每个实例各领一次 buvid 提示，多实例重复打印；
  3. `app_window.pick_folder()` 用已废弃 `webview.FOLDER_DIALOG`，每次调用打 deprecation。
- **修复**：manifest 告警按「包+manifest mtime+内容签名」去重、schema_version 跨包聚合成一行；buvid 提示改模块级标志每进程一次；pywebview 优先 `FileDialog.FOLDER`。部署版启动日志 40 行（原十几屏）。

### 4.3 防 AI 串台 / 记忆写反
- **提交**：`692cf76`（每条消息注入用户身份）、`7176037`（记忆写反与串台）、`3960c16`（多 Bot 隔离缺口）
- **现象**：AI 把 A 用户特征套到 B 用户；多 Bot 时知识库/记忆互相串台；user_id 缺失时完全失去身份锚点。
- **根因**：
  - 模型调用前未稳定注入"谁在说"，user_id 为空时上下文无身份锚点；
  - `extract_memory` 曾把 AI（肥鱼娘）特征也写进人物档案、且新旧事实冲突时无限叠加；
  - `knowledge_store` 未按 `agent_ctx` 命名空间隔离，`chat_service` 未消费 `core._model_override`。
- **修复**：
  - `chat_service` 最终调模型前给每条 user 消息加 `[说话人称呼]` 前缀（speaker_label 兜底链）；`memory_context` 加身份锚点降级；
  - `persona_memory` 加 `reconcile_persona_traits`，旧事实优先、冲突新特征 reject；
  - `knowledge_store` 按 `agent_ctx` 命名空间隔离（默认 feiyu，其余落 `agents/<id>/memory`），`chat_service` 主回复消费 `core._model_override`。
- **验证**：`3960c16` 部署验证 test_bot 知识库独立落盘、feiyu 40 条不受影响。

### 4.4 app.py 导入顺序崩溃（ModuleNotFoundError: quiet）
- **提交**：`8e2b1e4`（静默异常改造顺带修复）
- **现象**：部署实例启动即崩，`ModuleNotFoundError: quiet`。
- **根因**：`from quiet import degrade` 放在模块顶部，但 qq_bot 运行时路径要等 `bootstrap()` 才加入 `sys.path`，顶部 import 时还找不到。
- **修复**：把导入移到 `bootstrap()` 之后、首次使用之前。

### 4.5 telemetry.py 旧 bug 安全网（reporter 线程 5 秒崩、HMAC 静默失败）
- **位置**：`app.py` 约 L96（`# 安全网：若引擎升级重新带回 telemetry.py 缺 import time/hmac 的旧 bug`）
- **现象**：引擎升级后 `telemetry.py` 缺 `import time/hmac`，reporter 线程约 5 秒即崩、HMAC 签名静默失败。
- **根因**：引擎层 `telemetry.py` 历史上漏了 `time`/`hmac` 的导入，模块级用到这两个名字时抛 `AttributeError`。
- **修复（防御性）**：在 App 启动处探测 `telemetry` 模块，若缺 `time`/`hmac` 属性则直接补上（`_telemetry_mod.time = time; _telemetry_mod.hmac = hmac`），避免升级回带旧 bug 再次崩。属**安全网**而非根治——根治在引擎侧补齐导入。

### 4.6 构建助手只读接口误注册在 POST 分支导致 404
- **提交**：`2573bb7`
- **现象**：前端「参考文件」「构建历史」面板加载失败（404）。
- **根因**：`/api/builder/files`、`/api/builder/file/read`、`/api/builder/history` 三个只读接口放在 `do_POST` 分支，而前端用 GET 调用。
- **修复**：移到 `do_GET`（`file/read` 用 `?path=`、`history` 用 `?limit=`），POST 仅保留生成/改进/保存等写操作。

### 4.7 其它已修复的小问题（索引）
- `35e4f91` / `5705525`：主题切换除浅色外都保留暗夜蓝、选完主题色再进外观页跳回深夜蓝。
- `97a45df` / `eecc06e` / `d19ba13`：侧边栏按钮文字不可见、WebView2 缓存旧静态资源、重新上传背景图仍显示旧图（缓存未刷新）。
- `e43c623`：构建助手「未返回思考内容」提示文案修正。
- `7c68046`：`bridge/appearance.py` 重命名为 `appearance_api.py` 以匹配 import 约定（原名导致导入不一致）。
- `79c0958`：日志噪音修复，并合并另一会话的记忆整理。

---

## 5. 2026-09-20 会话修复（窗口不退出 / 聊天 NameError / 模型设置误报 / 自检 web 误报）

> 提交 `b4dc8d3`（仓库 + 三处部署副本同步：仓库 / `E:\qq_bot` 引擎 / `E:\feiyu_app` / `D:	esting\Fat-Fish`）。
> 用户实际运行 `D:	esting\Fat-Fish` 克隆版（`"E:\qq_bot\venv\Scripts\python.exe" app.py --with-core`）。

### 5.1 聊天 NameError: user_name is not defined
- **现象**：发任意聊天消息，前端返回「抱歉，出错了：name 'user_name' is not defined」，完全无法对话。
- **根因**：`chat_service._handle_message` 构造「说话人标识」（防串台降级）时引用了未定义的局部变量 `user_name`；正确来源是 `InboundMessage` 字段 `msg.user_name`。该异常被聊天管线的兜底 `except` 转成了用户可见错误。
- **修复**：在引用前补 `user_name = getattr(msg, "user_name", "") or ""`，后续复用 `msg.user_name` 的既有 `resolve_display_name` 链路。提交 `b4dc8d3`。

### 5.2 编辑模型时 API Key 留空误报「请填写 API Key」
- **现象**：配置页「AI 供应商（模型管理）」编辑已有命名模型，API Key 字段**留空**（不改）便保存，直接报「请填写 API Key」，无法单独改 Base URL / 模型名。
- **根因**：`provider_api.save_model` / `save_provider` 在**还原掩码密钥之前**就做 `if not api_key: 报错`，且完全没有「空值保留原 key」回退。前端对未改动的密钥回传掩码 `"****"` 或空串，于是被误判为缺值。
- **修复**：先 `load_provider_config()` 取 `existing` → 还原掩码密钥（`is_masked` 且 `existing` 有 key）→ 空值且 `existing` 有 key 则保留原值 → 最后才校验 `if not api_key` 报错。提交 `b4dc8d3`。

### 5.3 关闭窗口进程不彻底 / sidecar 孤儿堆积
- **现象**：关闭桌面窗口后 Python 主进程不退出；长期运行后系统出现数十上百个 `memory_server` / `monitor_server` / `telemetry_server` 孤儿进程（实测一次积压达 218 个进程）。
- **根因**（两处叠加）：
  1. `app_window._wait_for_close` 在 **Edge `--app` 模式**下是 `while True: time.sleep(3600)` 死循环，永不感知窗口关闭（webui / 浏览器模式本就常驻是合理的；Edge 模式是本次新增的退不干净来源）；
  2. `app.py` 的 `finally` 清理后用 `sys.exit()`，而引擎核心可能起**非守护线程**，`sys.exit` 只结束主线程、残留线程会拖住整个进程不退出。
- **修复**：
  - `app_window._open_edge_app` 保存 Edge 子进程句柄到模块级 `_edge_proc`；`_wait_for_close` 改为每 0.5s 轮询 `_edge_proc.poll()`，窗口关即 `return` 触发清理；
  - `app.py` 清理后改用 `os._exit(exit_code)` 强制终止（清理已在 `finally` 完成、sidecar 子进程已回收）。提交 `b4dc8d3`。
- **排查要点**：`Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*app.py*' -or $_.CommandLine -match 'memory_server|monitor_server|telemetry_server' }` 找主实例与孤儿，再 `Stop-Process -Force`；重启用 detached `cmd /c`（勿 `-NoNewWindow` 会阻塞）。

### 5.4 注册表自检误报「web 平台已请求启用但未注册」
- **现象**：App 启动日志 `[REGISTRY][ERR] web: 平台已请求启用但未注册进 PluginManager`，但功能正常。
- **根因**：App 模式 `core_bridge._build_core` 故意 `register_builtin_plugins(platforms=[])`（App 自身即平台、用 `server.py` 提供 WebUI，不启动引擎的 `WebPlugin`）；而 `agent_core.core.start()` 的 `validate_registry` 用 `_platforms_from_config()`，引擎 `config.ENABLE_WEB_PLUGIN=True` → 推导出 `["web"]`，把「config 要求、实际未注册」判成漂移。**纯属 App 模式误报，不影响运行**。
- **修复**：`agent_core.core.start()` 自检时若检测到 `core.app_bridge`（App 嵌入模式标记）已挂载，则给 `validate_registry` 传 `platforms=None`（复用其既有「`platforms=None` 不检查平台」守卫）；引擎独立运行（`main.py` / `web_plugin.py`，不挂 `app_bridge`）仍按 config 全量校验平台。修复后日志 `[REGISTRY] 校验通过：清单与已注册插件一致`。提交 `b4dc8d3`。

---

## 排查技巧速记

- 国内访问 GitHub 不便时，用 GitHub API 而非网页定位 CI：
  - `GET /repos/<owner>/<repo>/actions/runs?per_page=20` 看最近 runs 的 `conclusion` / `head_sha`；
  - `GET /repos/<owner>/<repo>/actions/runs/<id>/jobs` 看各 step 状态；
  - 在 `ci.yml` 加 commit comment 步骤把 traceback 回贴到提交评论。
- Windows PowerShell 下中文 commit message 用 `>` 写 `commit_msg.txt` 再 `git commit -F`；`curl` 被别名成 `Invoke-WebRequest` 时改用 `cmd /c curl ...` 或 `python -c urllib`。
- 平台耦合断言：凡断言里出现字面路径/前缀（`m.py:`、`/tmp/...`、大小写），优先改路径无关匹配。
- 关闭窗口进程不退出 → 先看是哪种窗口模式：`app_window.MODE`（`webview` / `edge-app` / `webui`）。`edge-app` 模式主进程靠轮询 Edge 子进程 `poll()` 退出；若残留，用 `Get-CimInstance Win32_Process` 按 `CommandLine` 找 `*app.py*` 主实例与 `*memory_server|monitor_server|telemetry_server*` 孤儿 sidecar，逐一 `Stop-Process -Force`。`app.py` 清理后 `os._exit` 兜底非守护线程。
