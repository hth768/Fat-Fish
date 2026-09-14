# 架构说明：智能体为核心，平台即插件

> 2026-09 重构：原 `bot.py`（2300 行单文件 QQ 机器人）已拆分为
> 「智能体核心 + 插件系统」。QQ 聊天降级为一个平台插件，
> 后续可接入 B 站、直播、Telegram 等任何平台，核心零改动。
> 同月再加「核心大脑」层：`AgentCore.brains` 大脑注册表统一管理所有智能体大脑
> （聊天 / 模组 MC / 原版 MC Bot）的生命周期、状态与求助事件通道，未来新 agent
> 实现 `AgentBrain` + 一行注册即接入（见下「核心大脑（大脑注册表）」）。

## 总览

```
                         ┌──────────────────────────────────┐
                         │            AgentCore             │
                         │   chat: ChatService 聊天大脑      │
                         │   plugins: PluginManager          │
                         │   bus: AgentEventBus 事件总线      │
                         └────────┬────────────────┬────────┘
                  InboundMessage  │                │  MessageSender / 事件
                  （平台→核心）    │                │  （核心→平台主动推送）
        ┌─────────────────────────▼──┐    ┌────────▼──────────────────┐
        │ 平台插件 PlatformPlugin      │    │ 功能插件 FeaturePlugin      │
        │  qq_plugin.QQPlugin         │    │  余额监控                   │
        │  console_plugin.ConsolePlugin│   │  主动说话                   │
        │  bilibili_plugin（直播平台）  │    │  (未来) 定时任务 / 定时播报  │
        └─────────────────────────────┘    └───────────────────────────┘
```

## 文件职责

