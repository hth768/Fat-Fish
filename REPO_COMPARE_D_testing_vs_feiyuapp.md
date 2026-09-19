# 组织仓库全量对比分析报告

生成时间：2026-09-19
分析范围：
- `E:\feiyu_app` —— feiyu 单机版运行实例（非 git 仓库，上游 `hth768/Fat-Fish.git`）
- `D:\testing` —— `Pal-AI-Lab` 组织下 7 个本地 git 仓库副本
- 参照：`E:\feiyu_standalone` —— feiyu 项目主开发仓库（git，上游同 feiyu_app）

---

## 一、总览结论

**关键发现：`E:\feiyu_app` 与 `D:\testing` 的 7 个仓库没有任何代码交集，属于完全不同的项目体系。**

| 维度 | feiyu_app / feiyu_standalone | D:\testing 7 仓库 |
|------|------------------------------|------------------|
| 上游组织 | `hth768`（个人） | `Pal-AI-Lab`（组织） |
| 仓库 | Fat-Fish（单一 Python 项目） | Cortico / Cortina / META-TINA / ThereIsNoApp / 3×cortico-world-* |
| 语言 | Python（FastAPI/Flask 风格） | TypeScript / Node 为主，部分为纯文档 |
| 功能 | QQ/多平台 Bot + 构建助手 + 插件系统 | Agent 框架 / 记忆 / VTuber / 游戏 MC 接入等 |
| feiyu 相关文件 | 有（app.py/bridge/plugins） | **零**（7 仓库均无 app.py/builder_api/feiyu 字样） |

=> 二者无法做"同名仓库内容 diff"，只能做**组织级横向盘点 + 各自完整性审计**。

---

## 二、E:\feiyu_app 与 E:\feiyu_standalone 关系

- `feiyu_app` 是 `feiyu_standalone` 的运行实例副本（非 git）。
- 文件集差异：feiyu_app 独有 31 个文件（全部为 `plugins/<pkg>/{manifest.json,plugin.py}` + `_boot.log/err`）；feiyu_standalone 独有 4992 个（主要是 `.git`、`.codebuddy/memory`、`_build/` 打包产物、`.vscode`）。
- 结论：feiyu_app 由 feiyu_standalone 同步而来，运行时注入 `plugins/`（16 个真实插件包）与 `data/`。
- feiyu_standalone 当前 HEAD：`10969bb docs: README 增加多 Bot 章节`（领先于昨日提交的 `b4e3bcf` 工作区读写等）。

---

## 三、D:\testing 组织仓库全量盘点

### 3.1 仓库清单与 git 状态

| 仓库 | 上游 | 当前 HEAD | 领先/落后 origin | 脏文件 | 最近提交 |
|------|------|-----------|-----------------|--------|----------|
| **Cortico** | Pal-AI-Lab/Cortico | e35a79e | ahead=4 / behind=0 | 1 | 4 days ago (hth768) |
| **cortico-world-mc-agent** | Pal-AI-Lab/cortico-world-mc-agent | **无 commit**（detached） | — | 8（全未跟踪） | 无 |
| **cortico-world-memory** | Pal-AI-Lab/cortico-world-memory | **无 commit**（detached） | — | 5（全未跟踪） | 无 |
| **cortico-world-vtuber** | Pal-AI-Lab/cortico-world-vtuber | 3d830a4 | ahead=0 / behind=0 | 0 | 4 days ago (Phant) |
| **Cortina** | Pal-AI-Lab/Cortina | c0b467c | ahead=0 / behind=0 | 0 | 6 days ago (Phant) |
| **META-TINA** | Pal-AI-Lab/META-TINA | 40c9266 | ahead=0 / behind=0 | 0 | 6 days ago (Phant) |
| **ThereIsNoApp** | Pal-AI-Lab/ThereIsNoApp | a95f4d5 | ahead=0 / behind=0 | 0 | 6 days ago (Phant) |

### 3.2 规模与语言分布（排除 node_modules/dist/.git）

