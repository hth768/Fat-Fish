# MEMORY（长期记忆）

## 项目结构：Feiyu Standalone（肥鱼单机版）
- 仓库：`e:\feiyu_standalone`（GitHub: hth768/Fat-Fish）。桌面应用 = Python 后端(HTTP) + pywebview/Edge 窗口 + 原生 JS/CSS WebUI(`webui/`)。
- bot 引擎有两份需同步的副本：
  - 捆绑版：`e:\feiyu_standalone\libs\qq_bot_runtime`（随仓库走，用户常称 "qq_bot"）
  - 独立版：`e:\qq_bot`（用户称 "独立版本"）
  - 改 bot 逻辑须两份同步，否则运行/独立版行为不一致。
- 捆绑 Python 解释器：`e:\feiyu_standalone\libs\qq_bot_runtime\runtime\python\python.exe`（语法校验用 `python -m py_compile`）。

## bot 记忆子系统（qq_bot_runtime）
- `long_term_memory.py`：用户档案（事实 facts）。`merge_profile_facts` 策略=新覆盖旧（用户改主意时合理）。
- `persona_memory.py`：AI 人格记忆（与某用户相处方式）。策略=旧事实优先，见 `reconcile_persona_traits`（冲突的新增特征被否定）。
- `reflection_memory.py`：对话反思提炼交互规则（按 user_id 键入；`user_id==""` 的规则为全局共享）。
- `important_notes.py`：一次性重要信息。`chat_service.py` 调用上述模块。
- 关键避坑：人物档案/反思必须明确主体是"用户(人类)"还是"AI(肥鱼娘)"，否则模型会把 AI 特征写进用户档案（写反/串台）。

## 外观自定义（已合入仓库 7c68046，origin/main 同步）
- `bridge/appearance_api.py`（原名 appearance.py，因 import 名不符已 git mv 改名）：THEMES 8 套、JSON 存 `data/appearance.json`、背景图原始字节经 `server.py._serve_raw`。
- WebUI：`webui/index.html|app.js|styles.css`，侧栏"外观"，pywebview `js_api.set_title` 实时改标题栏。

## git 提交规范（PowerShell 中文坑）
- 环境：Windows + PowerShell 5.1，git 默认 `i18n.commitEncoding=utf-8`。PowerShell 以 **GBK** 代码页传中文参数给 git → 中文 commit message 会**乱码存储**（chcp 65001 后仍乱码即说明已存乱码）。
- 正确方法：用工具（非命令行中文）写 UTF-8 的 message 文件，再 `git commit -F <file>`；amend 同样 `git commit --amend -F <file>`。
- 若需修正已乱码提交：`git -c i18n.commitEncoding=gbk commit --amend -F <file>`（让 git 把 GBK 字节正确解码为 UTF-8）。注意 `-m "..."` 中文若含括号等会触发 PowerShell 字符串解析错误，故一律用 `-F` 文件法。
- 验证：`chcp 65001 > $null; git --no-pager log -1 --format=%B` 应正常显示中文。
- 推送历史：曾遇 GitHub 443 超时，重试可成功。