| 文件 | 角色 | 说明 |
|---|---|---|
| `main.py` | 启动入口 | 装配核心 + 按配置/参数加载平台插件 |
| `agent_core.py` | **核心** | 生命周期、插件注册、事件转发、**大脑注册与生命周期**（`brains` 注册表：聊天/模组 MC/原版 MC Bot 三个内建大脑，见「核心大脑（大脑注册表）」）；不依赖任何平台 |
| `brain_base.py` | **核心大脑框架** | `AgentBrain` 大脑接口 / `BrainManager` 注册表 / `brain.event` 统一大脑事件（help/notice/state）；内建大脑包装与注册在 agent_core.py |
| `chat_service.py` | **聊天大脑** | 命令分发、记忆、图片/语音/视频、意图识别、MC 注入、DeepSeek 调用；平台无关 |
| `memory_server.py` | **sidecar（RPC）** | 记忆/重负载独立进程：基于 `service_host` 通用 RPC 骨架，用本地 HTTP 承载 `vector_memory` 等阻塞式重负载（embedding 模型加载、向量检索），仅监听 127.0.0.1 |
| `memory_client.py` | **sidecar 客户端** | `chat_service` 等通过 `from memory_client import vector_memory` 透明调用；开启 `ENABLE_MEMORY_SERVER` 走 RPC，否则/掉线时自动降级为进程内调用，零破坏 |
| `service_host.py` | **sidecar 基础设施（通用，非记忆专属）** | 通用本地 RPC 服务骨架（HTTP + asyncio 派发，统一 sync/async）：**不 import 任何业务模块**，各服务进程只声明托管模块与超时；当前使用者为 `memory_server`（托管 `vector_memory`），knowledge / agent 若独立进程可直接复用。对齐 N.E.K.O 的 main/agent/memory 多服务拆分 |
| `ai_provider.py` | **多供应商抽象** | 统一 LLM 入口（对齐 N.E.K.O 的 14+ providers）：`capability`(chat/reasoning/vision/tools) → `AI_CAPABILITY_ROUTING` 有序供应商列表，调用时故障转移；`get_llm().chat()` 统一文本/视觉/工具/thinking |
| `monitor_server.py` | **sidecar（监控）** | 可观测性进程（对齐 N.E.K.O 的 `monitor` / 本地遥测）：独立进程暴露 `/status` 与简易看板 `/`，接收 bot 周期推送的 `core.status()` 快照并聚合各 sidecar 健康度，不依赖主循环 |
| `monitor_client.py` | **监控客户端** | `main` 周期经 `push_status()` 推送状态快照；`ENABLE_MONITOR` 关闭或掉线时静默 no-op，零副作用 |
| `plugin_base.py` | 插件系统 | `Plugin` / `PlatformPlugin` / `FeaturePlugin` / `PluginManager` |
| `message_bus.py` | 消息契约 | `InboundMessage`（收）/ `ReplyTarget`（回）/ `MessageSender`（主动发）/ 事件总线 |
| `qq_plugin.py` | QQ 平台插件 | NapCat WebSocket、OneBot 协议转换、QQ 表情码、图片/视频抓取、SILK 语音解码、私聊聚合 |
| `console_plugin.py` | 控制台插件 | 终端聊天，新平台接入的最小参考实现 |
| `bilibili_plugin.py` | **B 站直播平台插件** | 两种模式：streamer（她当主播，弹幕当耳朵，回应说进直播音频）/ viewer（弹幕机器人）；弹幕 WS 协议、触发词进核心、礼物事件、断线重连 |
| `bili_api.py` | B 站共享 API 层 | Cookie 会话（buvid/wbi 风控应对）、公开 GET/POST 封装、弹幕文本压缩（20 字上限） |
| `bili_captions.py` | **直播字幕服务** | 她说的话（弹幕回应/感谢/闲聊/点播）推为直播画面字幕：本地 HTTP 环形缓冲 + 自绘页面，直播姬「浏览器素材」指向即实时上屏 |
| `bili_streamer.py` | **B 站开播模块（她的"身体"）** | startLive 拿 RTMP 地址、ffmpeg 推流（画面 + 音频管道）、直播嗓音 speak()/play_wav()（TTS → 16k PCM → 实时节奏写管道） |
| `qq_adapter.py` | QQ 主动发送 | 实现 MessageSender（send_private/send_group），供核心模块主动推送 |
| `bot.py` | 兼容垫片 | `python bot.py` ≡ `python main.py --qq`（start.bat 无需改动） |
| `run_agent.py` | 独立入口 | 控制台模式 + 可选 `--mc` 启动自主游戏 |
| `mc_*.py` | MC 功能模块 | 自主代理/监控/导航/合成等（与聊天平台无关，被 chat_service 调用） |
| `knowledge_store.py` | **通用知识库** | 自主学习闭环的存储层：搜索学到知识沉淀为带日期/来源/关键词的条目，回答前查库复用 |
| `pc_agent.py` | **电脑操控大脑** | 任务制自主 GUI 智能体：截屏→视觉理解（GLM 视觉为主/Gemini 兜底）→DeepSeek 工具调用→键鼠执行；主人 `/电脑做` 下任务，一次一个，四层护栏（主人专属/步数上限/动作节流/急停） |
| `pc_control.py` | **电脑操控工具层** | pc_agent 的唯一执行通道：mss 截屏 + pyautogui 键鼠（DPI 感知，截图↔屏幕坐标换算）+ 窗口管理 + 受限 shell（高危命令黑名单）；急停双通道（/电脑停、鼠标甩屏幕左上角） |
| `pvz_agent.py` | **PVZ 游戏大脑** | PvzAgent：实时对抗自主循环——快层本地 cv2 收阳光 + 慢层 LLM 读盘出 JSON + pvz_strategy 规则引擎执行；输赢自动复盘写 tips（`ENABLE_PVZ_BRAIN`） |
| `pvz_vision.py` | **PVZ 感知层** | 窗口定位/前台保持、客户区截图、黄色斑块阳光检测、网格坐标换算（实测标定 1.2.0.1073；该游戏失焦暂停+只认真鼠标点击） |
| `pvz_strategy.py` | **PVZ 策略引擎** | 纯函数规则：读盘 JSON 容错解析 → 救火>经济>火力>防线 的确定性动作（pvz_tips 注入读盘 prompt） |
| `pvz_tips.py` | **PVZ 技巧库** | 攻略(user/联网)+每局复盘(learned) 沉淀（pvz_tips.json，结构对齐 mc_tips），self_knowledge kind=`pvz` 统一查看/教/删 |
| `identity.py` | **身份注册表** | QQ↔MC 游戏名双向确认绑定；canonical 记忆键解析、跨场景身份提示、历史合并触发（identity_bindings.json） |
| `file_lock.py` | 跨进程文件锁 | msvcrt 字节区锁（进程内可重入），保护多进程共写的档案/笔记/绑定/历史文件 |
| `emotion.py` | **AI 情绪模块** | 聊天大脑的心情状态（emotion_data.json）：全局一份心情值/主导情绪/事件流水（原因归人），随时间回落；对话前注入语气指引，支持自然语言问心情 |
| `self_knowledge.py` | 自我知识管理 | 统一查看/学习/删除接口：游戏知识（tips/landmarks 等）+ 百科知识（facts → knowledge_store） |
| `web_tools.py` | 联网工具 | DeepSeek web_search 实时搜索 + 网页抓取解析 |

## 数据流

**收消息（平台 → 核心）**

