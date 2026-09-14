# PVZ 大脑：让智能体自己玩《植物大战僵尸》

## Context（为什么做 / 目标）

主人希望这个 QQ 智能体（肥鱼娘）具备玩 PvZ 的能力。机器上已装原版 PvZ 1：`F:\新建文件夹\PlantsVsZombies.exe`（PopCap 原版，主窗口约 800x600）。

项目已有两块可复用地基：**pc 大脑**（`pc_agent.py`/`pc_control.py`：截屏→视觉→键鼠执行，带急停/节流护栏）和**大脑注册表**（`brain_base.py` + `agent_core.py`：实现 `AgentBrain` + 一行注册即接入）。

但 PvZ 是**实时对抗**，pc 大脑"一次任务 ≤12 步、每步 5~10 秒 LLM 循环"节奏太慢（收阳光/临场救火来不及）。因此新建专用**自主循环大脑 pvz**，采用两层感知：

- **快层（本地 cv2，零成本）**：~1Hz 截游戏窗口，黄色斑块检测阳光 → 直接点击收取（误点向日葵无害：游戏本身拒绝在已有植物处种植）；
- **慢层（LLM 结构化读盘 + 确定性策略引擎）**：每 ~5s 一张游戏截图给视觉模型（复用 `GLMClient.describe_image`，要求 JSON 输出），解析出界面状态/种子槽与冷却/阳光数/9x5 草坪网格/僵尸行列表，由**策略引擎（纯代码规则）**产出动作点击执行。

策略知识接入自主学习闭环：开局查 `pvz_tips` + `core.knowledge.recall`，查不到联网搜攻略沉淀；输一局自动复盘总结写 `add_tip(source="learned")` 并 QQ 汇报。过程播报（开局/胜利/失败/救火）走 `brain.event` + notify 私聊。

## 实现步骤

**0. 先探测**：启动 PvZ，用 pygetwindow/win32 实测窗口标题/类名/客户区几何，标定网格坐标（初值：格中心 x=80+80·col, y=140+100·row；种子栏区域 (0,0)-(800,60)），存 2~3 张参照截图。

**1. `pvz_vision.py` 感知层（纯 cv2）**：`find_game_window()`、`capture_game()`（mss 截客户区，参照 `vision_capture.py:347` 做法但取精确客户区）、`find_suns()`（HSV 黄色+连通域）、`img→jpeg base64`、`annotate()`（调试标注图存 data/）、网格坐标→屏幕坐标换算（win32 `ClientToScreen`，不依赖前台）。

**2. `pvz_tips.py` + `pvz_tips.json`**：完全照 `mc_tips.py` 惯例（`{id,content,source,time}` + `add_tip/match_tips/...`）。攻略 `source="user"`（含联网学的），复盘 `source="learned"`。决策 prompt 注入 `match_tips("pvz")`。

**3. `pvz_strategy.py` 策略引擎（纯函数）**：输入 GameState，输出动作列表——向日葵铺到 8~10 株→豌豆铺两列→坚果挡位→樱桃/辣椒救火→补被吃植物；`PVZ_LAYOUT` 表驱动；含读盘 JSON 容错解析（字段缺失即降级跳过）。

**4. `pvz_agent.py` 自主循环（仿 pc_agent 结构）**：`get_agent()` 单例、`start_game(requester, notify)`/`request_stop()` 互斥；asyncio 循环 tick=2s：每 tick 截图收阳光（点击走 `pc_control` 护栏），每 5s 读盘→策略→执行；生命周期识别（菜单自动进冒险模式→选卡界面自动点卡→战斗→胜负结算）；败局复盘写 tips；连续读盘失败停局求助（brain.event help）；急停复用 `pc_control.request_emergency_stop` + 总时长上限。

**5. 注册与命令**：`agent_core.py` 加 `PvzBrain`（kind=autonomous，按 `ENABLE_PVZ_BRAIN` 注册）；`chat_service.py` 前缀分发区加 `/pvz` → `_handle_pvz_command`（照 `_handle_pc_command` chat_service.py:908 的判权/开关结构）：`/pvz玩 [关卡]`、`/pvz停`、`/pvz状态`、`/pvz看盘`（读盘调试回话）、裸命令帮助。intent_router 不动。

**6. `config.py`**：`ENABLE_PVZ_BRAIN`、`PVZ_EXE_PATH`、`PVZ_WINDOW_TITLE`、`PVZ_TICK_SECONDS=2.0`、`PVZ_DECIDE_INTERVAL=5.0`、`PVZ_MAX_GAME_SECONDS=1500`、阳光面积过滤、`PVZ_AUTO_RESTART`。

**7. 学习闭环**：开局 `knowledge.recall` 为空则后台 `search_web`+`learn_async`；`self_knowledge.py` 注册 `kind="pvz"` 统一查看/学习/删除。

**8. 文档**：`ARCHITECTURE.md` 大脑表加 `pvz` 行 + 一节简介。

## 验证
1. `/pvz看盘` + `data/pvz_debug_*.png` 核对网格对齐/僵尸位置/阳光数 → 校常量；
2. 手造 GameState 单测策略引擎（阳光够先向日葵、僵尸进 6 列出樱桃等）；
3. 局中 1 分钟看阳光数持续上涨（快层证据）；
4. 端到端：`/pvz玩 第一关` 她自主开局、布阵、打赢、QQ 汇报；故意送一波看失败复盘；`/pvz停` 急停；
5. 回归：`/大脑` 含 pvz；开关关闭行为不变。

## 风险与边界
- 读盘是概率性的：菜单类动作需连续 2 次同结论才执行；战斗字段缺失即跳过；
- 操作期间占用鼠标（同 /电脑做，开局提醒）；
- v1 范围：冒险模式前期（1-x~2-x），目标"能玩、能赢普通关、能学习"；矿车/泳池/生存模式不做；
- 独占全屏拿不到客户区的回退：提示以窗口模式运行（原版默认窗口化，风险低）。