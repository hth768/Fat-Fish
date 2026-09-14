## 目标:给 AgentCore 建立"核心大脑"层(大脑注册表),统一接入现有三个大脑

三个现有大脑:聊天大脑 ChatService(消息式,核心内)、模组 MC 大脑 MCAgent(自主循环,核心进程内线程)、原版 MC Bot 大脑 BotAgent(自主循环,独立进程 mc_bot_run.py)。方案:新增 AgentBrain 接口 + BrainManager 注册表,三个大脑各包一层注册进核心;大脑事件/求助统一走 message_bus;进程拓扑不变、内部大文件(mc_agent/mc_bot_brain)只加壳不改;后续新 agent 实现接口 + 一行注册即接入。

### 1. 新增 `brain_base.py`(新文件,框架本体)
- `AgentBrain(plugin_base.Plugin 子类)` — 复用 core 引用/started/start/stop 生命周期契约;新增属性:`title`、`kind`("chat" 消息式 | "autonomous" 自主循环 | "external" 独立进程)、`description`。
- `BrainManager(plugin_base.PluginManager 子类)` — register/get/all/start_all/stop_all,新增 `brains()` 按 kind 过滤;状态汇总 `statuses()`。
- 统一事件约定与工具:`brain_event(core, source, kind, text)` → `bus.emit("brain.event", {source, kind, text, ts})`,kind ∈ help/notice/state。
- 模块 docstring:三类大脑形态说明 + 未来新大脑接入步骤示例(实现 AgentBrain + 注册一行 + 可选 config 门)。

### 2. `agent_core.py`(核心改造)
- `self._brains = None` 懒加载 `brains` property(首次访问自动 `register_builtin_brains()`,幂等)。
- 新增三个内建大脑包装类(定义在 agent_core.py,内部懒 import,不增加 import 期耦合):
  - `ChatBrain` — name="chat", kind="chat",聊天大脑;生命周期随核心,status 汇报当前已启动平台列表。
  - `McModBrain` — name="mc_mod", kind="autonomous",包装 `mc_agent.get_agent()`;start/stop 委托(保留现有 is_running 幂等/API 在线检查语义),status 取 `get_stats()`。
  - `McBotBrain` — name="mc_bot", kind="external",原版 MC 大脑(独立进程);status 读 `mc_bot_live.json`(新增 mc_live 公共函数);start() 不拉起子进程,返回提示"由 mc_bot_run.py 独立运行"(拓扑不变),注释说明后续可升级为受管子进程。
- 生命周期:core.start() 中 `brains.start_all()`(ChatBrain 幂等常开;McModBrain/McBotBrain 的 start 默认不自动拉起,维持现状行为);core.shutdown() 改为经 brains.stop_all() 停大脑(替换现在手工 get_agent().stop() 那段,保留 mc_watcher 停止)。
- **求助转发改造(核心兜底)**:现有 `_help_request_forwarder` 不再只发无订阅者的 `agent.help_request` 事件,改为:每批求助 → ① 统一 `bus.emit("brain.event", {source:"mc_mod", kind:"help", ...})`;② 若 `core.plugins.get("proactive_speaker")` 未启动 且 `message_bus.get_sender()` 存在 且配置了 PROACTIVE_PRIVATE_USER_ID → `sender.send_private(主人, 求助)` 兜底送达(带简单速率限制);proactive 开启时仍由它转发(现状),不重复。
- `AgentCore.status()` 增加 `"brains": [...]` 汇总。

### 3. `mc_live.py`(小改)
把私有 `_read_status()` 公开为 `read_live_status()`(保留原函数名兼容或直接改名,`build_live_hint` 逻辑不动),供 McBotBrain.status 使用。

### 4. `chat_service.py`(小改)
新增 `/大脑` 命令(约 15 行):列出核心内全部大脑 name/title/kind/running/状态摘要,作为"整合进核心"的可视化入口;命令处理内懒 import `agent_core.get_core()`,无循环导入风险。其余不动(现有 /mc状态 等命令继续直连 MCAgent,保持兼容)。

### 5. `run_agent.py`(小改)
`--mc` 启动路径改走 `core.brains.get("mc_mod").start()`(统一入口,行为等价,打印保持)。

### 6. `ARCHITECTURE.md`(文档)
- 总览图核心框内加 brains 注册表;文件职责表新增 brain_base.py 行、agent_core 职责补充"大脑注册与生命周期"。
- 新章节「核心大脑(大脑注册表)」:AgentBrain 契约、三个已注册大脑、`brain.event` 事件约定、求助兜底转发策略、未来新 agent 接入三步走(实现类 → register_builtin_brains 一行 → 可选 config 门)。

### 行为变化(透明声明)
1. proactive 关闭时,模组 MC 大脑求助现在会由核心兜底私聊主人(之前发总线事件但无订阅者,实际丢失);proactive 开启时不重复。
2. 其余保持现状:main.py 不自动拉起 MC 大脑(维持手动 /mc自动 或 --mc;若以后想"ENABLE_MC_AGENT=True 即自动启动",在 core.start 加一行即可,方案留了接口)。
3. mc_bot_brain 独立进程与 SnowLuma 直连通知不变(跨进程无法走内存总线,文档注明);三个大脑的会话上下文不合并,共享的是服务与事件。

### 验证
- py_compile 所有改动文件。
- 冒烟:`python -c` 实例化 AgentCore,检查 brains 注册表三个大脑、status() 汇总、mc_bot 状态文件缺失时容错。
- `python main.py --console` 短跑,`/大脑` 命令输出正常后退出(console 平台无 NapCat 依赖)。