```
QQ 消息事件
  → QQPlugin.build_inbound()      # OneBot 数组 → InboundMessage（语音在平台层解码为 wav）
  → QQReplyTarget(msg)            # 每条消息一个回复上下文
  → AgentCore.chat.handle_message(msg, reply)   # 聊天大脑
  → reply.reply(...) / reply.reply_voice(...)   # 大脑通过上下文回话（平台决定呈现细节）
```

**主动发消息（核心 → 平台）**

```
主动说话 / 余额提醒 / 视觉异常
  → message_bus.get_sender()      # 当前活跃平台注册的 MessageSender
  → sender.send_private(...)      # 平台各自实现
```

**自主学习闭环（盲区 → 搜索 → 记录 → 复用，全平台共享）**

```
遇到知识盲区（should_web_search 判定需要联网）
  → web_tools.search_web()                    # 联网搜索
  → knowledge_service.learn_async() 后台任务  # 不阻塞回复
      → learn()：一次廉价 LLM 调用提炼「值得长期记住的通用知识」
      → knowledge_store.add_entry()           # 主题/事实/关键词/来源/学习日期入库
                                              # 同主题合并、事实全库去重、总量上限淘汰最旧
下次遇到相关问题
  → knowledge_service.recall()                # 回答前先查知识库
  → 命中 → 注入记录（带学习日期，提示注意时效），跳过联网搜索，省一次搜索费
  → 未命中 → 照常 should_web_search → 搜索 → 再沉淀（闭环）
```

- **任何平台插件自动继承整个闭环**：闭环长在 chat_service.handle_message() 里，
  平台插件只负责把消息转成 InboundMessage，B 站/直播等新平台零改动接入；
  知识按主题全局存储（不分平台/用户），QQ 上学会的，B 站插件同样能用。
- **插件编程访问**：插件持有 core 引用，直接用 `self.core.knowledge`：
  `learn_async(问题, 文本)`（从搜索结果/网页/字幕学知识，后台执行）、
  `recall(问题)`（查库拿注入上下文）、`record(主题, 事实, 关键词)`（直接记录）、
  `search / view / remove`。
- 用户消息含「最新/现在/今天」等词时不拦截，照常联网（知识可能过时）；
- 用户随时可用 `/搜索 问题` 强制重新联网查证；
- `/查看知识 facts` / 知识类型 `facts` 可查看与删除沉淀的知识（self_knowledge.py）。

config.py 相关开关：

```python
ENABLE_KNOWLEDGE_LEARN = True   # 搜索后自动沉淀知识
ENABLE_KNOWLEDGE_RECALL = True  # 回答前先查知识库，命中跳过联网
KNOWLEDGE_MAX_ENTRIES = 200     # 知识库主题数上限
```

## 情绪模块（聊天大脑的心情状态）

> 让肥鱼娘"有持续的心情"：被夸会开心、被骂会委屈/生气、被冷落会低落，
> 气不记一辈子——心情随时间自然回落。感知不新增 LLM 调用，零成本。
> 心情是**全局一份**（AI 只有一个心情，不分用户），但每条心情变化的
> **原因都归到具体的人**（事件记录引起者，展示/注入时解析成称呼）。

- **状态**：emotion.py 维护全局一份心情档案，落盘 `emotion_data.json`
  （重启不丢；旧版按用户分隔的格式自动合并迁移）：心情值 -10~10（平滑底色，
  指数回落到 0）、此刻主导情绪标签（开心/生气/委屈/低落/被暖到…，按
  `EMOTION_LABEL_MINUTES`×强度持续后消散，并记录"谁引起的"）、最近情绪事件
  流水（8 条，带时间/引起者/原因/影响值）。
- **感知**：聊天每轮已有的「记忆提取」调用顺带做（extract_memory include_mood=True，
  第三类【心情】区，标签限 `emotion.MOOD_LABELS` 枚举），解析后以消息发送者为
  引起者写入档案；情绪影响从**下一轮**回复开始体现（不拖慢当前回复）。
- **动态**：事件影响单次限幅 ±3.5、累积夹紧 ±10；心情值按 `EMOTION_DECAY_HOURS`
  （默认 2h）半衰期指数回落；长时间不互动自然回平静，平静时提示不注入（省 token）。
- **表达**：对话前 `build_mood_hint(user_id)` 注入 system——"此刻心情 + 起因（谁干的）+
  该情绪下的语气指引"；心情是别人惹的会附"别迁怒眼前的人"；傲娇人设允许炸毛/撒娇/
  低落，附底线：不伤人、不摆烂正经问题、对方示好给台阶下。
