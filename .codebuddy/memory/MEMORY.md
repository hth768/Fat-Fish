# MEMORY（长期记忆）

*逐日细节见同目录 `YYYY-MM-DD.md`。本文件只存跨会话稳定、高频复用的要点。*

## 1. 项目结构与部署副本
- 仓库 `e:\feiyu_standalone`（GitHub `hth768/Fat-Fish`）= Python HTTP 后端 + pywebview/Edge 窗口 + 原生 JS WebUI。含捆绑引擎 `libs/qq_bot_runtime`，**唯一 git 提交目标**。
- 部署副本（均非 git，一律不提交）：
  - `e:\qq_bot`：纯引擎（无 App 层；比仓库多 ~27 个游戏自动化 `mc_*`/`pc_*`/`pvz_*` + 陈旧孤立 `codebuddy_cli.py`，同步时须保留不删）。
  - `E:\feiyu_app`：App 层（无 libs），靠 `FEIYU_QQ_BOT=E:\qq_bot` 复用引擎。
  - `D:\testing\Fat-Fish`：完整克隆（D 盘根无写权限，放 testing 子目录），**用户实际运行**：`"E:\qq_bot\venv\Scripts\python.exe" D:\testing\Fat-Fish\app.py --with-core`，日志 `data\app.log`，读 `E:/feiyu_standalone/data/app_settings.json` 覆盖层。`--with-core` 同名两 python 进程 = 正常父子。
- **同步方向铁律**：仓库是唯一真相源，漂移修复一律 **仓库→部署**（复制/新增、**绝不删除**部署副本独有文件）。部署残留的 `codebuddy_cli.py`/`CODEBUDDY_*` 是已被 `self_coding.py`/构建助手取代的旧方案，**切勿合回仓库**。
- **三处副本都要同步**：引擎层→`e:\qq_bot`（须连带依赖模块，否则"启动能、来消息崩"）；App 层→`E:\feiyu_app`（改 webui 必重启=WebView2 缓存）；全量→`D:\testing\Fat-Fish`。先 `Get-FileHash` 比对落后文件。
- **重启**：`Get-CimInstance Win32_Process` 找命令行含 `Fat-Fish\app.py` 的 PID → `Stop-Process -Force`；再 `Start-Process cmd.exe -ArgumentList '/c','set FEIYU_QQ_BOT=E:\qq_bot&& E:\qq_bot\venv\Scripts\python.exe D:\testing\Fat-Fish\app.py --with-core > D:\testing\Fat-Fish\data\app.log 2>&1'`（detached，**勿 `-NoNewWindow`** 会阻塞）。
- 模型 Key：真实值在同目录 `ai_providers.json`（覆盖层、热重载），**勿写 config.py/勿提交**；`config.AI_PROVIDERS={}`/`AI_CAPABILITY_ROUTING` 置空。`GLM_API_KEY` 等顶层变量（语音用）保留。
- 端点约定：`/api/appearance/icon`(+reset) POST；`/api/memory/export` GET；`/api/builder/*` 只读在 do_GET、写只在 do_POST。
- 捆绑 Python：`libs\qq_bot_runtime\runtime\python\python.exe`（`-m py_compile` 校验）。本机无 F: 盘，绝对路径先 `Test-Path`。