| 仓库 | 总文件数 | 代码文件数 | 主要语言 | 备注 |
|------|---------|-----------|----------|------|
| **Cortico** | 815 | 586 | TS/TSX | 主框架，最大最完整 |
| **cortico-world-vtuber** | 108 | 85 | TS | 较完整 |
| **META-TINA** | 49 | 0 | 文档/MD | 仅文档与配置（0 代码） |
| **Cortina** | 17 | 0 | 文档/MD | 仅 v0.1.0 发布件（0 代码） |
| **cortico-world-memory** | 14 | 10 | TS | 新建未提交 |
| **cortico-world-mc-agent** | 16 | 9 | TS | 新建未提交 |
| **ThereIsNoApp** | 3 | 0 | 文档 | 最小（仅 LICENSE/README） |

### 3.3 重点问题

1. **两个仓库尚未初始化提交**：
   - `cortico-world-mc-agent`、`cortico-world-memory` 仅有 `git init`，所有文件（src/、package.json、tsconfig 等）均为未跟踪状态，HEAD detached，无 `origin/HEAD`。**这些代码随时可能因误操作丢失，且无法 push。**
   - 建议：立即 `git add -A && git commit -m "init"`，并确认远程默认分支（main/master）已设置。

2. **Cortico 本地领先远程 4 个提交未推送**：
   - HEAD `e35a79e` 是 `Merge remote-tracking branch 'origin/master' into fix/code-review-findings`，本地有 4 个未 push 提交（likely 在 `fix/code-review-findings` 分支）。
   - 还有 1 个脏文件（可能是 merge 冲突残留或新改动）。需 `git status` 确认后 push 或处理。

3. **Cortina / META-TINA 实为"文档型仓库"**：
   - 代码文件数为 0，仅含 README/AGENTS.md/LICENSE/配置。它们可能是 Agent 定义/提示词仓库，而非传统代码仓库——与 feiyu_app 的"可执行插件系统"定位不同。

4. **ThereIsNoApp 几乎为空**：仅 3 个文件，可能是占位/孵化中项目。

---

## 四、跨仓库横向对比（组织内共性）

- **技术栈一致性**：除 feiyu（Python）外，Pal-AI-Lab 组织 7 仓库统一用 TypeScript + pnpm workspace（Cortico 含 `pnpm-workspace.yaml`）。
- **提交人**：Cortico 最近由 `hth768` 提交，其余 5 个由 `Phant` 提交——说明组织内有至少 2 名活跃贡献者。
- **活跃时间窗**：全部集中在 4–6 days ago，属于同一波开发节奏。
- **许可证**：META-TINA / ThereIsNoApp 已加 MIT LICENSE；Cortico 等需确认 LICENSE 是否齐备（其根目录未见 LICENSE 文件 listing，但有 `LICENSE` 在 feiyu 侧）。

---

## 五、与 feiyu_app 的关联可能性

用户在 `D:\testing` 中寻找 feiyu 相关代码——**未找到任何匹配**。两种可能：
1. feiyu 项目（Fat-Fish）不属于 Pal-AI-Lab 组织，是独立个人仓库 `hth768/Fat-Fish`。
2. 若希望把 feiyu 的插件/builder 能力迁移或对齐到 Pal-AI-Lab 的某个 Agent 框架（如 Cortico），需要做**架构适配**（Python vs TS 鸿沟大，建议以"插件协议对齐"而非代码复用方式）。

---

## 六、行动建议

| 优先级 | 动作 | 涉及仓库 |
|--------|------|----------|
| P0 | 为 mc-agent / memory 两个仓库创建首个 commit 并推远程 | cortico-world-mc-agent, cortico-world-memory |
| P1 | 推送 Cortico 本地 4 个领先提交，清理 1 个脏文件 | Cortico |
| P2 | 确认 Cortina/META-TINA 是否应含代码（当前为纯文档） | Cortina, META-TINA |
| P3 | 评估 feiyu（Python）与 Pal-AI-Lab（TS）是否需要互通，若需要定义跨语言插件协议 | feiyu_app ↔ Cortico |

---

## 附录：检查命令（可复现）

```powershell
# 仓库性质
Test-Path E:\feiyu_app\.git          # False（运行实例）
cd D:\testing; git -C <repo> remote -v
# 领先/落后
git -C <repo> rev-list --count origin/HEAD..HEAD   # ahead
git -C <repo> rev-list --count HEAD..origin/HEAD   # behind
# 文件计数（排除 node_modules/dist/.git）
Get-ChildItem <repo> -Recurse -File | Where-Object {$_.FullName -notmatch '\\node_modules\\|\\.git\\|\\dist\\'}
```