- **查询（免指令）**：自然语言问（"你心情怎么样""你还在生气吗"）由 `is_mood_query()`
  识别后直接回 `describe_mood()`——口语化人设作答（不暴露心情值/事件流水等文件数据，
  但原因同样归到具体的人）；说用户自己心情时不抢答。`/心情` 命令保留兼容，
  `/心情 重置` 清零和好。结构化心情数据只经 `build_mood_hint()` 给模型。
- **影响面**：目前只作用于聊天大脑主对话路径；主动说话（proactive_speaker）、实时语音
  等其它路径可零侵入复用 `emotion.build_mood_hint()`（不注入 = 现有行为）。

config.py 相关开关：

```python
EMOTION_ENABLED = True        # 情绪模块总开关
EMOTION_DECAY_HOURS = 2.0     # 心情底色回落半衰期（小时）
EMOTION_LABEL_MINUTES = 40    # 主导情绪标签基础持续时间（分钟，×强度 1~3）
```

## 核心大脑（大脑注册表）

> 大脑 = 会自己思考/回应的智能体单元。所有大脑注册进 `AgentCore.brains` 后，
> 自动获得统一生命周期、统一状态查询（`/大脑` 命令、`core.status()`）与统一事件通道。
> 各大脑的会话上下文保持私有，共享的是核心服务：记忆、知识、事件与状态。

| name | title | 形态 | 驱动 / 生命周期 |
|---|---|---|---|
| `chat` | 聊天大脑 | 消息式 | ChatService + 平台插件；随核心常开 |
| `mc_mod` | 模组世界 MC 大脑 | 自主循环 | MCAgent 线程；`/mc自动` 或 `run_agent.py --mc` 按需启动，核心关闭时统一停止 |
| `mc_bot` | 原版世界 MC Bot 大脑 | 外部进程 | `python mc_bot_run.py` 独立驱动（mineflayer 桥）；核心只注册状态通道，不接管进程 |
| `pc` | 电脑操控大脑 | 自主任务 | PcAgent；无常驻循环，主人 `/电脑做 <任务>` 下发，一次一个，完成/急停即结束（`ENABLE_PC_CONTROL`） |
| `pvz` | PVZ 游戏大脑 | 自主循环 | PvzAgent；主人 `/pvz玩` 开一局（一次一局），胜/负/急停即结束（`ENABLE_PVZ_BRAIN`） |

```python
core.brains.all()                        # 全部大脑
core.brains.get("mc_mod").start()        # 按需启动某个大脑（幂等）
core.brains.brains(kind="autonomous")    # 按形态过滤：chat / autonomous / external
core.status()["brains"]                  # 统一状态汇总（/大脑 命令同源）
```

- 随核心启动：`auto_start_on_core=True` 的大脑（当前只有 `chat`）在 `core.start()` 自动拉起，
  其余注册待命；`core.shutdown()` 统一停止全部大脑（含手动启动的）。
- 统一大脑事件 `brain.event`：payload `{source: 大脑名, kind, text, ts}`，kind ∈
  `help`（求助）/ `notice`（播报）/ `state`（状态变化）；任意平台/功能插件可
  `core.bus.on("brain.event", ...)` 订阅呈现。
- **求助兜底通道**：核心轮询模组 MC 大脑的求助 → ① 发 `brain.event`（help）；
  ② 主动说话调度器（ENABLE_PROACTIVE_SPEAKER）**未**启动时，核心直接用已注册的
  `MessageSender` 私聊主人（PROACTIVE_PRIVATE_USER_ID，带频控）；调度器启动时由其转发，不重复。
- `mc_bot` 大脑在独立进程，其掉线/死亡等主动汇报仍直连 SnowLuma（QQ_HTTP_API）——
  跨进程无法走内存事件总线，进程拓扑与通知方式保持不变（后续可升级为核心受管子进程 + HTTP 回调）。

## 人物身份绑定（跨平台记忆打通）

> 记忆/档案/笔记按 user_id 键隔离，QQ 号与 MC 游戏名原本是两套互不知晓的
> 命名空间（`chat_history/<qq号>.jsonl` 与 `<游戏名>.jsonl` 并存）。身份注册表
> （`identity.py` → `identity_bindings.json`）把经双向确认的 QQ 号与 MC 游戏名
> 归为同一个人，记忆键统一到该 QQ 号（canonical），两侧记忆从此互通：
> 游戏里学到的档案/笔记与 QQ 侧同源，游戏对白带 `mc` 标签进入该人的全文历史。

```
QQ 侧 /绑定 mc:X ──▶ pending(等游戏确认) ──▶ 游戏里 X 回「确认绑定」──▶ 生效
游戏里 X 说「QQ号Y是我」──▶ pending(等 QQ 确认) ──▶ QQ 侧 Y 回「同意绑定」──▶ 生效
主人预置（config.IDENTITY_OWNER_MC_NAMES）开机即 active；主人可 /绑定批准 直接激活
```

