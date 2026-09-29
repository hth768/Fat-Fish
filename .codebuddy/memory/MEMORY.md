# MEMORY（长期记忆，精简版）

*逐日细节见同目录 `YYYY-MM-DD.md`。只存跨会话稳定、高频复用要点。*

## 1. 项目结构 / 部署副本 / 运维
- 仓库 `e:\feiyu_standalone`（GitHub `hth768/Fat-Fish`）= Python 后端 + pywebview/Edge + 原生 JS WebUI，含捆绑引擎 `libs/qq_bot_runtime`，**唯一 git 提交目标**（dist/ 已 gitignore）。部署副本均非 git、不提交：`e:\qq_bot`(纯引擎，多 ~27 个游戏自动化 `mc_*`/`pc_*`/`pvz_*`，**同步保留不删**)；`E:\feiyu_app`(App 层无 libs，`FEIYU_QQ_BOT=E:\qq_bot` 复用引擎)；`D:\testing\Fat-Fish`(**完整克隆、独立 git 仓库，与仓库同源同根提交 28a4617，可 git 三方合并**)。同步它**用 git 不用 robocopy**：它已把本仓库配成远端 `repo`，流程 `git fetch repo` + `git merge --ff-only repo/main`（有分叉才用普通 merge）。**判断领先/落后前必须先 `git fetch`（所有远端）**，否则远端追踪指针过期会算出假的分叉数（曾据此误判"领先 31 个提交"，实际 fetch 后 0/0）。
- **同步铁律**：仓库=唯一真相源，修复一律 仓库→部署（`robocopy /E`，绝不删副本独有文件）。引擎改动同步 `e:\qq_bot`；App 层 `server.py`+`bridge`+`webui` 三处都要同步。
- **⚠ 同步清单绝对禁止包含 `config.py`**（2026-09-29 事故：脚本把仓库纯净模板复制到 `E:\qq_bot\config.py`，覆盖了本机差异如 ffmpeg/VOXCPM 绝对路径）。部署 config 只准**追加**新配置项（用 UTF-8 的 .py 脚本读写，勿用命令行内联中文）。
- **重启**：先 `Get-CimInstance Win32_Process` 看实际命令行（实例可能是 `E:\feiyu_app\app.py` 或 `D:\testing\Fat-Fish\app.py`，勿凭记忆）。杀整树 `taskkill /T /F /PID <父PID>`；detached 启动：`Start-Process cmd.exe -ArgumentList '/c','set FEIYU_QQ_BOT=E:\qq_bot&& E:\qq_bot\venv\Scripts\python.exe <app.py> --with-core > data\app.log 2>&1'`。venv python 进程成对正常（stub+身子）。
- 模型 Key 在 `ai_providers.json`（覆盖层热重载，勿写 config.py/勿提交）。`config.AI_PROVIDERS={}`。联网凭据只在 `e:\qq_bot\config.py`。本机**无 F: 盘**。
- ffmpeg：真实位置 `E:\plugins\tools_pack\tools\ffmpeg\ffmpeg-2026-05-28-git-7b46c6a2a3-full_build\bin\ffmpeg.exe`。捆绑 Python：`libs\qq_bot_runtime\runtime\python\python.exe`。
- WebView2 缓存击穿：静态资源 `Cache-Control: no-store` + `?v=<mtime>` 注入；改 webui 必重启。
- 端点：SSE=`/api/events?session=`；`/api/appearance/icon`(+reset)；`/api/memory/export`；`/api/builder/*` 读 GET/写 POST；`GET /api/debug/log`。

## 2. 调试模式 / 日志
- 配置页开关→`bridge.set_debug()`；采集 stdout tee / excepthook / warnings / `quiet.set_sink`；环形缓冲 3000；心跳 5s。前端 `#debugConsole`+`#debugFab`。
- **铁律**：`server._sse_write` 写失败必须上抛（否则客户端断开后写线程永不退，形成 degrade→push_debug→广播失败自我放大刷屏环）。
- **坑**：degrade WARN 只进 WebUI 调试控制台（push_debug→SSE 环形缓冲），**不进 app.log**。venv 内联 `python -c` 会被 PowerShell 转义破坏→诊断脚本写成 .py 再跑。app.log 是 GBK 编码，读用 `io.open(p,encoding='gbk',errors='replace')`。

