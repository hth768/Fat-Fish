# 肥鱼娘 App · 独立便携版

**全便携**桌面 AI 伴侣：主体软件自带 Python 解释器与运行引擎，**clone 后即可运行**
（纯文字聊天 + 云端 TTS）；16 个能力插件 + 4 类资源大件通过**插件市场**按需安装。

```
feiyu_standalone/            ← 本仓库（软件主体，~1.5GB）
├─ FeiyuApp.exe              双击启动（首启自动建环境，之后秒开）
├─ start_app.bat             命令行启动（带控制台日志，排障用）
├─ app.py / server.py / settings_store.py   APP 壳（界面/桥接/覆盖式配置）
├─ bridge/                   桥接层（核心宿主/记忆/总结/插件市场/配置 API）
├─ webui/                    前端（仪表盘/聊天/记忆/总结/插件/配置）
├─ plugins/                  插件库（**初始为空**——从插件市场安装）
└─ libs/
   ├─ offline_deps_core.zip  首启依赖离线包 5.46GB（Releases 下载，见下）
   └─ qq_bot_runtime/        引擎（含 51MB 基础解释器；模型/资源不在仓库）

<盘>:\plugins\               ← 插件仓库（包外，Releases 下载后解压到此处）
├─ plugins/     16 个插件代码包（也可从市场装）
├─ mc_pack/     Minecraft 资源（mc_bot + mc_mod + _mc_ref）
├─ tools_pack/  语音转码（ffmpeg + silk）
├─ voice_pack/  本地语音（VoxCPM2 模型 + venv_vox 推理环境）
└─ vl_pack/     本地视频理解模型（Qwen2.5-VL）
```

## 快速开始

1. **clone 本仓库**（或 Releases 下载主包 zip）
2. **下载依赖离线包**：Releases 的 `feiyu_core.part1/2/3`（已发布 ✓），合并后放到
   `libs/offline_deps_core.zip`（不下载也能跑——首启自动转 pip 在线装，约 30-90 分钟）
3. 双击 `FeiyuApp.exe` → 首启自动配置（进度窗口，约 2-3 分钟）→ 「配置」页填 DeepSeek/GLM
   Key → 保存 → 聊天