- 生效后：游戏侧该玩家说话 → 注入其 QQ 档案/笔记/近况/游戏史（替换旧版
  硬编码 `MC_OWNER_GAME_NAMES`，已迁移到 config + 注册表）；QQ 侧该用户说话 →
  注入「TA 在游戏里叫 XX」身份提示 + 游戏实时动态（live hint 放宽到绑定用户）。
- 历史文件合并：绑定激活时把 `chat_history/<游戏名>.jsonl` 一次性并入
  `<QQ号>.jsonl`（行改写 user_id，原文件改名 `.merged`，幂等）。
- 命令：`/绑定 mc:名字`（本人发起）、`/绑定列表`、`/绑定总览`（主人）、
  `/绑定批准 mc:名字`（主人）、`/解除绑定 mc:名字`（本人/主人）；纯文本
  「同意绑定」/「不是」完成 QQ 侧确认。
- 游戏侧规则（LLM 之前处理，见 `mc_bot_brain._handle_chat_rule`）：
  「绑定QQ <号>」/「QQ<号>是我」发起声明、「确认绑定」/「不是」回应确认、
  「记住:xxx」直接写入该人重要笔记（与 QQ `/记住` 同源）。
- 并发：`identity_bindings.json`/`user_profiles.json`/`important_notes.json`
  由核心进程与 MC bot 进程双写，load-modify-save 全部走 `file_lock.py`
  （Windows msvcrt 字节区锁，进程内可重入）；全文历史按文件追加 + 锁，跨进程安全。
- 开关 `ENABLE_IDENTITY_LINK`；关闭时身份层原样返回（记忆恢复按名隔离），
  主人游戏名按 config 列表兜底旧逻辑。解除绑定只停止融合，历史不拆分。

## 电脑操控大脑（pc_agent / pc_control）

> 主人专属：在 QQ 里发 `/电脑做 <任务描述>`，肥鱼娘就自己看屏幕、动鼠标键盘替你操作这台电脑。
> 与 mc_mod 的无尽生存循环不同，pc 是**任务制**：一次一个任务，完成/急停即结束。

```
/电脑做 打开记事本写一句话
  → PcAgent.start_task()（互斥：已有任务先拒绝）
  → 循环（≤ PC_AGENT_MAX_STEPS 轮）：
      截屏(pc_control.take_screenshot, 长边缩到 PC_SCREEN_MAX_SIZE)
        → 看图出观察（GLM/DeepSeek 视觉为主，Gemini 兜底；要求给出元素像素坐标）
        → DeepSeek chat_with_tools 选动作（截图像素坐标 → map_xy 换算真实屏幕坐标）
        → pc_control 执行：click/type/press/open_app/list_windows/focus/run_command…
      每步对照截图确认上一步生效；task_done(摘要) 结束
  → 汇报发起人（notify 回调 → 私聊兜底）+ brain.event 总线
```

- **命令**：`/电脑做 任务`、`/电脑状态`（进度）、`/电脑截图 [问题]`（只看不做）、
  `/电脑停`（急停，也可把鼠标甩到屏幕左上角触发 pyautogui FAILSAFE）。
- **四层护栏**：①主人专属（`identity.is_owner_qq`，控制台平台放行）；②步数/每轮动作数
  上限；③动作节流（`PC_ACTION_DELAY`）+ shell 高危命令黑名单（shutdown/format/递归删除/
  reg delete/netsh 等）；④急停双通道（命令 / FAILSAFE），执行过的 shell 命令原样写进任务汇报。
- **安全人设**（system prompt）：敏感操作（删文件/发消息/支付）非任务明确要求不做；
  密码/验证码绝不代输，卡住就 task_done 汇报卡点；默认不碰 QQ/微信等聊天窗口。
- **坐标体系**：LLM 在"截图像素坐标系"里给坐标（原点左上），`pc_control.map_xy`
  按截图/屏幕比例换算并夹紧到屏内；进程启动即设 DPI 感知，截图与键鼠坐标一致。

config.py 相关开关：

```python
ENABLE_PC_CONTROL = True    # 电脑操控大脑总开关
PC_AGENT_MAX_STEPS = 12     # 每任务最多「截屏→决策」轮数
PC_AGENT_MAX_TOOL_CALLS = 8 # 每轮最多动作数
PC_ACTION_DELAY = 0.5       # 键鼠动作最小间隔（秒）
PC_ALLOW_SHELL = True       # run_command 开关（黑名单始终生效）
```

