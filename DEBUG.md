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

## 排查技巧速记

- 国内访问 GitHub 不便时，用 GitHub API 而非网页定位 CI：
  - `GET /repos/<owner>/<repo>/actions/runs?per_page=20` 看最近 runs 的 `conclusion` / `head_sha`；
  - `GET /repos/<owner>/<repo>/actions/runs/<id>/jobs` 看各 step 状态；
  - 在 `ci.yml` 加 commit comment 步骤把 traceback 回贴到提交评论。
- Windows PowerShell 下中文 commit message 用 `>` 写 `commit_msg.txt` 再 `git commit -F`；`curl` 被别名成 `Invoke-WebRequest` 时改用 `cmd /c curl ...` 或 `python -c urllib`。
- 平台耦合断言：凡断言里出现字面路径/前缀（`m.py:`、`/tmp/...`、大小写），优先改路径无关匹配。