## 2. 自编程（BOT Self Coding）
- 当前架构：`libs/qq_bot_runtime/self_coding.py`（Issue 机制 `/同意issue` `/拒绝issue`，复用 `bridge/builder_api`）+ `config.BOT_SELF_CODING_*` 开关。旧 `codebuddy_cli.py`（外部 CLI）已弃用。
- Issue 两类：①被动报障——`ai_provider.UnifiedLLM.chat` 失败时调 `report_ai_error` 提 `kind="fix"`（含 traceback）；②用户显式 `/提issue` `/提需求` 提 `kind="feature"`。
- `report_ai_error` 闸门：①`BOT_SELF_CODING_ENABLED=True`（默认 False，需 `/开启自我编程`）；②`world!="self_coding"`（防递归）；③同 `(world,异常类型,上下文前40字)` 10 分钟冷却（`_REPORT_COOLDOWN=600s`）；④永不抛异常。
- 开关默认：`BOT_SELF_CODING_ENABLED=False`、PERM="full"、`ISSUE_AUTO=True`（自动派构建助手）、`AUTO_LOAD=True`（自动装载+启动）。命令 `/开启自我编程` `/关闭自我编程` `/权限 <档>`。
- **构建后自动上架插件页**：`_try_load` 在 `pm.load(name)` 成功后 `pm.bridge.push(None,{"type":"plugins_updated","name":name})` 广播 UI 刷新 + 推送「✅ 已自动装载并启动」；前端 `app.js` `handleEvent` 收 `plugins_updated`→`loadPlugins()`+`refreshCorePill()`。
- **能力提示防"做不到了"**：`chat_service._build_self_capability_hint()` 在 `_chat_pipeline` 调模型前注入 system——列出 `plugins_api.manager().list_all()` **已启用**插件，且 `BOT_SELF_CODING_ENABLED` 真时附「自我编程能力」说明。无已启用插件且自编程关时返回空串不注入（零开销）。
- 游戏大脑（McBotBrain/PcBrain/PvzBrain）在 `E:\feiyu_app\plugins/brain_*` 插件包，`plugin_registry` 仅内置 `chat`。
- 端点/群白名单见下；QQ 群白名单 `config.QQ_GROUP_WHITELIST=[]`（空=所有群；`e:\qq_bot\config.py` 填群号）。

## 3. 记忆子系统 / 防串台
- `long_term_memory`(用户档案,新覆盖旧) / `persona_memory`(AI 人格,旧优先) / `reflection_memory`(交互规则,按 user_id,空=全局) / `important_notes`(一次性)，均由 `chat_service` 调用。
- 避坑：档案/反思须区分主体"用户"vs"AI"，否则 AI 特征写进用户档案。
- 防串台：每条发 AI 的 user 消息带 `[称呼]` 前缀（`_chat_pipeline` 构建新列表不改持久历史）；`build_memory_messages` 注入有 `if user_id` 守卫，空则整段跳过（靠消息标签兜底）。

## 4. 外观 / 侧边栏（提交 7c68046+）
- `bridge/appearance_api.py`：8 套主题、存 `data/appearance.json`、背景图经 `server.py._serve_raw`。**静态资源必须 `Cache-Control: no-store`** 否则 WebView2 旧缓存。侧边栏 `#sidebarToggle`→`body.sidebar-collapsed`+`margin-left:-216px`，状态 `localStorage`。

## 5. 安卓版（feiyu-android，不进仓库只发 Release）
- 整目录 gitignore，**勿 git add**；分发=GitHub Release（tag `android-v1.0.x` 附件 apk），已 v1.0.4（含语音播报）。
- 构建全在 E 盘：JDK17 `E:\jdk17`、Gradle 8.9 `E:\gradle-dist\gradle-8.9`、`GRADLE_USER_HOME=E:\gradle-home`、SDK `E:\AndroidSDK`，加 `--max-workers=2 -Xmx1024m` 防 OOM。
- Release：token **文件喂 stdin**（管道被吞）→ `POST releases`(`--data-binary @file`)→ `POST uploads.../assets`。

## 6. 构建助手（bridge/builder_api.py）——改它必读
- 能力：上下文/构建历史/生成·改进·保存 agent+plugin/工作区源码读写 diff。写入前自动备份 `data/builder_bak/<rel>.<ts>`（留 20 份）。
- 工作区安全：`_WORKSPACE_ROOTS=(plugins,libs,bridge,webui,agents,config)`+`_WORKSPACE_DENY`(.codebuddy/.git/.env/__pycache__/settings_store.py/ai_providers.json/data/user_profiles.json)；`_resolve_rooted` realpath 越界校验；`_is_text_file` 拒二进制；单文件 512KB。**统一入口 `workspace_root()`**（自定义看 `is_custom_workspace()`），**勿直接用 APP_DIR**。
- 权限 5 档 plan/default/acceptEdits/full/bypassPermissions；闸门 `_gate_operation` 在 `_exec_tool` 开头仅管 WRITE_TOOLS；`classify_operation` 双字段 level+kind 都要维护，中文标签 `_MODE_LABEL` 但下拉 value 用英文 id。
- 批准 `_queue_approval`→`data/builder_approvals.json` 返回 `{"pending":True,"approval_id"}`；真执行只在 `approve_approval_sync`/`approve_all_sync`。规则 `data/builder_rules.json`，`RULE_SCOPES=(file,dir,all)`。
- 主循环 `run_chat()`：多轮 tool-calling（上限 max_steps 默认 14）；生成类只写 drafts，点保存才落盘；`_reasoning` 本地元数据进行前 `_strip_meta()` 剔除；写回历史 `base_idx=len(convo)-1`（off-by-one 坑）。`capability="tools"` 无路由回退 `"chat"`。
- 引擎 `ai_provider.py` 改动**必须同步 `e:\qq_bot\ai_provider.py`**（`chat()` 有 `keep_reasoning`/`stream`/`on_delta`，否则 `TypeError`）。流式端点 `POST /api/builder/chat/stream`（`server.py` 手写 chunked 帧，勿复用 `_serve_sse`）。联网 `WEB_TOOLS` 双后端 DeepSeek→GLM 回退，凭据只在 `e:\qq_bot\config.py`。
- 前端 `webui/app.js`（状态 `bchat`，前缀 `_bc*`），改 UI 后 `node --check webui/app.js`。测试红线：写目标须不存在临时文件；测完恢复 mode=default/workspace=""。

