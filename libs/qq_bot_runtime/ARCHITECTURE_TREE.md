# 肥鱼娘 / DeepSeek娘 · 项目架构树状图

> 范围：`f:/qq_bot`（独立项目，参考猫娘计划/N.E.K.O 设计思路）。
> 核心理念：**智能体为核心，平台即插件，大脑可注册**。核心（`AgentCore`）不依赖任何聊天平台，
> QQ / 控制台 / B站 / Web 都是可插拔的平台插件；各类自主智能体（MC / 电脑操控 / PVZ）是注册进大脑注册表的「大脑」。

```
肥鱼娘 / DeepSeek娘 (qq_bot)
│
├── ① 启动与装配层（Entry & Assembly）
│   ├── start.bat / start.py ──────────── 统一启动器门面（一键拉起 Web+记忆/监控 sidecar）
│   ├── start_web.bat ─────────────────── 双击一键启动 Web 控制台（核心失败自动降级 --no-core）
│   ├── launcher.py ───────────────────── 编排 sidecar 就绪后再启动 bot（ACTIVE_LAUNCHER 单例）
│   ├── launcher_core/ ────────────────── 启动器基础设施（bootstrap 环境自愈 / runtime SidecarSpec）
│   ├── main.py ───────────────────────── 平台装配入口（按 config / --qq/--console/--bili/--web 加载插件）
│   ├── bot.py ────────────────────────── 兼容垫片（= main.py --qq，start.bat 无需改动）
│   ├── run_agent.py ──────────────────── 控制台模式 + 可选 --mc 自主游戏
│   └── config.py ─────────────────────── 所有开关与凭据（AI 供应商 / 平台 / 大脑 / 学习 / 情绪…）
│
├── ② 智能体核心层（AgentCore — 不依赖任何平台）
│   ├── agent_core.py ─────────────────── 核心：生命周期 / 插件注册 / 事件转发 / 大脑注册表 / 求助兜底
│   ├── brain_base.py ─────────────────── AgentBrain 大脑接口 / BrainManager 注册表 / brain.event 统一事件
│   ├── plugin_base.py ────────────────── Plugin / PlatformPlugin / FeaturePlugin / PluginManager
│   │                                      + 注册表校验（validate_registry：漂移检测）
│   ├── plugin_registry.py ────────────── 统一插件注册表（唯一事实来源）：平台/功能/大脑/sidecar
│   │                                      清单 + 版本约束 + 依赖/循环/顺序校验（含 core 伪条目）
│   ├── message_bus.py ────────────────── InboundMessage / ReplyTarget / MessageSender / AgentEventBus 事件总线
│   └── scheduler.py ──────────────────── 后台定时任务（余额监控循环等）
│
├── ③ 大脑层（Brains — 注册进 AgentCore.brains）
│   ├── chat（聊天大脑，消息式，随核心常开）
│   │   └── chat_service.py ───────────── 命令分发 / 记忆 / 图片语音视频 / 意图识别 / MC 注入 / DeepSeek 调用
│   ├── mc_mod（模组世界 MC 大脑，自主循环，`/mc自动` 或 run_agent --mc 启动）
│   │   ├── mc_agent.py / mc_bot_brain.py ─ 自主生存/决策大脑
│   │   ├── mc_common.py / mc_nav.py / mc_explored.py ─ 导航/探索/常识
│   │   ├── mc_survival.py ───────────── 生存策略（被 mc_agent/mc_bot_brain/mc_recipes 共用）
│   │   ├── mc_recipes.py / mc_recipe_usage.py ─ 合成配方
│   │   ├── mc_critic.py / mc_analyze.py ─ 自评/分析
│   │   ├── mc_skills.py / mc_routines.py ─ 技能/例行
│   │   ├── mc_monitor.py / mc_game_watch.py / mc_watcher.py ─ 监控/观战
│   │   ├── mc_live.py / mc_dashboard.py ─ 直播推流/看板
│   │   └── mc_mods.py / mc_tips.py ───── 模组/技巧库
│   ├── mc_bot（原版世界 MC Bot 大脑，外部进程 `python mc_bot_run.py`）
│   │   ├── mc_bot_run.py ─────────────── Node mineflayer 桥（子进程）
│   │   └── mc_bot/ ───────────────────── mineflayer 工程（ts/js，独立运行）
│   ├── pc（电脑操控大脑，任务制，主人 `/电脑做 <任务>`）
│   │   ├── pc_agent.py ───────────────── 截屏→视觉理解→工具调用→键鼠执行（四层护栏）
│   │   ├── pc_control.py ─────────────── 执行通道：mss 截屏 + pyautogui 键鼠 + 受限 shell + 急停
│   │   └── pc_intent.py ──────────────── 任务意图解析
│   └── pvz（PVZ 游戏大脑，实时对抗循环，主人 `/pvz玩`）
│       ├── pvz_agent.py ──────────────── 快层收阳光 + 慢层读盘决策循环
│       ├── pvz_vision.py ─────────────── 窗口定位/截图/阳光检测/网格换算（标定 1.2.0.1073）
│       ├── pvz_strategy.py ───────────── 纯函数规则引擎（救火>经济>火力>防线）
│       └── pvz_tips.py ───────────────── 攻略+复盘技巧库
│
├── ④ 平台插件层（Platform Plugins — 接入聊天平台）
│   ├── qq_plugin.py ──────────────────── QQ 平台（NapCat/OneBot WS、表情码、媒体抓取、SILK 解码、私聊聚合）
│   ├── qq_adapter.py ─────────────────── QQ 主动发送（实现 MessageSender）
│   ├── console_plugin.py ─────────────── 控制台平台（终端聊天，最小参考实现）
│   ├── web_plugin.py ─────────────────── Web 平台（http+SSE 前端、只读数据 API、控制 API）
│   └── bilibili_plugin.py ────────────── B站直播平台（弹幕 WS、触发词、礼物事件、断线重连）
│       ├── bili_streamer.py ──────────── B站开播"身体"（RTMP+ffmpeg 推流、直播嗓音 speak/play_wav）
│       ├── bili_captions.py ──────────── 直播字幕服务（环形缓冲 + 自绘页面）
│       ├── bili_api.py ───────────────── B站共享 API 层（Cookie/wbi 风控、GET/POST 封装、弹幕压缩）
│       ├── bili_dm.py ────────────────── B站私信 DM（白名单，复用核心大脑）
│       ├── bili_learn.py ─────────────── B站视频学习（移植 BLB：看→总结→沉淀）
│       └── bili_learn_scheduler.py ───── B站每日定时自主学习（关键词/UP 搜索→学习→分享）
│
├── ⑤ 功能插件层（Feature Plugins — 核心后台能力）
│   ├── BalanceMonitorPlugin（agent_core 内建）─ DeepSeek 余额监控
│   ├── ProactiveSpeakerPlugin（agent_core 内建）─ 主动说话调度器（私聊/接话/游戏分享）
│   │   └── proactive_speaker.py ──────────────── 主动播报引擎
│   └── intent_router.py ──────────────────────── 意图路由（命令/对话分流）
│
├── ⑥ 多供应商 LLM 与视觉层（AI Capability）
│   ├── ai_provider.py ───────────────── 统一 LLM 入口（AI_PROVIDERS + AI_CAPABILITY_ROUTING，故障转移）
│   ├── deepseek_client.py / gemini_client.py / glm_client.py ─ 各供应商客户端
│   ├── ai_profile.py ────────────────── AI 画像/角色
│   ├── vision_capture.py ────────────── 截图采集（视觉理解）
│   ├── screen_awareness.py ──────────── 屏幕感知（主动开口用）
│   ├── local_video_understand.py / video_processor.py ─ 本地视频理解/处理
│   └── camera_capture.py / dxcam_capture.py ─── 摄像头/屏幕采集
│
├── ⑦ 记忆与知识层（Memory & Knowledge）
│   ├── memory.py ───────────────────── **记忆 + 会话统一体**：Memory（持久化三级记忆
│   │                                     底座）/ Session(Memory)（持久化会话）/
│   │                                     SessionManager（按用户会话+预热+热切换）/
│   │                                     SessionManagerAdapter（ChatService.memory 接口）
│   ├── session_manager.py ───────────── 兼容垫片（re-export memory 的会话符号）
│   ├── long_term_memory.py ─────────── 长期记忆
│   ├── persona_memory.py / reflection_memory.py ─ 人设/反思记忆
│   ├── time_indexed_memory.py ───────── 时间索引记忆
│   ├── vector_memory.py ─────────────── 向量记忆（embedding 检索）
│   ├── knowledge_service.py / knowledge_store.py ─ 自主学习闭环（搜索→沉淀→复用）
│   ├── memory_context.py ────────────── 统一记忆上下文（记忆上下文聚合，供聊天/意图/MC 共用）
│   ├── memory_server.py / memory_client.py ─ sidecar RPC（重负载独立进程，掉线降级进程内）
│   │   └──（依赖通用骨架 service_host.py，见⑩）
│   ├── emotion.py ───────────────────── AI 情绪模块（全局一份心情，落盘 emotion_data.json）
│   ├── identity.py ──────────────────── 身份注册表（QQ↔MC 双向绑定，identity_bindings.json）
│   ├── self_knowledge.py ────────────── 统一查看/学习/删除接口（tips/facts）
│   ├── fact_store.py / knowledge_base.json ─ 事实库（SQLite FTS5）
│   ├── important_notes.py ───────────── 重要笔记
│   ├── user_profiles.json / chat_history/ ─ 用户档案 / 全文历史
│   └── file_lock.py ─────────────────── 跨进程文件锁（msvcrt 字节区锁）
│
├── ⑧ 语音 / TTS 层（Voice & Speech）
│   ├── realtime_voice.py ────────────── 实时语音 LOCAL-RT（麦克风→云端 ASR→云端 LLM→本地 vox TTS）
│   ├── realtime.py ──────────────────── 实时语音会话
│   ├── voice_room.py ────────────────── 语音房间（LE202 设备解析/重采样播放）
│   ├── voice_client.py ──────────────── 语音客户端（ASR/STT 网关）
│   ├── vox_tts_server.py / tts_vox.py ─ VoxCPM2 本地 TTS（optimize 加载，vox_tts_server 进程）
│   └── gpu_pipeline.py ──────────────── GPU 推理管线（tensorrt 可选）
│
├── ⑨ 监控 / 可观测性层（Observability）
│   ├── monitor_server.py ────────────── sidecar 监控进程（/status 看板，聚合各 sidecar 健康度）
│   ├── monitor_client.py ───────────── 监控客户端（周期 push_status，掉线 no-op）
│   ├── telemetry_server.py / telemetry.py ─ 遥测服务
│   └── web_plugin.py Web 控制台 ─────── 侧栏「插件/服务」聚合平台/功能插件 + sidecar 运行态 + 功能开关
│
├── ⑩ 工具与基础设施层（Tools & Infra）
│   ├── service_host.py ──────────────── 通用本地 RPC 骨架（通用设施，非记忆专属；
│   │                                       依赖方向：业务 sidecar → service_host）
│   ├── web_tools.py ─────────────────── 联网搜索 + 网页抓取（DeepSeek web_search）
│   ├── time_context.py ──────────────── 统一时间上下文（发消息时感知当前时间）
│   ├── emoji_store.py ───────────────── 表情包存储（emojis/）
│   ├── codebuddy_cli.py ─────────────── CodeBuddy CLI 集成
│   ├── gpu_pipeline.py（见⑧）────────── GPU 推理管线
│   ├── message_bus.py（见②）────────── 消息契约/事件总线
│   └── 运维 / 迁移 / 基准脚本（独立运行，不被主流程 import）
│       ├── get_bili_cookie.py ───────── 获取 B 站登录 Cookie 并写入 config.py
│       ├── migrate_to_fact_store.py ─── user_profiles.json → fact_store(SQLite FTS5)
│       ├── migrate_to_time_indexed.py ─ JSONL 历史 → time_indexed_memory(SQLite)
│       ├── test_fact_store_performance.py / test_fts5_chinese.py
│       ├── test_time_indexed_performance.py
│       └── tensorrt_example.py ──────── TensorRT 推理占位示例（RTX 5060）
│
└── ⑪ 数据与运行资产（Data & Runtime）
    ├── data/ ────────────────────────── 记忆/历史/调试图/学习 jsonl
    ├── memory/ ──────────────────────── 记忆数据库（*.db）
    ├── caches/ ──────────────────────── 缓存与日志
    ├── models/ ──────────────────────── 模型权重（*.safetensors / *.pth）
    ├── voice_tmp/ ───────────────────── 语音临时文件（wav/pcm）
    ├── emojis/ ──────────────────────── 表情包素材
    ├── 音色试听/ ─────────────────────── TTS 音色试听样本
    ├── chat_history_backup_* / backup_* / _legacy / _mc_ref ─ 历史备份与参考副本
    ├── tests/ ───────────────────────── 测试套件（tests/run.py 入口，unittest 发现）
    ├── venv/ / venv_vox/ ────────────── Python 虚拟环境（主 / vox TTS 专用）
    ├── mc_mod/ ──────────────────────── Minecraft 模组 jar 资源
    ├── webui/ ───────────────────────── Web 前端（index.html / styles.css / app.js）
    ├── attic/ ───────────────────────── 归档区（零引用死代码，如 ring_buffer.py）
    └── requirements.txt / requirements_full.txt ─ 依赖清单
```