## PVZ 大脑（pvz_agent / pvz_vision / pvz_strategy / pvz_tips）

> 主人专属：QQ 发 `/pvz玩 [关卡]`，肥鱼娘自己看屏幕玩《植物大战僵尸》。
> 与 pc（任务制、一次一步）不同，这是**实时对抗自主循环**：慢层负责决策、快层负责收钱。

```
/pvz玩 冒险模式1-1
  → PvzAgent.start_game()（互斥：一局一局玩）→ 后台循环（≤ PVZ_MAX_GAME_SECONDS）：
      快层（每 tick ~2s，零成本本地 cv2）：截客户区 → 黄色斑块=阳光 → 真鼠标点收
      慢层（每 PVZ_DECIDE_INTERVAL ~5s）：截图 → 视觉模型输出局面 JSON
          {screen/sun/cards/plants(行,列)/zombies(行,x)/buttons}
        → pvz_strategy 规则引擎（纯函数）出动作：救火(樱桃/补射手) > 经济(向日葵) > 火力 > 防线
        → 执行：选卡点一下 + 格子点一下（实测两步点击有效）
  → 界面分流：菜单/选卡/胜负结算自动点按钮；win/lose 停局汇报（AUTO_RESTART 则续下一关）
  → 复盘：有真实战斗记录才让 LLM 总结一句经验写 pvz_tips(learned)；tips 少时联网搜攻略沉淀
  → 全程播报发起人 + brain.event 总线
```

- **命令**：`/pvz玩 [关卡]`、`/pvz状态`、`/pvz看盘`（读盘+存网格标注图 `data/pvz_debug_board.png`，标定用）、
  `/pvz教 一句话`（教 PvZ 技巧）、`/pvz停`（急停）。统一知识入口 kind=`pvz`。
- **实测约束（1.2.0.1073 中文版，探测记录）**：游戏**失焦即暂停** → 每个动作前 ensure_foreground
  抢前台（她玩的时候主人别动鼠标）；**PostMessage 合成点击不生效** → 只走 pyautogui 真实点击
  （复用 pc_control 急停/节流护栏）；客户区逻辑 640x480、物理 800x600（125% 缩放），
  游戏基准坐标(800x600)↔物理像素换算封装在 pvz_vision（PVZ_ROWS_Y/PVZ_COLS_X 等常量已按实测标定）。
- **护栏**：主人专属；单局时长上限；连续读盘失败停局求助（brain.event help）；
  动作走 pc_control 节流+FAILSAFE；AUTO_RESTART 时同一胜负界面连读 3 次仍不退才收手（防死循环）。
- **学习闭环**：pvz_tips（攻略 user + 复盘 learned）注入读盘 prompt；防幻觉——
  没打过的局（无植物/僵尸记录）绝不生成复盘经验。

config.py 相关开关：

```python
ENABLE_PVZ_BRAIN = True      # PVZ 大脑总开关
PVZ_EXE_PATH = r"..."        # 游戏路径（她找不到窗口时自己启动）
PVZ_TICK_SECONDS = 2.0       # 快层节奏
PVZ_DECIDE_INTERVAL = 5.0    # 慢层读盘周期
PVZ_MAX_GAME_SECONDS = 1800  # 单局时长上限
PVZ_AUTO_RESTART = False     # 胜负后自动续下一关
```

新增 agent 大脑三步走（新大脑接入核心零改动）：

```python
# 1. 新模块里实现接口（三种形态的差异见 brain_base.py docstring）
from brain_base import AgentBrain

class MyAgentBrain(AgentBrain):
    name = "my_agent"; title = "我的新智能体"; kind = "autonomous"
    description = "..."
    async def start(self): ...    # 想随核心自启则设 auto_start_on_core = True
    async def stop(self): ...
    def status(self): return {**super().status(), ...}   # /大脑 会展示

# 2. agent_core.register_builtin_brains() 里加一行（可按 config 开关注册）
m.register(MyAgentBrain(self))

# 3. 想主动联系主人：await brain_event(self.core, "my_agent", "help", "……")
```

## 启动方式

```bash
python main.py                # 按 config.py 的 ENABLE_QQ_PLUGIN / ENABLE_CONSOLE_PLUGIN / ENABLE_BILIBILI_PLUGIN
python main.py --qq           # 只开 QQ
python main.py --console      # 只开终端聊天（无需 NapCat，验证核心的最快方式）
python main.py --bili         # 只开 B 站直播平台（需先填 BILIBILI_ROOM_ID）
python main.py --qq --console # 多平台并存
python bot.py                 # 旧入口，等价 main.py --qq（向后兼容）
python run_agent.py --mc      # 控制台模式 + Minecraft 自主游戏
```