## 7. 插件协议（提交 84527b3）
- 官方 UI 事件通道 `core.app_bridge`：`getattr(self.core,"app_bridge",None)`（纯引擎 None 静默跳过）→ `bridge.push(session,{...})`，`session=None`=全局广播，线程安全。禁止 sys.modules hack。
- manifest(`MANIFEST_SCHEMA_VERSION=2`)：必填 name(=目录名)/title/version/kind(platform/feature/brain/sidecar/local)；`scan_packages()` 跳过 `_` 开头；装载前静态校验，失败以 `kind:invalid`+`manifest_errors` 列出（不静默消失）。`purge_wrapper_modules(name)` 三处清理 sys.modules。sidecar 日志 `<qq_bot>/logs/sidecar_<name>.log`。范本 `plugins/greeting_demo/`（自包含、官方通道、config_schema+on_config 热生效）；`plugins/` 进版本控制。

## 8. 浏览器自动化 / 视觉验证
- msedge headless `--remote-debugging-port=9222 --user-data-dir=E:\edge-cdp-profile`；`agent-browser connect 9222`→`open http://127.0.0.1:8900`→screenshot/click/eval。收尾杀进程+删目录。
- 铁律：勿用 PowerShell+splat 包装 agent-browser（退化打 help），一行一命令直调；CLI 传中文被 GBK 破坏→一律 CSS 选择器。

## 9. 日志噪音 / 工程约定
- 高频函数禁无条件 print（`scan_packages`/`read_manifest`/状态轮询改去重打印，实例级事件改模块级标志），启动日志目标 ~40 行。统计用 `sys.stdout.reconfigure(encoding="utf-8",errors="replace")`。
- 静默异常治理：`libs/qq_bot_runtime/quiet.py`(degrade/attention)+`tests/audit_silent_except.py`(AST 扫描→DEGRADE_AUDIT.md)；生产裸 `except:`=0。**坑：`app.py` 不能模块级 import quiet**（路径 bootstrap 后才就绪）。
- 两层测试：引擎 `libs/qq_bot_runtime/tests/` + App `tests/`(纯标准库 `python -m unittest discover -s tests -v`，改 bridge 后跑)。新增测试用 `_IsolatedDataMixin`(落 tempfile)。工作副本 ~30GB 但 git 跟踪 ~1520 文件/60MB（dist/ 已 gitignore）。**LICENSE=MIT**。

## 10. git 提交规范（中文编码坑）
- 本环境 PowerShell 为 GBK，`python -c "...中文..."` 或 heredoc 写的信息文件会被**双重编码成乱码**（`git config i18n.commitEncoding` 默认 utf-8 仍中招）。**正确做法：用 `write_to_file` 工具写 `.git/CMSG.txt`（可靠 UTF-8，字节 `e6 9e 84`=构），再 `git -c i18n.commitEncoding=utf-8 commit -F .git/CMSG.txt`，仅修正刚推送的 tip 用 `git push --force-with-lease origin main`**。`git log` 直看会误判（GBK 控制台），应以 `subprocess.check_output(['git','log','-1','--format=%B']).decode('utf-8')` 校验。
