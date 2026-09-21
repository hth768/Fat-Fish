# MEMORY（长期记忆）

*逐日细节见同目录 `YYYY-MM-DD.md`。只存跨会话稳定、高频复用要点。*

## 1. 项目结构与部署副本
- 仓库 `e:\feiyu_standalone`（GitHub `hth768/Fat-Fish`）= Python HTTP 后端 + pywebview/Edge 窗口 + 原生 JS WebUI，含捆绑引擎 `libs/qq_bot_runtime`，**唯一 git 提交目标**（dist/ 已 gitignore，LICENSE=MIT）。部署副本均非 git、一律不提交：`e:\qq_bot`(纯引擎，多 ~27 个游戏自动化 `mc_*`/`pc_*`/`pvz_*` 与陈旧孤立 `codebuddy_cli.py`，**同步时保留不删、切勿合回仓库**)；`E:\feiyu_app`(App 层无 libs，`FEIYU_QQ_BOT=E:\qq_bot` 复用引擎)；`D:\testing\Fat-Fish`(完整克隆，D 盘根无写权限放 testing 下)。
- **同步铁律**：仓库=唯一真相源，漂移修复一律 仓库→部署（`robocopy /E` 复制/新增、**绝不删**副本独有文件）。引擎改动须连带依赖同步 `e:\qq_bot`；App 层 `server.py`+`bridge`+`webui` 三处都要同步（曾因 `E:\feiyu_app\bridge` 旧签名→每发消息 TypeError→500）。
- **重启**：先 `Get-CimInstance Win32_Process` 看**实际**命令行——运行实例可能是 `E:\feiyu_app\app.py` 或 `D:\testing\Fat-Fish\app.py`，勿凭记忆匹配（踩过：按 Fat-Fish 匹配杀空）。杀整树 `taskkill /T /F /PID <父PID>`（连带 sidecar）；再 detached 启动：`Start-Process cmd.exe -ArgumentList '/c','set FEIYU_QQ_BOT=E:\qq_bot&& E:\qq_bot\venv\Scripts\python.exe <app.py> --with-core > data\app.log 2>&1'`（勿 `-NoNewWindow` 会阻塞）。
- **venv python.exe 进程成对正常**：凡经 `E:\qq_bot\venv\Scripts\python.exe` 启动的进程都显示 2 个同名 PID（stub 父+真身子），`--with-core` 两进程、sidecar 每个 2 PID 都是正常的；真正的端口叠加看 `netstat -ano` 同端口多条 LISTENING。sidecar（memory 8766/monitor 8770/telemetry 8771/vox_tts 8765）已改 `_ExclusiveHTTPServer`+launcher 启动前清残留（Windows SO_REUSEADDR 会静默叠加端口→请求随机路由旧进程）。
- 模型 Key 在同目录 `ai_providers.json`（覆盖层、热重载），**勿写 config.py/勿提交**；`config.AI_PROVIDERS={}`。联网 WEB_TOOLS 凭据只在 `e:\qq_bot\config.py`。捆绑 Python：`libs\qq_bot_runtime\runtime\python\python.exe`（`-m py_compile` 校验）。本机无 F: 盘，绝对路径先 `Test-Path`。
- **WebView2 缓存击穿**：静态资源必须 `Cache-Control: no-store` **加** `?v=<文件 mtime>` 查询参数（server 端 index.html 动态注入）；改 webui 必重启实例。
- 端点约定：SSE=`/api/events?session=`；`/api/appearance/icon`(+reset) POST；`/api/memory/export` GET；`/api/builder/*` 读在 do_GET、写在 do_POST；`GET /api/debug/log`。