4. 需要插件时：插件资源包解压到 `<盘>:\plugins\`（与主包同盘）→ App「插件」页 →
   **插件市场**点「安装」→ 列表中启用。也可点「**寻找插件**」全盘自动扫描安装

> 分卷合并：Windows `copy /b feiyu_core.part1+feiyu_core.part2+feiyu_core.part3 feiyu_core.zip`
> 或 `python _split_release.py merge feiyu_core`（自动 sha256 校验）。

## 插件一览（市场内按需安装）

每个插件独立安装/卸载，互不影响；插件代码装入主包 `plugins/`，
资源大件以**目录联接**挂入引擎（不复制、不占双份空间）。

### 平台类（接入聊天渠道）
| 插件 | 功能 | 需下载资源包 | 额外依赖 |
|---|---|---|---|
| `qq_platform` | QQ 聊天（NapCat/OneBot v11 协议） | — | QQ NT + NapCat |
| `bilibili_platform` | B 站直播弹幕互动 | — | B 站 cookie |
| `console_platform` | 控制台调试 | — | — |

### 功能类
| 插件 | 功能 | 需下载资源包 | 额外依赖 |
|---|---|---|---|
| `vox_tts` | 本地 VoxCPM2 语音合成 | `voice_pack`（7.3GB） | **NVIDIA 显卡**（无卡自动降级 GLM 云端语音） |
| `bilibili_dm` / `bilibili_learn` | B 站私信/直播间学习 | — | bilibili_platform |
| `balance_monitor` / `proactive_speaker` / `greeting_demo` | 余额监控/主动搭话/问好示例 | — | — |

### 大脑类（游戏与桌面智能体，含大脑实现）
| 插件 | 功能 | 需下载资源包 | 额外依赖 |
|---|---|---|---|
| `brain_mc_mod` | 模组世界 MC 自主生存大脑 | `mc_pack`（0.25GB） | FeiyuAPI mod 服务 |
| `brain_mc_bot` | 原版世界 MC Bot（状态通道） | `mc_pack` | mineflayer 独立进程 |
| `brain_pc` | 电脑操控（截屏→决策→键鼠） | — | /电脑做 命令 |
| `brain_pvz` | 植物大战僵尸自动对局 | — | **需先装 brain_pc**；游戏本体放 `pvz_games/` |

### 资源包（供上述插件使用，解压到 `<盘>:\plugins\` 后市场安装/自动接线）
| 资源包 | 内容 | 体积 | Releases 分卷 |
|---|---|---|---|
| `voice_pack` | VoxCPM2 模型 + venv_vox 推理环境 | 7.3 GB | part1/2/3 |
| `vl_pack` | Qwen2.5-VL 本地视频理解模型（云端优先时仅回退） | 6.1 GB | part1/2/3/4 |
| `mc_pack` | mineflayer node_modules + MC mod | 0.25 GB | 单文件 |
| `tools_pack` | ffmpeg + silk 语音转码 | 0.29 GB | 单文件 |
| `plugins_pack` | 16 个插件代码包（**不解压也可用市场逐个安装**） | 0.2 MB | 单文件 |

### 「寻找插件」全盘扫描
插件页点「寻找插件」：自动扫描所有本地硬盘（限深 3 层、跳系统目录与联接、120 秒超时），
发现特征匹配的插件包/资源大件即**自动安装接线**——把你手里任何位置的插件丢进
任意目录都能被找到。

## 从源码发布（维护者）

```powershell
python _split_release.py build   # 生成 dist/：各 pack zip + >1.9GB 自动分卷 + SHA256SUMS.txt
python _split_release.py merge feiyu_core   # 合并分卷并校验
```
Releases 上传 dist/ 全部文件即可（单资产 <2GiB）。

## 目标机器需求（唯一的外部依赖）

## 目标机器需求（唯一的外部依赖）

| 组件 | 需求 | 缺了会怎样 |
|---|---|---|
| Windows 10/11 | 必须 | —（.NET Framework / WebView2 系统自带） |
| DeepSeek/GLM API Key | 必须 | 配置页填入，不填无法聊天（保存即热生效，无需重启） |
| NVIDIA 显卡 | 可选 | 无 N 卡时 `vox_tts` 插件自动降级 GLM 云端 TTS，文字聊天不受影响 |
| QQ NT + NapCat | 可选 | 仅启用 `qq_platform` 插件才需要（NapCat 在 E:\NapCat 抢救包） |
| Minecraft / PvZ / CodeBuddy CLI | 可选 | 对应大脑插件才需要，config.py 内有注释说明 |

## 纯净状态说明（本包已重置）

- **记忆与知识已清空**：人物档案、重要事项、人格记忆、反思记忆、知识库、全文历史、
  会话记忆、情绪状态、事实库全部为空——她从零开始认识你。仪表盘统计卡应全为 0。
- **提示词已恢复原版**：引擎内置的肥鱼娘原版人设（覆盖层里的自定义提示词已删除，
  可在「配置」页重新修改）。
- **API Key 已清空**：DeepSeek / GLM / B站 cookie / QQ 号等全部为空。
  首次使用：打开「配置」页 → 填入你的 DeepSeek（或 GLM）Key → 保存即热生效。
- **全部插件默认关闭**：首次启动只有 智能体核心 + 聊天 + 记忆 + 总结 + 界面。
  到「插件」页按需打开（QQ/直播/大脑/TTS 等）。

两个 App 同时运行时注意端口：默认都占 8900（后启动的会提示端口占用），用
`--port 8901` 错开。QQ 平台插件（8080 端口）同理，两个实例不要同时启用。

## 可选大件（不在仓库，按需下载）

本地语音（voice_pack 7.3GB）、视频理解模型（vl_pack 6.1GB）、MC 资源、转码工具
均不在本仓库——从 Releases 下载解压到 `<盘>:\plugins\` 后在插件市场安装。
纯文字聊天与云端 TTS **不依赖任何大件**。

## 日常维护

- **加插件**：往 `plugins/` 新建文件夹（manifest.json + plugin.py），界面点「重新扫描」。
- **改配置**：界面「配置」页（写 `data/app_settings.json` 覆盖层，不改引擎源码；
  重启后 key/路由自动重建，不会丢）。
- **备份数据**：她的记忆都落在 `libs/qq_bot_runtime/` 下的 json/db 文件，整目录拷走即可。
- **引擎升级**：用新版 `qq_bot` 替换 `libs/qq_bot_runtime` 的 `*.py` 即可
  （数据与 `config.py`/`telemetry.py` 的定制保持不动）。
