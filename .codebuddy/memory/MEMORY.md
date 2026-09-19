# MEMORY（长期记忆）

*逐日细节见同目录 `YYYY-MM-DD.md`。本文件只存跨会话稳定、高频复用的要点。*

## 1. 项目结构与部署副本
- 仓库 `e:\feiyu_standalone`（GitHub `hth768/Fat-Fish`）= Python HTTP 后端 + pywebview/Edge 窗口 + 原生 JS WebUI（`webui/`）。
  - `e:\feiyu_standalone`：**唯一 git 提交目标**（含捆绑引擎 `libs/qq_bot_runtime`）。
  - `e:\qq_bot`：独立引擎版，只有引擎无 App 层（无 bridge/server/app/plugins），自带 webui 是引擎控制台。非 git。
  - `E:\feiyu_app`：第二份 App 部署（有 App 层无 libs），复用 `e:\qq_bot` 引擎，靠 `FEIYU_QQ_BOT=E:\qq_bot`。非 git，**部署副本一律不提交**。
- **重启部署**：`Get-CimInstance Win32_Process` 找命令行含 `feiyu_app\app.py` 的 PID → `Stop-Process -Force`；再 `Start-Process cmd.exe -ArgumentList '/c','set FEIYU_QQ_BOT=E:\qq_bot&& E:\qq_bot\venv\Scripts\python.exe E:\feiyu_app\app.py --with-core > E:\feiyu_app\data\<log> 2>&1'`（detached，**勿 `-NoNewWindow`** 会阻塞）。会短暂关窗口后自动重开。
- **同步规则**：App 层→`E:/feiyu_app`：`bridge/* webui/* server.py app.py settings_store.py plugins/groups.json README.md OVERVIEW.md PLUGINS.md tests/*`（先 Get-FileHash 比对落后文件；改 webui 必须重启=WebView2 缓存）。引擎层→`e:\qq_bot`：**必须连带复制全部依赖模块**（逐核对顶层 import + Test-Path），否则"启动能、来消息崩"。
- 模型注册表：`config.py` 的 `AI_PROVIDERS={}`/`AI_CAPABILITY_ROUTING` 置空；真实 Key 在同目录 `ai_providers.json`（覆盖层，热重载）。**Key 勿写 config.py/勿提交**；`GLM_API_KEY` 等顶层变量（语音用）保留。
- 端点约定：`/api/appearance/icon`(+reset) 是 POST；`/api/memory/export` 是 GET；`/api/builder/*` 只读查询在 do_GET(query 传参)，do_POST 只放写。
- 捆绑 Python：`libs\qq_bot_runtime\runtime\python\python.exe`（`-m py_compile` 校验）。盘符漂移排错：本机无 F: 盘，绝对路径先 Test-Path 再 where。

## 2. bot 记忆子系统（qq_bot_runtime）
- `long_term_memory.py` 用户档案（新覆盖旧）；`persona_memory.py` AI 人格（旧优先）；`reflection_memory.py` 交互规则（按 user_id；空=全局共享）；`important_notes.py` 一次性信息。均由 `chat_service.py` 调用。
- 避坑：档案/反思须区分主体是"用户"还是"AI"，否则模型把 AI 特征写进用户档案。
- **防串台**：每条发往 AI 的消息带 `[当前用户称呼]` 前缀（`chat_service._chat_pipeline` 给每条 user 消息加，构建新列表不改持久历史）。`build_memory_messages` 的 profile/notes/persona 注入有 `if user_id` 守卫，空 user_id 整段跳过（靠消息打标签兜底）。
- **QQ 群白名单**（提交 c9404de）：`config.QQ_GROUP_WHITELIST=[]`（空=关闭=所有群；非空=仅列表内群号）。`qq_plugin.py` 群分发入口不在白名单则 continue。`e:\qq_bot\config.py` 填群号生效。