## 2. 调试模式（2026-09-21 新增）
- 配置页开关→`settings_store` 的 `debug` 字段→`bridge.set_debug()`；采集：stdout tee(`_DebugStream` 0.25s 批刷)、`sys/threading excepthook`、`warnings.showwarning`、`quiet.set_sink`(DEGRADE→warn/ATTENTION→error)；环形缓冲 3000 条；心跳 5s 推运行状态。前端 `webui` `#debugConsole` 浮层+`#debugFab`，`GET /api/debug/log` 回灌、SSE `type:"debug"` 实时。
- **铁律：`server._sse_write` 写失败必须向上抛**——若吞异常，客户端断开后写线程永不退出且每次广播都失败；调试开启时 degrade→push_debug→广播→再失败形成**自我放大刷屏环**（每秒几十条 WinError 10053）。修复后线程首败即退+finally 注销订阅，仅留一条「SSE 事件流断开」。
- `push_debug` 防刷屏：连续同 level/text/where 且 <15s 折叠为 ×N 计数（buf 记 `count`，SSE 带 `repeat`，前端 `appendDebug` 更新 `.dcount` 徽标）；debug 事件 `push(record=False)` 不进通用回放 `_log`，防挤掉 recent(40) 里的真实事件。

## 3. 自编程 / 构建助手
- `libs/qq_bot_runtime/self_coding.py` Issue 机制（`/提issue` `/提需求` `/同意issue` `/拒绝issue`）；`ai_provider.UnifiedLLM.chat` 失败→`report_ai_error`(kind=fix 含 traceback；同键 10 分钟冷却；`world!="self_coding"` 防递归；永不抛)。开关 `BOT_SELF_CODING_ENABLED` 默认 False，命令 `/开启自我编程`。
- 构建成功自动 `pm.bridge.push(None,{"type":"plugins_updated"})` 刷插件页；`chat_service._build_self_capability_hint()` 调模型前注入已启用插件清单，防 AI 说「做不到了」。
- `bridge/builder_api.py`：工作区 `_WORKSPACE_ROOTS`+`_WORKSPACE_DENY`，统一入口 `workspace_root()` 勿直接用 APP_DIR；权限 5 档、闸门 `_gate_operation` 仅管写工具；批准队列 `data/builder_approvals.json`（真执行只在 approve_sync）；`run_chat()` 多轮 tool-calling ≤14 步；生成类只写 drafts。**引擎 `ai_provider.py` 改动必须同步 `e:\qq_bot\ai_provider.py`**（`chat()` 有 `keep_reasoning`/`stream`/`on_delta`，否则 TypeError）。流式=`POST /api/builder/chat/stream` 手写 chunked 帧，勿复用 `_serve_sse`。前端状态 `bchat`、`_bc*` 前缀；改 UI 后 `node --check webui/app.js`。
- **构建助手工作区坑（2026-09-21 实测+已修）**：工作区根=APP_DIR；App 层（如 `E:\feiyu_app`）**没有 `libs/qq_bot_runtime` 引擎源码**。`FEIYU_QQ_BOT` 实际指向**引擎运行时根目录本身**（顶层即含 `plugin_base.py`/`quiet.py`，并非其下的 `qq_bot_runtime` 子目录——这点踩过坑）。`builder_api.py:140 CONTEXT_ALLOW_DIRS` 含 `libs/qq_bot_runtime` 但 App 下不存在被静默跳过→模型找不到 `plugin_base.FeaturePlugin` 接口依据易迷失空耗步骤。
  - 修复 F3：`builder_api._engine_runtime_dir()`（读 `FEIYU_QQ_BOT`，兼容「env 即运行时根」与「env 下 qq_bot_runtime」两种布局）→ `list_context_files` 把引擎运行时（仅 `plugin_base.py`/`pkg_manager.py`/各 `*_base.py`/`PLUGIN_PROTOCOL.md`/`PLUGINS.md`/`manifest.py` + `*.md`）以 `libs/qq_bot_runtime/` 前缀并入只读上下文；`_is_allowed_context_path` 放行引擎目录只读、`read_context_file` 对 `libs/qq_bot_runtime/` 前缀重定向到外部引擎。**引擎运行时为只读上下文，写入仍走 `_WORKSPACE_ROOTS`/黑名单，不会误写 `E:\qq_bot`**。已验证：上下文含 `libs/qq_bot_runtime/plugin_base.py` 且可读。
  - 修复 F2：插件构建 `run_chat` 单轮 `max_steps` 14→24（写 manifest+plugin.py+语法校验留余量）。
  - 修复 F1：`self_coding._try_load`(libs/qq_bot_runtime/self_coding.py) 装载失败时附定向诊断（缺 plugin.py / 无 `create_plugin(bridge,cfg)` 入口）；`name=None` 时 `_scan_plugin_dirs_since` 兜底扫描残留目录并给出精准 `retry_hint`，让「修复」环知道该补 plugin.py（原先只笼统「未能从产物识别包名」）。