config.py 相关开关：

```python
ENABLE_QQ_PLUGIN = True       # QQ 平台插件
ENABLE_CONSOLE_PLUGIN = False # 终端聊天插件
ENABLE_BILIBILI_PLUGIN = True # B 站直播平台插件（还需 BILIBILI_ROOM_ID；
                              #  BILIBILI_MODE: streamer=当主播 / viewer=弹幕机器人）
```

## B 站直播（streamer 模式：她当主播）

> 最终形态：肥鱼娘自己开直播间。观众发弹幕 → 聊天大脑（记忆/人设/情绪全生效）
> → 语音回复写进直播音频——她在直播里"开口"回应观众；礼物口播感谢。

```
开播：BiliStreamer.start()（按 BILIBILI_PUSH_MODE 分流）
  hime 模式：不调开播 API、不起 ffmpeg。打开虚拟声卡（VB-CABLE）常驻播放流，
    她的声音实时写进 CABLE Input；画面/游戏原声/推流/开播全由官方直播姬负责
    （直播姬把 CABLE Output 当麦克风采集）。无需 SESSDATA Cookie。
  ffmpeg 模式：room_init 解析房间 →（设置标题/分区）→ startLive 拿 RTMPS 推流地址
    → ffmpeg 推流：画面（静态立绘 / 窗口捕获 / 全屏，-re 实时节流）
                + 音频（stdin 16k 单声道 PCM 管道，100ms 块实时节奏写，不说写静音）
耳朵：BilibiliPlugin 连自己房间弹幕 WS（复用 viewer 的协议层）
嘴  ：chat 大脑语音回复 → LiveReplyTarget.reply_voice(wav) → BiliStreamer.play_wav()
      （chat_service 按 capabilities.voice_only 恒走语音分支，VoxCPM/GLM 合成）
事件：礼物/上舰/开播 → _post_event() 合成消息进核心 → 口播回应（独立轻频控）
字幕：她说的话（bili_captions.py，BILIBILI_CAPTIONS=True 时随开播启动）——接口上
  chat 语音分支调 reply.caption()、文字兜底走 speak() 统一推，直播姬加「浏览器素材」
  指向 http://127.0.0.1:8768/ 即实时上屏（一条主字幕+两条历史，不受弹幕 20 字限制）。
MC 融合：她玩 MC 时，chat_service 对直播平台（voice_only）注入 _build_stream_mc_hint()
  ——mc_mod/mc_bot 谁在跑就带谁的实时状态（位置/血量/目标/最近动作），要求她以第一人称
  把"正在做的事/想法"自然融入回应；主播主动闲聊的提示词同样注入（游戏故事型话题）。
闲聊：BILIBILI_HOST_CHAT=True 时，观众沉默超过 BILIBILI_HOST_CHAT_IDLE（默认 180s）
  且距她上次开口超过 BILIBILI_HOST_CHAT_INTERVAL（默认 300s），她自己找话题开口
  （DeepSeek 生成两句话 → TTS → 直播音频；弹幕回复/礼物感谢/开场白都算「上次开口」，
  不抢话不冷场）。硬门槛：B 站侧 live_status==1（心跳循环每 30s 刷新，直播姬没点推流
  或已下播绝不自言自语；轮播 live_status==2 不算）+ 弹幕连接正常。
```

- **画面源**：`BILIBILI_VIDEO_SOURCE`——`window`（捕获游戏窗口，如她玩 MC 的客户端；
  标题模糊匹配，留空自动找 Minecraft 窗口，找不到会列出可见窗口）、`image`（静态立绘）、
  `desktop`（全屏）。开播时 `BILIBILI_STREAM_WITH_MC=True` 自动拉起 mc_mod 大脑
  （= /mc自动），游戏窗口即直播画面；ffmpeg 模式下游戏窗口需前台可见（gdigrab 抓
  屏幕区域），hime 模式画面归直播姬管、无此限制。
- **生命周期**：插件默认**待命启动**（BILIBILI_AUTO_START=False）——机器人启动只注册
  命令入口，不开声音通道、不连弹幕、不拉 MC；`/直播 开播` 才 `_boot()` 上线，
  `/直播 关播` 回待命（viewer 模式无开播概念，启动即连）。
- **控制命令**（QQ 端，主人）：`/直播 开播`、`/直播 关播`、`/直播 状态`、
  `/直播 说话 内容`（点播她开口）。
- **ffmpeg 二进制**：推流优先 `BILIBILI_FFMPEG_PATH` → imageio-ffmpeg 自带的稳定版
  → `FFMPEG_PATH`。极新的 git 构建（8.x 多线程调度器）对实时慢喂的管道音频会一直
  攒缓冲不出流（直到 EOF），无法直播，稳定发行版没这个问题。