## 3. 外观自定义 / 侧边栏（提交 7c68046 + 后续）
- `bridge/appearance_api.py`：THEMES 8 套、存 `data/appearance.json`、背景图经 `server.py._serve_raw`。**静态资源必须 `Cache-Control: no-store`** 否则 WebView2 旧缓存。
- 侧边栏折叠：`#sidebarToggle` → `body.sidebar-collapsed` + CSS `margin-left:-216px`；状态 `localStorage`。

## 4. 安卓版（feiyu-android）：不进仓库，只发 Release
- `feiyu-android/` 整目录 gitignore，**勿 git add**；分发=GitHub Release（tag `android-v1.0.x`，附件 apk）。已发至 v1.0.4（含语音播报）。
- 构建环境全在 E 盘：**JDK17** `E:\jdk17\...`（Android Studio 自带 JBR Java25 不兼容）、Gradle 8.9 `E:\gradle-dist\gradle-8.9`、`GRADLE_USER_HOME=E:\gradle-home`、SDK `E:\AndroidSDK`。构建加 `--max-workers=2 -Xmx1024m` 防 OOM。
- Release：token 须**文件喂 stdin**（管道被吞）→ `POST releases`(body 用 JSON 文件 `--data-binary @file`)→ `POST uploads.../assets`。

## 5. 构建助手（bridge/builder_api.py）——改它必读
- 能力：上下文文件读写、构建历史、生成/改进/保存 agent+plugin、工作区源码读写 diff、plugin/files。每次写入前自动备份 `data/builder_bak/<rel>.<ts>`（留 20 份）。
- 工作区安全：`_WORKSPACE_ROOTS=(plugins,libs,bridge,webui,agents,config)` + `_WORKSPACE_DENY`(.codebuddy/.git/.env/__pycache__/settings_store.py/ai_providers.json/data/user_profiles.json)；`_resolve_rooted` realpath 越界校验；`_is_text_file` 拒二进制；单文件 512KB。**统一入口 `workspace_root()`**（自定义工作区看 `is_custom_workspace()`），所有文件 API 基于它，**勿直接用 APP_DIR**。路径前缀匹配（`rel.startswith("libs/")`）。
- 设置 `data/builder_settings.json`：workspace/permission_mode/confirm_*/auto_backup/remember_approvals/max_steps/deny_extra/web_*。权限 5 档 plan/default/acceptEdits/full/bypassPermissions；闸门 `_gate_operation` 在 `_exec_tool` 开头，仅 WRITE_TOOLS 受管；`classify_operation` 双字段 level(low/high)+kind，两字段都要维护。中文标签 `_MODE_LABEL`，下拉 value 用英文 id。
- 批准：`_queue_approval`→`data/builder_approvals.json`，返回 `{"pending":True,"approval_id","message":"不要重复提交"}`；真执行只在 `approve_approval_sync`/`approve_all_sync`。结果以 `[系统通知]` user 消息写回。规则 `data/builder_rules.json`，`RULE_SCOPES=(file,dir,all)`。
- `capability="tools"` 无路由→`_chat_with_tools()` 先试 tools，异常含 tools/无可用供应商/不支持能力 时回退 `capability="chat"`。
- 主循环 `run_chat()`：多轮 tool-calling（上限 max_steps 默认 14）；生成类只写 drafts，点保存/模型调 save_* 才落盘。流式 `run_chat(stream=True,emit=cb)`。`_reasoning` 是本地元数据键，回传 API 前 `_strip_meta()` 剔除（新增下划线键同步它）。写回历史 `base_idx=len(convo)-1`（off-by-one 坑）。
- 引擎 `ai_provider.py` 改动**必须同步 `e:\qq_bot\ai_provider.py`**（否则 `TypeError: chat() got unexpected kw`），`chat()` 有 `keep_reasoning`/`stream`/`on_delta`。
- 流式端点 `POST /api/builder/chat/stream`：`server.py` 手写 chunked 帧（`b"%x\r\n"+payload+b"\r\n"`，结束 `b"0\r\n\r\n"`），勿复用 do_GET 的 `_serve_sse`。
- 联网工具 `WEB_TOOLS`：搜索双后端 DeepSeek Responses API 主 → GLM 回退；凭据只在 `e:\qq_bot\config.py`，**本地测联网须把 `e:\qq_bot` 插 sys.path 前**。
- 前端 `webui/app.js`（状态 `bchat`，前缀 `_bc*`）：三栏布局、`bcModel/bcThink/bcPerm/bcWs` 等控件。改 UI 后 `node --check webui/app.js`。
- 测试红线：写操作目标必须是不存在的临时文件；直接 POST workspace/write 立即覆盖磁盘；测完恢复 mode=default/workspace=""。