## 3. 自编程 / 构建助手
- Issue 机制 `/提issue` `/同意issue`；`report_ai_error` 同键 10min 冷却、`world!="self_coding"` 防递归。`BOT_SELF_CODING_ENABLED` 默认 False。
- `bridge/builder_api.py`：工作区 `_WORKSPACE_ROOTS`+`_WORKSPACE_DENY`，统一用 `workspace_root()`；批准队列 `data/builder_approvals.json`。**引擎 `ai_provider.py` 改动必须同步 `e:\qq_bot\ai_provider.py`**（`chat()` 签名 keep_reasoning/stream/on_delta）。流式=`POST /api/builder/chat/stream` 手写 chunked 帧，勿复用 `_serve_sse`。
- 工作区坑：`FEIYU_QQ_BOT` 指向引擎运行时根（顶层含 plugin_base.py，非其下 qq_bot_runtime 子目录）。`builder_api._engine_runtime_dir()` 把引擎只读上下文以 `libs/qq_bot_runtime/` 前缀并入；写入仍走 `_WORKSPACE_ROOTS`。

## 4. 插件协议 / 记忆 / 外观 / 群聊接话
- `core.app_bridge`：`getattr(core,"app_bridge",None)` 静默跳过；`session=None` 全局广播。manifest v2 必填 name(=目录名)/title/version/kind。游戏大脑在 `E:\feiyu_app\plugins/brain_*`。
- `chat_service._chat_pipeline`：分支内定义、汇合处用的变量必须提前初始化（曾图片消息 UnboundLocalError）。记忆四件套 `long_term_memory`/`persona_memory`/`reflection_memory`/`important_notes`；防串台 `if user_id` 守卫。
- **群聊接话三级 OR**（`ONLY_MENTION_OR_PRIVATE=True` 时）：①关键词快路径 `_is_related_topic`；②10% 随机 `GROUP_CHATTER_PROB`；③LLM 语义 `_semantic_interest`（超时/异常降级 False）。`GROUP_SEMANTIC_REPLY`/`GROUP_TOPIC_GUIDE_REPLY` 默认 True。`GROUP_CHATTER_GROUPS` 白名单（空=全群）。被动：@/`quoted_self` 必回。
- **两套主动机制**：①实时接话（`GROUP_CHATTER_GROUPS`）；②`proactive_speaker.py` 后台定时自说（`ENABLE_PROACTIVE_SPEAKER`/`PROACTIVE_GROUP_ID`/`PROACTIVE_PRIVATE_USER_ID`）。
- 运行实例 `E:\feiyu_app\app.py --with-core`；配置改动落 `E:\qq_bot\config.py`（非仓库）；主人 QQ=2190720017。`config.py` 机密勿提交。

## 5. 安卓版 / 工程约定
- feiyu-android 整目录 gitignore 勿 git add；Release 分发（tag `android-v*`，JDK17 `E:\jdk17`/Gradle 8.9/SDK `E:\AndroidSDK`/`--max-workers=2 -Xmx1024m` 防 OOM）。
- msedge headless CDP `--remote-debugging-port=9222` + `agent-browser connect 9222`：一行一命令直调，中文经 CLI 被 GBK 破坏→用 CSS 选择器。
- 测试：引擎 `libs/qq_bot_runtime/tests/` + App `tests/`（`python -m unittest discover -s tests -v`）；`tests/test_backlog_barge.py`（改 chat_service 后必跑）。
- git 中文 commit：写 `.git/CMSG.txt` 再 `git commit -F`。

## 6. 聊天会话调度器
- `ChatService.handle_message` 唯一入口→`_dispatch_message`（会话 key=`(platform,channel_type,channel_id,user_id)` 串行队列+消费者 task）。解决连发乱序。
- 积压：`BACKLOG_COALESCE_MS`(700) 合并窗口 / `BACKLOG_MERGE` 合并或逐条；`BACKLOG_IDLE_TIMEOUT`(5s) 空闲退出。
- 插嘴：`InboundMessage.barge_in`；WebUI「⚡插嘴」/命令 `/插嘴`；到达 `sess.current.cancel()`。`BARGE_IN_ENABLED` 默认 True。