- **频控**：`BILIBILI_REPLY_COOLDOWN`（AI 回复间隔）+ `BILIBILI_HOST_REPLY_ALL`
  （是否回应所有弹幕）+ 事件轻频控（礼物感谢不被弹幕冷却误杀）。
- **媒体能力**：直播音频算语音通道（voice/voice_only=True）；观众侧图片/视频不接收。

## 如何接入新平台（B 站已是现成参考实现）

`bilibili_plugin.py`（约 450 行）就是完整的范例，接新平台照它的骨架来：

1. 新建 `xxx_plugin.py`，继承 `PlatformPlugin`，实现三件套：
   `ReplyTarget.reply()`（呈现层）、`MessageSender`（主动发送）、
   `start()` 里把平台事件转成 `InboundMessage` 交 `core.chat.handle_message()`；
2. 在 `agent_core.AgentCore.register_builtin_plugins()` 加一行注册（按 config 开关）；
3. 在 config.py 加 `ENABLE_XXX_PLUGIN` 开关与凭据。

B 站实现里的关键点（接其它平台同样用得上）：

- **弹幕协议**：`room_init` 解析真实房间号 → `getDanmuInfo` 拿 token/服务器 →
  WebSocket `wss://{host}/sub`，16 字节二进制头（protover 2 = zlib），
  鉴权(op=7/8) + 30s 心跳(op=2/3，回包带人气值) + 消息(op=5, DANMU_MSG/SEND_GIFT...)。
- **风控应对**：匿名请求先经 `finger/spi` 领 buvid3/buvid4 Cookie；
  `getDanmuInfo` 被风控（code=-352）时自动降级：wbi 签名重试（nav 取 key）→
  旧版 `getConf` 接口兜底。
- **触发策略**：弹幕量大，`BILIBILI_TRIGGER_WORDS` 命中才进核心（`mentioned=True`），
  外加回复冷却与发送间隔双频控；弹幕 20 字上限，AI 长回复剔除表情码/动作描写后
  断在标点截断（`_fit_danmaku`）。
- **语音降级**：`capabilities.voice=False`，chat_service 会跳过语音意图判断与合成，
  直接文字回复（`ReplyTarget.reply_voice` 再抛异常兜底，触发 chat_service 的文字退回）。

要点（平台无关，所有新平台适用）：
- **用户标识自带命名空间**：跨平台用户 id 用 `bili:123` / `qq:456` 形式传入
  `InboundMessage.user_id`，记忆/档案自动按用户隔离（QQ 端沿用纯 QQ 号，兼容历史记忆）。
- **知识库开箱即用**：聊天层的「盲区→搜索→沉淀→复用」闭环在 handle_message 里，
  新平台无需任何接线；插件自己的功能代码要用知识库时，`self.core.knowledge`
  提供学习/查库/记录/删除全套接口（知识全局共享，不分平台）。
- **媒体用"引用"懒加载**：`InboundMessage.image_refs / video_ref` 携带平台不透明引用，
  只有确定要回复时 ChatService 才通过 `ReplyTarget.fetch_image/fetch_video` 取内容，
  避免为不回复的消息白白下载。
- **能力声明**：`capabilities` 里声明平台支持什么，不支持的能力 ChatService 会自然降级。

功能插件（FeaturePlugin）同理：实现 `start()/stop()`，在核心注册即可获得生命周期管理。

## 迁移对照（旧 bot.py → 新结构）

| 旧 bot.py 内容 | 去向 |
|---|---|
| `handle_message` 命令与聊天流水线 | `chat_service.ChatService` |
| WebSocket 服务 / handler 循环 / 私聊聚合 | `qq_plugin.QQPlugin` |
| `send_reply` / `parse_reply_segments` / `FACE_MAP` / 分句 | `qq_plugin`（呈现层） |
| `get_image_bytes` / `get_video_file` / `get_quoted_message` | `qq_plugin`（媒体抓取） |
| QQ 语音 SILK 解码 | `qq_plugin.decode_voice_wav`（平台层）；ASR/TTS 在 `chat_service` |
| `should_web_search` / `extract_memory` / `summarize_history` 等 | `chat_service` 模块函数 |
| `scheduler.start_background_tasks(ws)` | `agent_core` 的功能插件（BalanceMonitor / ProactiveSpeaker） |
| 视觉/余额提醒的裸 ws 发送 | 改走 `message_bus.get_sender()`（平台无关） |
| `memory_context` 反向依赖 bot | 改为 `chat_service.retrieve_relevant_history` |
