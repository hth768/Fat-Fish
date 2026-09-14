# 肥鱼娘 App · 独立便携版（E:/feiyu_standalone）

**全便携**独立项目：自带 Python 解释器、运行引擎（依赖库）、插件库、桌面窗口界面、
本地 TTS 模型。**整包拷到任何 Windows 10/11 机器即可运行**，不需要安装 Python。

```
E:/feiyu_standalone/
├─ FeiyuApp.exe           双击启动（内置自举：自动把 venv 指向包内解释器）
├─ start_app.bat          命令行启动（带控制台日志，自举逻辑同 exe）
├─ app.py / server.py / settings_store.py   APP 壳（界面、桥接、覆盖式配置）
├─ bridge/                                 桥接层（核心宿主/记忆/总结/插件/配置 API）
├─ webui/                                  前端（仪表盘/聊天/记忆/总结/插件/配置）
├─ plugins/            ★ 插件库（16 个可插拔插件包）
├─ libs/               ★ 依赖库（~17 GB，全部自包含）
│  └─ qq_bot_runtime/
│     ├─ runtime/python/       基础 Python 3.13 解释器（51MB，venv 重定位目标）
│     ├─ venv/                 Python 依赖（5.45GB，pyvenv.cfg 由启动器动态指向包内解释器）
│     ├─ models/ + venv_vox/   本地 VoxCPM2 TTS（9.7GB，可选：vox_tts 插件用）
│     ├─ tools/                ffmpeg + silk 编解码（语音功能，config 按包内路径推导）
│     ├─ *.py                  智能体引擎源
│     ├─ data/                 运行数据（hf_cache/video_tmp 等，首次自动填充）
│     └─ emojis/               表情资源
├─ pvz_games/             （可选）放 PlantsVsZombies.exe 供 PVZ 大脑自动开局
└─ data/                  APP 覆盖层配置（纯净：全部插件默认关、无任何密钥）
```

## 启动（拷到任何 Windows 机器）

1. 把整个 `feiyu_standalone` 文件夹拷到目标机器任意位置（如 `D:\feiyu_standalone`）
2. 双击 **`FeiyuApp.exe`** → 桌面窗口弹出 → 「配置」页填入你的 DeepSeek（或 GLM）Key → 保存
3. 聊天。首次启动自动完成：venv 指向包内解释器（无需装 Python）、数据目录创建

也可双击 `start_app.bat`（带控制台日志，排障用）。
路径解析优先级：`libs\qq_bot_runtime`（本依赖库）→ 环境变量 `FEIYU_QQ_BOT` → `f:\qq_bot`。

## 分开下载（主包 + 可选插件资源包）

主体核心与插件大资源已分离，可分开下载（用 `python _make_packs.py all` 在本机生成 `dist/` 各 zip）：

| 包 | 内容 | 体积约 | 对应功能 |
|---|---|---|---|
| `feiyu_core.zip` | 引擎 + Python + 界面（**不含插件**） | ~8 GB | **必下**。纯文字聊天 + 云端 TTS 即开即用 |
| `plugins_pack.zip` | 16 个插件包装器 + 依赖分组 | <1 MB | 插件页（不解压则插件页为空，核心功能不受影响） |
| `voice_pack.zip` | VoxCPM2 模型 + venv_vox | 9.7 GB | vox_tts 本地语音插件（需 NVIDIA 卡） |
| `mc_pack.zip` | mc_bot（mineflayer）+ mc_mod | 0.65 GB | Minecraft 两大脑插件 |
| `tools_pack.zip` | ffmpeg + silk 编解码 | 0.68 GB | 语音消息转码（QQ 语音链路） |
| `vl_pack.zip` | Qwen2.5-VL 视频理解模型 | 7 GB | 本地视频理解回退（云端优先不受影响） |

**安装方式**：解压 `feiyu_core.zip` 后即可使用（纯文字聊天 + 云端 TTS）。
需要插件时：把插件资源包解压到 `<盘>:\plugins\`（与主包同盘，如
`voice_pack.zip` → `E:\plugins\voice_pack`，`plugins_pack.zip` → `E:\plugins\plugins`），
然后打开 App「插件」页 → **插件市场**，点「安装」即可——
插件代码装入主包插件库（`plugins/`，初始为空），资源大件以目录联接挂入引擎
（不复制、不占双份空间），装完在插件列表中启用即可。也可「卸载」随时移除。
**缺插件不影响启动**：插件页/市场为空、语音走 GLM 云端、其余功能完好。

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

## 可选大件（本包已含，无需补齐）

本地 VoxCPM2 TTS 的 `venv_vox/`（5.08 GB）与 `models/`（4.7 GB）**已包含在
libs/qq_bot_runtime/ 内**，在「插件」页启用 `vox_tts` 包即可（需 NVIDIA 显卡）。
云端 TTS / 纯文字聊天不依赖这些。

## 日常维护

- **加插件**：往 `plugins/` 新建文件夹（manifest.json + plugin.py），界面点「重新扫描」。
- **改配置**：界面「配置」页（写 `data/app_settings.json` 覆盖层，不改引擎源码；
  重启后 key/路由自动重建，不会丢）。
- **备份数据**：她的记忆都落在 `libs/qq_bot_runtime/` 下的 json/db 文件，整目录拷走即可。
- **引擎升级**：用新版 `qq_bot` 替换 `libs/qq_bot_runtime` 的 `*.py` 即可
  （数据与 `config.py`/`telemetry.py` 的定制保持不动）。