## 4. 插件协议 / 记忆子系统 / 外观
- UI 事件通道 `core.app_bridge`：`getattr(core,"app_bridge",None)`（纯引擎 None 静默跳过），`session=None`=全局广播，线程安全，禁 sys.modules hack。manifest v2 必填 name(=目录名)/title/version/kind；`scan_packages()` 跳 `_` 开头；范本 `plugins/greeting_demo/`。游戏大脑（McBotBrain/PcBrain/PvzBrain）在 `E:\feiyu_app\plugins/brain_*`。
- `chat_service._chat_pipeline` 是「多分支预处理+汇合后处理」结构：**分支内定义、汇合处用的变量必须提升到分支链之前初始化**（曾图片消息 UnboundLocalError，详见 DEBUG.md §7）。记忆四件套 `long_term_memory`/`persona_memory`/`reflection_memory`/`important_notes`；防串台：user 消息带 `[称呼]` 前缀、`build_memory_messages` 有 `if user_id` 守卫；档案/反思须区分主体「用户」vs「AI」。
- 外观 `bridge/appearance_api.py`：8 主题存 `data/appearance.json`，背景图走 `server.py._serve_raw`；侧边栏 `#sidebarToggle`+`body.sidebar-collapsed`。

## 5. 安卓版 / 浏览器验证 / 工程约定
- feiyu-android 整目录 gitignore **勿 git add**，分发=GitHub Release(tag `android-v*` 附件 apk)，已 v1.0.4。构建全在 E 盘（JDK17 `E:\jdk17`、Gradle 8.9、SDK `E:\AndroidSDK`、`GRADLE_USER_HOME=E:\gradle-home`），加 `--max-workers=2 -Xmx1024m` 防 OOM。Release：token 文件喂 stdin→`POST releases`→`POST uploads/.../assets`。
- msedge headless CDP `--remote-debugging-port=9222 --user-data-dir=E:\edge-cdp-profile` + `agent-browser connect 9222`：一行一命令直调（勿 PowerShell+splat 包装会退化打 help），中文经 CLI 被 GBK 破坏→一律 CSS 选择器；收尾杀进程+删目录。
- 日志噪音：高频函数禁无条件 print（启动日志目标 ~40 行）；`quiet.py` degrade（默认静默计数，`FEIYU_TRACE_DEGRADE=1` 才打，同站 5s 合并）/attention（始终打，3s 节流）；生产裸 `except:`=0（`tests/audit_silent_except.py` AST 扫描）。**坑：`app.py` 不能模块级 import quiet**（路径 bootstrap 后才就绪）。
- 测试两层：引擎 `libs/qq_bot_runtime/tests/` + App `tests/`（`python -m unittest discover -s tests -v`，改 bridge 后必跑，59 个）；新增测试用 `_IsolatedDataMixin`。
- git 提交中文编码坑：PowerShell GBK 下 heredoc/python -c 写中文会乱码，**用 write_to_file 写 `.git/CMSG.txt` 再 `git commit -F .git/CMSG.txt`**；校验用 `subprocess.check_output(['git','log','-1','--format=%B']).decode('utf-8')`。
