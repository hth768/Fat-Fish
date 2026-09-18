# 静默异常改造清单（DEGRADE_AUDIT）

> 由 `tests/audit_silent_except.py` 生成，配合 `libs/qq_bot_runtime/quiet.py` 使用。

「静默异常」指 `except ...: pass / continue / ...` —— 异常被吞掉且**事后零痕迹**，出问题时无法定位。改造目标不是把它们改成抛错（多数是刻意的降级路径，抛错会打断正常流程），而是**可信地留痕**：刻意降级用 `quiet.degrade()` 计数（默认不打印，避免刷屏），真问题用 `quiet.attention()` 输出告警。

## 统计

| 范围 | 数量 |
|---|---|
| App 层（`bridge/`、`app.py`、`server.py`） | 1 |
| 引擎层（`libs/qq_bot_runtime/`） | 0 |
| 其它 | 1 |
| **合计** | **2** |

| 分桶 | 含义 | 数量 |
|---|---|---|
| A | 探测 / 默认值回落（大概率刻意） | 1 |
| B | 静默失败风险（含写/发/删等副作用） | 0 |
| C | 需人工判定 | 1 |

## 按文件分布（降序）

「已留痕」= 该文件里已写入的 `degrade()/attention()` 调用数（改造进度）。

| 文件 | A | B | C | 剩余 | 已留痕 |
|---|---|---|---|---|---|
| `plugins/greeting_demo/plugin.py` | 0 | 0 | 1 | 1 | 0 |
| `tests/audit_silent_except.py` | 1 | 0 | 0 | 1 | 7 |

本轮已完成留痕的文件：`bridge/builder_api.py`(34)、`libs/qq_bot_runtime/mc_bot_brain.py`(23)、`bridge/memory_api.py`(17)、`libs/qq_bot_runtime/realtime_voice.py`(15)、`libs/qq_bot_runtime/web_plugin.py`(11)、`tests/test_quiet_and_audit.py`(9)、`bridge/plugins_api.py`(7)、`libs/qq_bot_runtime/chat_service.py`(7)、`tests/audit_silent_except.py`(7)、`libs/qq_bot_runtime/bilibili_plugin.py`(6)、`libs/qq_bot_runtime/bili_streamer.py`(6)、`libs/qq_bot_runtime/quiet.py`(6)、`libs/qq_bot_runtime/launcher_core/runtime.py`(6)、`bridge/sidecar_runner.py`(5)、`libs/qq_bot_runtime/get_bili_cookie.py`(5)、`libs/qq_bot_runtime/telemetry.py`(5)、`libs/qq_bot_runtime/tts_vox.py`(5)、`bridge/appearance_api.py`(4)、`bridge/app_window.py`(4)、`libs/qq_bot_runtime/proactive_speaker.py`(4)、`libs/qq_bot_runtime/qq_plugin.py`(4)、`bridge/pkg_manager.py`(3)、`libs/qq_bot_runtime/agent_core.py`(3)、`libs/qq_bot_runtime/ai_provider.py`(3)、`libs/qq_bot_runtime/codebuddy_cli.py`(3)、`libs/qq_bot_runtime/identity.py`(3)、`libs/qq_bot_runtime/long_term_memory.py`(3)、`libs/qq_bot_runtime/main.py`(3)、`libs/qq_bot_runtime/service_host.py`(3)、`libs/qq_bot_runtime/vision_capture.py`(3)、`libs/qq_bot_runtime/voice_client.py`(3)、`bridge/loop.py`(2)、`libs/qq_bot_runtime/agent_manager.py`(2)、`libs/qq_bot_runtime/ai_profile.py`(2)、`libs/qq_bot_runtime/bili_dm.py`(2)、`libs/qq_bot_runtime/bili_learn_scheduler.py`(2)、`libs/qq_bot_runtime/emotion.py`(2)、`libs/qq_bot_runtime/file_lock.py`(2)、`libs/qq_bot_runtime/memory.py`(2)、`libs/qq_bot_runtime/telemetry_server.py`(2)、`libs/qq_bot_runtime/video_processor.py`(2)、`libs/qq_bot_runtime/voice_room.py`(2)、`libs/qq_bot_runtime/vox_tts_server.py`(2)、`libs/qq_bot_runtime/launcher_core/bootstrap.py`(2)、`bridge/core_bridge.py`(1)、`libs/qq_bot_runtime/bili_captions.py`(1)、`libs/qq_bot_runtime/bili_learn.py`(1)、`libs/qq_bot_runtime/dxcam_capture.py`(1)、`libs/qq_bot_runtime/fact_store.py`(1)、`libs/qq_bot_runtime/intent_router.py`(1)、`libs/qq_bot_runtime/mc_bot_run.py`(1)、`libs/qq_bot_runtime/memory_client.py`(1)、`libs/qq_bot_runtime/monitor_server.py`(1)、`libs/qq_bot_runtime/run_agent.py`(1)、`libs/qq_bot_runtime/test_time_indexed_performance.py`(1)、`libs/qq_bot_runtime/time_indexed_memory.py`(1)、`libs/qq_bot_runtime/vector_memory.py`(1)、`libs/qq_bot_runtime/_dbg_mem_tmp.py`(1)

## 逐条清单

### A 探测 / 回落类（可用 degrade 留痕）（1 条）

| 序号 | 位置 | 函数 | except | 体 | try 首句 |
|---|---|---|---|---|---|
| 1 | `tests/audit_silent_except.py:282` | `count_quiet_calls` | `Exception` | continue | `with open(p, "r", encoding="utf-8", errors="replace") as f` |

### C 需人工判定（1 条）

| 序号 | 位置 | 函数 | except | 体 | try 首句 |
|---|---|---|---|---|---|
| 1 | `plugins/greeting_demo/plugin.py:90` | `GreetingDemoPlugin._loop` | `asyncio.TimeoutError` | pass | `await asyncio.wait_for(self._wake.wait(),` |

---

重新生成：`python tests/audit_silent_except.py --md DEGRADE_AUDIT.md --json data/degrade_inventory.json`