## 6. 插件协议与加固（提交 84527b3）
- 官方 UI 事件通道 `core.app_bridge`：`core_bridge.build_engine()` 设 `core.app_bridge=self`；插件用 `getattr(self.core,"app_bridge",None)`（纯引擎 None 静默跳过）→ `bridge.push(session,{...})`，`session=None`=全局广播，线程安全。禁止 sys.modules hack。
- manifest(`MANIFEST_SCHEMA_VERSION=2`)：必填 name(=目录名)/title/version/kind(platform/feature/brain/sidecar/local)；`scan_packages()` 跳过 `_` 开头目录。装载前静态校验，不通过以 kind="invalid"+manifest_errors 出现（不静默消失）。`purge_wrapper_modules(name)` 三处清理 sys.modules。sidecar 日志 `<qq_bot>/logs/sidecar_<name>.log`。
- 示例包 `plugins/greeting_demo/`（自包含、官方通道、config_schema+on_config 热生效）；`plugins/` 进版本控制。

## 7. 浏览器自动化 / 视觉验证
- 起 msedge headless `--remote-debugging-port=9222 --user-data-dir=E:\edge-cdp-profile`；`agent-browser connect 9222`→`open http://127.0.0.1:8900`→screenshot/click/eval 断言。收尾杀 edge-cdp-profile 进程+删目录。
- **铁律**：勿用 PowerShell 函数+splat 包装 agent-browser（退化打印 help），一行一命令直接调；CLI 传中文被 GBK 破坏→一律 CSS 选择器。

## 8. 日志噪音约定（cddf082）：高频函数禁止无条件 print
- `scan_packages()`/`read_manifest()`/状态轮询改用去重打印（`_log_manifest_issues` 按 mtime+签名；schema 缺失跨包聚合 `_flush_schema_notice`）。实例级事件改模块级标志（如 `_BUVID_NOTICE_DONE`）。启动日志目标 ~40 行。统计用 `sys.stdout.reconfigure(encoding="utf-8",errors="replace")`。

## 9. git 提交规范 / 工程
- PowerShell 中文 commit 乱码 → **UTF-8 message 文件 + `git commit -F <file>`**（勿 `-m`，括号会被解析错）。验证 `chcp 65001; git --no-pager log -1 --format=%B`。
- 静默异常治理（已完成全量）：`libs/qq_bot_runtime/quiet.py`(degrade/attention)+`tests/audit_silent_except.py`(AST 扫描分桶 A/B/C→DEGRADE_AUDIT.md)；生产代码静默 except 已清零。**坑：`app.py` 不能模块级 import quiet**（路径 bootstrap 后才就绪）。
- 两层测试：引擎 `libs/qq_bot_runtime/tests/` + App `tests/`(57 项，`python -m unittest discover -s tests -v`，纯标准库)。改 bridge 后跑后者。新增测试用 `_IsolatedDataMixin`(落 tempfile 不污染 data/)。
- 真正裸 `except:`=0；`except Exception:`=692(bridge 159/engine 500)。工作副本 ~30GB 但 git 只跟踪 ~1520 文件/60MB（dist/ 已 gitignore）。**LICENSE=MIT**。