## 7. 本地语音 TTS（VoxCPM2）
- 权重在 `E:\plugins\voice_pack\models\VoxCPM2`（model.safetensors 4.58G+audiovae.pth+config.json）；`vox_tts_server.py` 端口 8765。引擎根 `E:\qq_bot` 下**无** models/VoxCPM2。
- 已修：`_resolve_model_dir()` 支持绝对路径 + `spawn()` 透传 `VOXCPM_MODEL_DIR`；`FFMPEG_PATH` 改绝对路径（本机 `_TOOLS` 下无 ffmpeg）。运行配置 `VOXCPM_MODEL_DIR="E:\plugins\voice_pack\models\VoxCPM2"`、`VOXCPM_INFERENCE_TIMESTEPS=25`、`VOXCPM_VOICE_DESC` 中文萌妹描述、`VOXCPM_TIMEOUT_SECONDS=240`。
- 发语音策略：`VOICE_AUTO_CHAT=False`（不做默认语音），由 `need_voice` 语义判定（仅用户明确要求语音/想听声音/要求念某段才发；普通闲聊/长内容/代码一律打字）。`qq_plugin.reply_voice` 把 wav→mp3(`-b:a 128k`)发 QQ record，失败回退 wav。
- 资源底线：VoxCPM2 加载 ~5GB 显存+数 GB 内存；残留孤儿 sidecar 占显存→当前 sidecar 加载 `MemoryError`，杀残留即可。
- 余额静默：`BALANCE_ALERT_SILENT`(config 默认 True)=低余额只打后台日志不打扰；`ENABLE_BALANCE_MONITOR` 默认 False。
- 表情包：`emoji_store.build_emoji_hint` 从全部库 `random.sample` 抽 60 张 + 按 usage/last_used 冷门优先，`exclude` 最近 3 + `record_emoji_used` 累计；`chat_service._emoji_recent` 按会话 key 记最近用过的 3 个文件名。
- QQ 插件对齐 cortico-world-qq（移植非替换）：`QQ_WS_MODE`/`known_messages`+`QQ_SERIAL_PER_CONV`/`QQ_VISION_ON_IMAGE`+`QQ_VISION_RECALL`/`QQ_FORWARD_EXPAND`/`QQ_SEND_CONFIRM`+`QQ_SEND_CONFIRM_TTL`/引用 `get_quoted_message`+`QQ_STATE_PERSIST` 落盘 `data/qq_plugin_state.json`。
- **已对齐到新版 `D:\testing\Cortico-QQ-World\cortico-world-qq-better`(v0.1.31)**（2026-09-29）：新增模块 `festival.py`(节日/节气/农历)、`affinity.py`(好感度±100)、`reminder.py`(闹钟+`parse_when`+后台扫描)、`routine.py`(作息睡眠/午休/LLM 播报)、`qq_guard.py`(群限速 groupSpeak+防死循环 antiLoop，**拦截时计数保持，只有用户消息才清零**)；命令 `/提醒 /好感 /作息 /节日 /发空间`；`extract_memory` 加 `include_affinity/include_reminder`（复用同一次调用）；`PROACTIVE_TOPIC_SIMILARITY` 二元组 Jaccard 主动去重；`[表情22：白眼]` 全角冒号解析。所有新配置用 `getattr` 读，缺省可跑。测试 `tests/test_qq_align.py`。
- 坑：PowerShell 单行命令传中文给 `python -c` 会被 GBK 破坏（曾把部署 config 追加成乱码）→ 改配置一律写 `.py` 脚本用 UTF-8 读写。
- **Cortico 双架构兼容层**（2026-09-29 新增，默认关）：`libs/qq_bot_runtime/cortico/` 实现 Cortico `api=5` 扩展契约的 Python 镜像 + Node 运行时桥（stdio JSON-RPC 真跑 TS world 包）；装配层 `registry.WorldAssembly` 对齐 `cortico/src/world.ts`；`brain.CorticoWorldBrain` 事件驱动。开关 `config.CORTICO_ENABLED`，World 经 `plugin_registry` 条目 `cortico_worlds` 挂载。契约源：`D:\testing\Cortico\src\core\types.ts` 与 `src/world.ts`。首个落地 World：cortico-world-dungeon@0.1.2（需 CORTICO_PACKAGE_ROOTS + CORTICO_CORE_ROOT）。
- Node 钩子坑：Windows `--import` 只认 `file://` URL（入口脚本反而用普通路径）；钩子必须 `register()`；必须同时导出 `resolve`+`resolveSync`（tsx 走同步链）。

## 8. 用户偏好（按权重定，勿把旧事实写死）
- 语音加权真相：早期要语音能用且好听 + 后期嫌磨叽 → 取中间态（可用 + 语义择机触发 + 回复简短）。勿写死"强烈偏好/讨厌语音"。
- 交互风格：简短随意打字式；长内容/代码/列表走文字；人设 `[回复长度]`=随手打字 1-3 句、`[语气强调]`=动作描写点到为止。
- 事实权重原则（元指令）：冲突时按"时效性+具体性+频次"加权，新观察可修正旧事实，留权重/反例而非绝对断言。
- 群聊多人聚合+话题检测：`chat_service` 模块级 `_GROUP_MSG_BUFFER`+`_record_group_message`+`_detect_group_topic`(防抖 LLM 短标签)+`_get_group_context`(话题+最近≤15条)；`_chat_pipeline` 群聊注入整体上下文；`should_reply` 语义接话也喂群整体上下文。
