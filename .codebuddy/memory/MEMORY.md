# MEMORY（长期记忆）

## 项目结构：Feiyu Standalone（肥鱼单机版）
- 仓库：`e:\feiyu_standalone`（GitHub: hth768/Fat-Fish）。桌面应用 = Python 后端(HTTP) + pywebview/Edge 窗口 + 原生 JS/CSS WebUI(`webui/`)。
- bot 引擎/App 部署副本（共三处，须注意同步）：
  - 捆绑版：`e:\feiyu_standalone\libs\qq_bot_runtime`（随仓库走，App 直接调用）
  - 独立版：`e:\qq_bot`（用户称 "独立版本"，App 层改动不含此目录）
  - **`E:/feiyu_app`**：第二份 standalone App 部署（有 `bridge/ webui/ server.py app.py`，无 `libs/`）。其 `start_app.bat` 引擎探测优先级为 `E:/feiyu_app/runtime/qq_bot`(不存在) → **`E:\qq_bot`(命中)** → `f:\qq_bot`(不存在)。即 **E:/feiyu_app 实际复用 `e:\qq_bot` 作为引擎**。
  - 同步经验：E:/feiyu_app 的 App 层改动直接按相对路径复制（`settings_store/app/server.py bridge/* webui/*` 等）；引擎层改动只需落在 `e:\qq_bot` 即自动对 E:/feiyu_app 生效，不必再单独建 `libs/` 副本（README 虽写捆绑 libs，但该部署实际靠 env 指向 e:\qq_bot）。
  - **启动必须带环境变量 `FEIYU_QQ_BOT=E:\qq_bot`**（来自 `start_app.bat`）。直接 `python E:\feiyu_app\app.py --with-core` 不带该变量会因「找不到 qq_bot 运行时」而退出。引擎解析优先级：`FEIYU_QQ_BOT` → `f:/feiyu_app/runtime/qq_bot` → `e:\qq_bot` → `E:/feiyu_app/libs/qq_bot_runtime`；原常驻实例(PID 29624)能跑正是因 workbuddy 启动环境里设了该变量。
  - **重启 E:/feiyu_app 实例的正确方式**：①`Get-CimInstance Win32_Process` 找 8900 监听 PID（命令行含 `feiyu_app\app.py`）→ `Stop-Process -Id <pid> -Force`；②`cmd /c "set FEIYU_QQ_BOT=E:\qq_bot && E:\qq_bot\venv\Scripts\python.exe E:\feiyu_app\app.py --with-core"` 拉起（用 `Start-Process` 脱离，勿用 `-NoNewWindow` 否则阻塞工具）。注意：重启会短暂关闭用户当前 App 窗口，新实例会自动重开窗口。
  - 端点校验提醒：`/api/appearance/icon` 与 `/api/appearance/icon/reset` 是 **POST**（server.py POST 分支），用 GET 测会 404；`/api/memory/export` 是 GET。POST 空参数返回 400 即代表端点已注册。
  - **【提交约定】本地部署副本的改动一律不提交**：
    - `E:/feiyu_app/`：本地工作/运行副本，非 git 仓库，含运行态临时文件（`_boot.log`/`_boot.err`），绝不纳入提交。
    - `e:\qq_bot`（独立版）：**不是 git 仓库**（无 `.git`/`.gitignore`），只是本地引擎工作副本，同步过去的改动也不提交。
    - 即：任何 `git add`/提交都只针对 `e:\feiyu_standalone` 主仓库；向 `e:\qq_bot`、`E:/feiyu_app` 同步仅为本地运行，不做版本化。
    - 注意 `e:\qq_bot\ai_providers.json` 含真实 API Key，本就无 git 保护，切忌复制它到任何 git 跟踪目录。
  - **模型注册表约定（两版现已一致）**：`config.py` 的 `AI_PROVIDERS={}`、`AI_CAPABILITY_ROUTING` 置空，真实供应商 Key 放在同目录 `ai_providers.json`（覆盖层，ai_provider.py 读取，支持热重载）。**切勿把真实 Key 写死进 config.py 或提交到 git**；`GLM_API_KEY` 等顶层变量（语音用）保留。独立版 `ai_providers.json` 含真实 Key，须 .gitignore 忽略。
- 捆绑 Python 解释器：`e:\feiyu_standalone\libs\qq_bot_runtime\runtime\python\python.exe`（语法校验用 `python -m py_compile`）。

## bot 记忆子系统（qq_bot_runtime）
- `long_term_memory.py`：用户档案（事实 facts）。`merge_profile_facts` 策略=新覆盖旧（用户改主意时合理）。
- `persona_memory.py`：AI 人格记忆（与某用户相处方式）。策略=旧事实优先，见 `reconcile_persona_traits`（冲突的新增特征被否定）。
- `reflection_memory.py`：对话反思提炼交互规则（按 user_id 键入；`user_id==""` 的规则为全局共享）。
- `important_notes.py`：一次性重要信息。`chat_service.py` 调用上述模块。
- 关键避坑：人物档案/反思必须明确主体是"用户(人类)"还是"AI(肥鱼娘)"，否则模型会把 AI 特征写进用户档案（写反/串台）。
- **防串台设计原则（用户明确提出的架构要求）**：每条发给 AI 的消息，上下文里都必须带"当前说话人是谁"的信息；否则上下文按 user_id 隔离虽在，但 user_id 为空/被共用、或旧会话残留混入时，模型从单条 role:user 消息看不出身份 → 串台。
  - 已实现：在 `_chat_pipeline` 最终 `self.llm.chat` 之前，把每条 `user` 消息的 content 前缀加上 `[当前用户称呼]`（来自 `emotion.resolve_display_name(user_id)`，空则回退 user_id/"用户"）。构建新列表、不改动持久化历史 dict。
  - 用户原话给的两条路："每条消息注入用户信息 **或** 及时清空上下文"。选了前者（注入身份），因为清空上下文会丢连续性，且无法解决多用户共用 key 的问题。
  - 注：`build_memory_messages` 的 profile/notes/persona 注入有 `if user_id` 守卫——user_id 为空时整段跳过，这是串台的另一个隐患点（已靠 user 消息打标签兜底）。

## 外观自定义（已合入仓库 7c68046，origin/main 同步）
- `bridge/appearance_api.py`（原名 appearance.py，因 import 名不符已 git mv 改名）：THEMES 8 套、JSON 存 `data/appearance.json`、背景图原始字节经 `server.py._serve_raw`。
- WebUI：`webui/index.html|app.js|styles.css`，侧栏"外观"，pywebview `js_api.set_title` 实时改标题栏。

## git 提交规范（PowerShell 中文坑）
- 环境：Windows + PowerShell 5.1，git 默认 `i18n.commitEncoding=utf-8`。PowerShell 以 **GBK** 代码页传中文参数给 git → 中文 commit message 会**乱码存储**（chcp 65001 后仍乱码即说明已存乱码）。
- 正确方法：用工具（非命令行中文）写 UTF-8 的 message 文件，再 `git commit -F <file>`；amend 同样 `git commit --amend -F <file>`。
- 若需修正已乱码提交：`git -c i18n.commitEncoding=gbk commit --amend -F <file>`（让 git 把 GBK 字节正确解码为 UTF-8）。注意 `-m "..."` 中文若含括号等会触发 PowerShell 字符串解析错误，故一律用 `-F` 文件法。
- 验证：`chcp 65001 > $null; git --no-pager log -1 --format=%B` 应正常显示中文。
- 推送历史：曾遇 GitHub 443 超时，重试可成功。