## 关键数据流（一句话）

- **注册表驱动注册**：`plugin_registry.SPECS`（唯一事实来源）→ 核心按 `enabled_specs()` 实例化插件 / launcher 按 sidecar 条目拉起进程 / Web 面板读清单展示完整状态（含未启用项）；启动时 `validate_registry()` 自检版本与依赖
- **收消息**：平台插件 `build_inbound()` → `InboundMessage` → `core.chat.handle_message()`（聊天大脑）→ `ReplyTarget.reply/reply_voice`
- **主动发**：核心/大脑 → `message_bus.get_sender()` → 平台 `MessageSender.send_*`
- **学习闭环**：知识盲区 → `web_tools.search` → `knowledge_service.learn_async` 沉淀 → 下次 `recall` 复用（全平台共享）
- **大脑统一**：所有自主智能体注册进 `AgentCore.brains`，统一生命周期 / 状态 / `brain.event` 求助通道
- **Sidecar 解耦**：记忆（重负载 embedding）、监控（可观测性）独立进程，RPC 调用，掉线自动降级进程内

## 启动方式

```bash
start.bat            # 统一启动（Web 控制台 + 记忆/监控 sidecar）
start_web.bat        # 仅 Web 控制台（核心失败自动降级 --no-core）
main.py --qq         # 只开 QQ
main.py --console    # 只开终端（验证核心最快）
main.py --bili       # 只开 B站直播
main.py --web        # 作为 bot 的平台插件开 Web
run_agent.py --mc    # 控制台 + Minecraft 自主游戏
```
