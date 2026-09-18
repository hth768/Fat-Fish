# 静默异常改造清单（DEGRADE_AUDIT）

> 由 `tests/audit_silent_except.py` 生成，配合 `libs/qq_bot_runtime/quiet.py` 使用。

「静默异常」指 `except ...: pass / continue / ...` —— 异常被吞掉且**事后零痕迹**，出问题时无法定位。改造目标不是把它们改成抛错（多数是刻意的降级路径，抛错会打断正常流程），而是**可信地留痕**：刻意降级用 `quiet.degrade()` 计数（默认不打印，避免刷屏），真问题用 `quiet.attention()` 输出告警。

## 统计

| 范围 | 数量 |
|---|---|
| App 层（`bridge/`、`app.py`、`server.py`） | 21 |
| 引擎层（`libs/qq_bot_runtime/`） | 161 |
| 其它 | 4 |
| **合计** | **186** |

| 分桶 | 含义 | 数量 |
|---|---|---|
| A | 探测 / 默认值回落（大概率刻意） | 63 |
| B | 静默失败风险（含写/发/删等副作用） | 46 |
| C | 需人工判定 | 77 |

## 按文件分布（降序）

「已留痕」= 该文件里已写入的 `degrade()/attention()` 调用数（改造进度）。

| 文件 | A | B | C | 剩余 | 已留痕 |
|---|---|---|---|---|---|
| `libs/qq_bot_runtime/mc_bot_brain.py` | 12 | 0 | 11 | 23 | 0 |
| `libs/qq_bot_runtime/realtime_voice.py` | 3 | 9 | 3 | 15 | 0 |
| `libs/qq_bot_runtime/web_plugin.py` | 6 | 1 | 4 | 11 | 0 |
| `libs/qq_bot_runtime/chat_service.py` | 4 | 0 | 3 | 7 | 0 |
| `libs/qq_bot_runtime/bilibili_plugin.py` | 2 | 4 | 0 | 6 | 0 |
| `libs/qq_bot_runtime/bili_streamer.py` | 1 | 5 | 0 | 6 | 0 |
| `libs/qq_bot_runtime/launcher_core/runtime.py` | 0 | 4 | 2 | 6 | 0 |
| `bridge/plugins_api.py` | 0 | 0 | 5 | 5 | 2 |
| `libs/qq_bot_runtime/get_bili_cookie.py` | 3 | 1 | 1 | 5 | 0 |
| `libs/qq_bot_runtime/telemetry.py` | 3 | 1 | 1 | 5 | 0 |
| `libs/qq_bot_runtime/tts_vox.py` | 2 | 3 | 0 | 5 | 0 |
| `bridge/builder_api.py` | 0 | 0 | 4 | 4 | 30 |
| `libs/qq_bot_runtime/proactive_speaker.py` | 4 | 0 | 0 | 4 | 0 |
| `libs/qq_bot_runtime/qq_plugin.py` | 2 | 0 | 2 | 4 | 0 |
| `plugins/greeting_demo/plugin.py` | 0 | 1 | 3 | 4 | 0 |
| `libs/qq_bot_runtime/agent_core.py` | 0 | 2 | 1 | 3 | 0 |
| `libs/qq_bot_runtime/ai_provider.py` | 1 | 0 | 2 | 3 | 0 |
| `libs/qq_bot_runtime/codebuddy_cli.py` | 1 | 1 | 1 | 3 | 0 |
| `libs/qq_bot_runtime/identity.py` | 3 | 0 | 0 | 3 | 0 |
| `libs/qq_bot_runtime/long_term_memory.py` | 1 | 1 | 1 | 3 | 0 |
| `libs/qq_bot_runtime/main.py` | 0 | 1 | 2 | 3 | 0 |
| `libs/qq_bot_runtime/service_host.py` | 0 | 0 | 3 | 3 | 0 |
| `libs/qq_bot_runtime/vision_capture.py` | 0 | 0 | 3 | 3 | 0 |
| `libs/qq_bot_runtime/voice_client.py` | 1 | 2 | 0 | 3 | 0 |
| `app.py` | 2 | 0 | 0 | 2 | 0 |
| `bridge/app_window.py` | 0 | 0 | 2 | 2 | 2 |
| `bridge/pkg_manager.py` | 0 | 0 | 2 | 2 | 1 |
| `bridge/sidecar_runner.py` | 0 | 0 | 2 | 2 | 3 |
| `libs/qq_bot_runtime/agent_manager.py` | 2 | 0 | 0 | 2 | 0 |
| `libs/qq_bot_runtime/ai_profile.py` | 1 | 1 | 0 | 2 | 0 |
| `libs/qq_bot_runtime/bili_dm.py` | 1 | 0 | 1 | 2 | 0 |
| `libs/qq_bot_runtime/bili_learn_scheduler.py` | 0 | 0 | 2 | 2 | 0 |
| `libs/qq_bot_runtime/emotion.py` | 2 | 0 | 0 | 2 | 0 |
| `libs/qq_bot_runtime/file_lock.py` | 0 | 1 | 1 | 2 | 0 |
| `libs/qq_bot_runtime/memory.py` | 0 | 1 | 1 | 2 | 0 |
| `libs/qq_bot_runtime/telemetry_server.py` | 0 | 1 | 1 | 2 | 0 |
| `libs/qq_bot_runtime/video_processor.py` | 0 | 2 | 0 | 2 | 0 |
| `libs/qq_bot_runtime/voice_room.py` | 0 | 0 | 2 | 2 | 0 |
| `libs/qq_bot_runtime/vox_tts_server.py` | 2 | 0 | 0 | 2 | 0 |
| `libs/qq_bot_runtime/launcher_core/bootstrap.py` | 0 | 0 | 2 | 2 | 0 |
| `server.py` | 0 | 0 | 1 | 1 | 0 |
| `bridge/core_bridge.py` | 0 | 0 | 1 | 1 | 0 |
| `bridge/memory_api.py` | 0 | 0 | 1 | 1 | 16 |
| `libs/qq_bot_runtime/bili_captions.py` | 0 | 0 | 1 | 1 | 0 |
| `libs/qq_bot_runtime/bili_learn.py` | 0 | 1 | 0 | 1 | 0 |
| `libs/qq_bot_runtime/dxcam_capture.py` | 0 | 0 | 1 | 1 | 0 |
| `libs/qq_bot_runtime/fact_store.py` | 0 | 1 | 0 | 1 | 0 |
| `libs/qq_bot_runtime/intent_router.py` | 1 | 0 | 0 | 1 | 0 |
| `libs/qq_bot_runtime/mc_bot_run.py` | 0 | 0 | 1 | 1 | 0 |
| `libs/qq_bot_runtime/memory_client.py` | 0 | 0 | 1 | 1 | 0 |
| `libs/qq_bot_runtime/monitor_server.py` | 0 | 0 | 1 | 1 | 0 |
| `libs/qq_bot_runtime/run_agent.py` | 0 | 0 | 1 | 1 | 0 |
| `libs/qq_bot_runtime/test_time_indexed_performance.py` | 0 | 1 | 0 | 1 | 0 |
| `libs/qq_bot_runtime/time_indexed_memory.py` | 0 | 1 | 0 | 1 | 0 |
| `libs/qq_bot_runtime/vector_memory.py` | 1 | 0 | 0 | 1 | 0 |
| `libs/qq_bot_runtime/_dbg_mem_tmp.py` | 1 | 0 | 0 | 1 | 0 |
| `tests/audit_silent_except.py` | 1 | 0 | 0 | 1 | 7 |

本轮已完成留痕的文件：`bridge/builder_api.py`(30)、`bridge/memory_api.py`(16)、`tests/test_quiet_and_audit.py`(9)、`tests/audit_silent_except.py`(7)、`libs/qq_bot_runtime/quiet.py`(6)、`bridge/appearance_api.py`(4)、`bridge/sidecar_runner.py`(3)、`bridge/app_window.py`(2)、`bridge/loop.py`(2)、`bridge/plugins_api.py`(2)、`bridge/pkg_manager.py`(1)

## 逐条清单

### B 静默失败风险（优先人工判定）（46 条）

| 序号 | 位置 | 函数 | except | 体 | try 首句 |
|---|---|---|---|---|---|
| 1 | `libs/qq_bot_runtime/agent_core.py:95` | `ProactiveSpeakerPlugin.stop` | `Exception` | pass | `from proactive_speaker import get_speaker` |
| 2 | `libs/qq_bot_runtime/agent_core.py:298` | `AgentCore.shutdown` | `Exception` | pass | `from mc_watcher import mc_watcher` |
| 3 | `libs/qq_bot_runtime/ai_profile.py:50` | `load_profile` | `Exception` | pass | `import agent_manager` |
| 4 | `libs/qq_bot_runtime/bili_learn.py:179` | `fetch_audio_transcript` | `OSError` | pass | `os.remove(wav_path)` |
| 5 | `libs/qq_bot_runtime/bili_streamer.py:145` | `BiliStreamer.stop` | `Exception` | pass | `self._ffmpeg.stdin.close()` |
| 6 | `libs/qq_bot_runtime/bili_streamer.py:153` | `BiliStreamer.stop` | `Exception` | pass | `self._ffmpeg.kill()` |
| 7 | `libs/qq_bot_runtime/bili_streamer.py:163` | `BiliStreamer.stop` | `Exception` | pass | `self._sd_stream.stop()` |
| 8 | `libs/qq_bot_runtime/bili_streamer.py:169` | `BiliStreamer.stop` | `Exception` | pass | `self._ffmpeg_log.close()` |
| 9 | `libs/qq_bot_runtime/bili_streamer.py:368` | `BiliStreamer.speak` | `Exception` | pass | `import bili_captions` |
| 10 | `libs/qq_bot_runtime/bilibili_plugin.py:153` | `LiveReplyTarget.caption` | `Exception` | pass | `import bili_captions` |
| 11 | `libs/qq_bot_runtime/bilibili_plugin.py:333` | `BilibiliPlugin._go_standby` | `Exception` | pass | `import bili_captions` |
| 12 | `libs/qq_bot_runtime/bilibili_plugin.py:561` | `BilibiliPlugin._heartbeat_loop` | `Exception` | pass | `await ws.close()` |
| 13 | `libs/qq_bot_runtime/bilibili_plugin.py:565` | `BilibiliPlugin._heartbeat_loop` | `asyncio.CancelledError` | pass | `while True` |
| 14 | `libs/qq_bot_runtime/codebuddy_cli.py:267` | `_notify_edit_request` | `Exception` | pass | `from message_bus import get_event_bus` |
| 15 | `libs/qq_bot_runtime/fact_store.py:243` | `FactStore._main_insert` | `sqlite3.IntegrityError` | pass | `conn.execute("""` |
| 16 | `libs/qq_bot_runtime/file_lock.py:121` | `file_lock` | `OSError` | pass | `os.remove(path)` |
| 17 | `libs/qq_bot_runtime/get_bili_cookie.py:180` | `read_from_browser` | `Exception` | pass | `os.remove(tmp)` |
| 18 | `libs/qq_bot_runtime/launcher_core/runtime.py:142` | `Launcher.restart` | `Exception` | pass | `proc.terminate()` |
| 19 | `libs/qq_bot_runtime/launcher_core/runtime.py:149` | `Launcher.restart` | `Exception` | pass | `proc.kill()` |
| 20 | `libs/qq_bot_runtime/launcher_core/runtime.py:167` | `Launcher.shutdown` | `Exception` | pass | `proc.terminate()` |
| 21 | `libs/qq_bot_runtime/launcher_core/runtime.py:175` | `Launcher.shutdown` | `Exception` | pass | `proc.kill()` |
| 22 | `libs/qq_bot_runtime/long_term_memory.py:312` | `merge_alias_history` | `json.JSONDecodeError` | continue | `r = json.loads(line)` |
| 23 | `libs/qq_bot_runtime/main.py:167` | `run_bot` | `Exception` | pass | `proc.terminate()` |
| 24 | `libs/qq_bot_runtime/memory.py:451` | `SessionManagerAdapter.save` | `Exception` | pass | `sess.save(force=force)` |
| 25 | `libs/qq_bot_runtime/realtime_voice.py:154` | `RealtimeSession._close_streams` | `Exception` | pass | `await asyncio.to_thread(st.stop)` |
| 26 | `libs/qq_bot_runtime/realtime_voice.py:175` | `RealtimeSession._safe_close_ws` | `Exception` | pass | `await ws.close()` |
| 27 | `libs/qq_bot_runtime/realtime_voice.py:677` | `_PCMPlayer.stop` | `Exception` | pass | `self.stream.stop()` |
| 28 | `libs/qq_bot_runtime/realtime_voice.py:681` | `_PCMPlayer.stop` | `Exception` | pass | `self.stream.close()` |
| 29 | `libs/qq_bot_runtime/realtime_voice.py:759` | `LocalRealtimeSession.stop` | `Exception` | pass | `await asyncio.to_thread(self._in_stream.stop)` |
| 30 | `libs/qq_bot_runtime/realtime_voice.py:763` | `LocalRealtimeSession.stop` | `Exception` | pass | `self._in_stream.close()` |
| 31 | `libs/qq_bot_runtime/realtime_voice.py:772` | `LocalRealtimeSession.stop` | `Exception` | pass | `self._player.stop()` |
| 32 | `libs/qq_bot_runtime/realtime_voice.py:1163` | `LocalRealtimeSession._ensure_player` | `Exception` | pass | `self._player.stop()` |
| 33 | `libs/qq_bot_runtime/realtime_voice.py:1193` | `LocalRealtimeSession._interrupt` | `Exception` | pass | `resp.close()` |
| 34 | `libs/qq_bot_runtime/telemetry.py:70` | `_save` | `Exception` | pass | `os.makedirs(os.path.dirname(_TELEMETRY_FILE), exist_ok=True)` |
| 35 | `libs/qq_bot_runtime/telemetry_server.py:90` | `_save` | `Exception` | pass | `os.makedirs(os.path.dirname(STORE_FILE), exist_ok=True)` |
| 36 | `libs/qq_bot_runtime/test_time_indexed_performance.py:141` | `test_concurrent_performance` | `Exception` | pass | `os.remove(db_file)` |
| 37 | `libs/qq_bot_runtime/time_indexed_memory.py:271` | `TimeIndexedMemory.close_all` | `Exception` | pass | `conn.close()` |
| 38 | `libs/qq_bot_runtime/tts_vox.py:101` | `_SidecarProcess.terminate` | `Exception` | pass | `self.proc.kill()` |
| 39 | `libs/qq_bot_runtime/tts_vox.py:203` | `_cache_cleanup` | `OSError` | pass | `os.remove(old)` |
| 40 | `libs/qq_bot_runtime/tts_vox.py:205` | `_cache_cleanup` | `OSError` | pass | `limit = getattr(config, "VOXCPM_CACHE_MAX_FILES", 200)` |
| 41 | `libs/qq_bot_runtime/video_processor.py:176` | `extract_audio` | `OSError` | pass | `os.remove(wav_path)` |
| 42 | `libs/qq_bot_runtime/video_processor.py:223` | `extract_audio_segment` | `OSError` | pass | `os.remove(wav_path)` |
| 43 | `libs/qq_bot_runtime/voice_client.py:77` | `_write_atomic` | `OSError` | pass | `os.remove(old)` |
| 44 | `libs/qq_bot_runtime/voice_client.py:79` | `_write_atomic` | `OSError` | pass | `limit = getattr(config, "VOICE_TTS_CACHE_MAX_FILES", 300)` |
| 45 | `libs/qq_bot_runtime/web_plugin.py:647` | `_Handler._serve_sse` | `(BrokenPipeError, ConnectionResetError, ConnectionAbortedError)` | pass | `while True` |
| 46 | `plugins/greeting_demo/plugin.py:102` | `GreetingDemoPlugin._greet` | `Exception` | pass | `bridge.push(None, {"type": "message", "role": "assistant", "` |

### A 探测 / 回落类（可用 degrade 留痕）（63 条）

| 序号 | 位置 | 函数 | except | 体 | try 首句 |
|---|---|---|---|---|---|
| 1 | `app.py:108` | `bootstrap` | `Exception` | pass | `import time as _time_mod` |
| 2 | `app.py:152` | `main` | `Exception` | pass | `import ctypes` |
| 3 | `libs/qq_bot_runtime/_dbg_mem_tmp.py:34` | `<module>` | `Exception` | pass | `with urllib.request.urlopen("http://127.0.0.1:8799/health", ` |
| 4 | `libs/qq_bot_runtime/agent_manager.py:75` | `list_agents` | `Exception` | continue | `with open(fp, "r", encoding="utf-8") as f` |
| 5 | `libs/qq_bot_runtime/agent_manager.py:194` | `resolve_profile` | `Exception` | pass | `import ai_profile` |
| 6 | `libs/qq_bot_runtime/ai_profile.py:60` | `load_profile` | `(json.JSONDecodeError, OSError)` | pass | `with open(path, "r", encoding="utf-8") as f` |
| 7 | `libs/qq_bot_runtime/ai_provider.py:374` | `UnifiedLLM._call_openai_stream` | `Exception` | continue | `obj = json.loads(chunk)` |
| 8 | `libs/qq_bot_runtime/bili_dm.py:164` | `BilibiliDmPlugin._loop` | `asyncio.CancelledError` | pass | `while True` |
| 9 | `libs/qq_bot_runtime/bili_streamer.py:68` | `_resolve_ffmpeg` | `ImportError` | pass | `import imageio_ffmpeg` |
| 10 | `libs/qq_bot_runtime/bilibili_plugin.py:577` | `BilibiliPlugin._refresh_live_status` | `Exception` | pass | `async with httpx.AsyncClient() as http` |
| 11 | `libs/qq_bot_runtime/bilibili_plugin.py:619` | `BilibiliPlugin._on_message_body` | `Exception` | continue | `data = json.loads(pbody.decode("utf-8"))` |
| 12 | `libs/qq_bot_runtime/chat_service.py:135` | `ChatService._handle_message` | `Exception` | pass | `from proactive_speaker import get_speaker` |
| 13 | `libs/qq_bot_runtime/chat_service.py:1511` | `ChatService._chat_pipeline` | `Exception` | pass | `from realtime import get_time_context` |
| 14 | `libs/qq_bot_runtime/chat_service.py:1805` | `ChatService._build_stream_mc_hint` | `Exception` | pass | `st = core.brains.get("mc_bot").status()` |
| 15 | `libs/qq_bot_runtime/chat_service.py:1818` | `ChatService._build_stream_mc_hint` | `Exception` | pass | `brain = core.brains.get("mc_mod")` |
| 16 | `libs/qq_bot_runtime/codebuddy_cli.py:247` | `request_edit_approval` | `Exception` | pass | `from message_bus import get_event_bus` |
| 17 | `libs/qq_bot_runtime/emotion.py:280` | `_resolve_name` | `Exception` | pass | `override = (getattr(config, "NAME_OVERRIDES", None) or {}).g` |
| 18 | `libs/qq_bot_runtime/emotion.py:298` | `_resolve_name` | `Exception` | pass | `import long_term_memory` |
| 19 | `libs/qq_bot_runtime/get_bili_cookie.py:126` | `_decrypt_value` | `Exception` | pass | `return AESGCM(key).decrypt(nonce, ct, None).decode("utf-8", ` |
| 20 | `libs/qq_bot_runtime/get_bili_cookie.py:133` | `_decrypt_value` | `Exception` | pass | `return AESGCM(key).decrypt(nonce, ct, None).decode("utf-8", ` |
| 21 | `libs/qq_bot_runtime/get_bili_cookie.py:268` | `do_qr` | `Exception` | pass | `webbrowser.open(qr_url)` |
| 22 | `libs/qq_bot_runtime/identity.py:97` | `load_bindings` | `(json.JSONDecodeError, OSError)` | pass | `with open(_FILE, "r", encoding="utf-8") as f` |
| 23 | `libs/qq_bot_runtime/identity.py:546` | `mark_mc_asked` | `Exception` | pass | `with file_lock.file_lock(_FILE)` |
| 24 | `libs/qq_bot_runtime/identity.py:563` | `pending_mc_qq_of` | `Exception` | pass | `data = load_bindings()` |
| 25 | `libs/qq_bot_runtime/intent_router.py:558` | `detect_intent_ai` | `Exception` | pass | `from memory_context import build_memory_messages` |
| 26 | `libs/qq_bot_runtime/long_term_memory.py:364` | `_read_user_history_lines` | `json.JSONDecodeError` | continue | `records.append(json.loads(line))` |
| 27 | `libs/qq_bot_runtime/mc_bot_brain.py:250` | `_owner_call_name` | `Exception` | pass | `import emotion` |
| 28 | `libs/qq_bot_runtime/mc_bot_brain.py:472` | `BotAgent._heartbeat` | `Exception` | pass | `st = await asyncio.to_thread(_bridge_get, "/state")` |
| 29 | `libs/qq_bot_runtime/mc_bot_brain.py:582` | `BotAgent._guard_warning` | `Exception` | pass | `pr = self._probe_now()` |
| 30 | `libs/qq_bot_runtime/mc_bot_brain.py:622` | `BotAgent._stuck_check` | `Exception` | pass | `pr = _bridge_cmd("probe", timeout=8.0).get("probe") or {}` |
| 31 | `libs/qq_bot_runtime/mc_bot_brain.py:878` | `BotAgent._observe` | `Exception` | pass | `pr = self._probe_now()` |
| 32 | `libs/qq_bot_runtime/mc_bot_brain.py:943` | `BotAgent._observe` | `Exception` | pass | `from mc_tips import match_tips` |
| 33 | `libs/qq_bot_runtime/mc_bot_brain.py:963` | `BotAgent._observe` | `Exception` | pass | `invd = st.get("inventory") or {}` |
| 34 | `libs/qq_bot_runtime/mc_bot_brain.py:1304` | `BotAgent._t_craft` | `Exception` | pass | `from mc_recipes import check_craft_materials` |
| 35 | `libs/qq_bot_runtime/mc_bot_brain.py:1474` | `BotAgent.find_landmark` | `Exception` | pass | `from mc_explored import list_landmarks` |
| 36 | `libs/qq_bot_runtime/mc_bot_brain.py:1490` | `BotAgent.mark_explored` | `Exception` | pass | `from mc_explored import mark_explored` |
| 37 | `libs/qq_bot_runtime/mc_bot_brain.py:1524` | `BotAgent._routine_suggest` | `Exception` | pass | `from mc_explored import list_landmarks` |
| 38 | `libs/qq_bot_runtime/mc_bot_brain.py:1581` | `BotAgent._t_run_skill` | `Exception` | pass | `mc_skills.record_use(skill.get("name", name), success)` |
| 39 | `libs/qq_bot_runtime/proactive_speaker.py:135` | `ProactiveSpeaker.set_ws` | `Exception` | pass | `from message_bus import get_sender` |
| 40 | `libs/qq_bot_runtime/proactive_speaker.py:282` | `ProactiveSpeaker._generate_no_game_msg` | `Exception` | pass | `from realtime import format_now` |
| 41 | `libs/qq_bot_runtime/proactive_speaker.py:292` | `ProactiveSpeaker._generate_no_game_msg` | `Exception` | pass | `from screen_awareness import get_screen_awareness` |
| 42 | `libs/qq_bot_runtime/proactive_speaker.py:436` | `ProactiveSpeaker._generate_message` | `Exception` | pass | `from realtime import format_now` |
| 43 | `libs/qq_bot_runtime/qq_plugin.py:597` | `QQPlugin._handler` | `Exception` | pass | `from vision_capture import vision_capture` |
| 44 | `libs/qq_bot_runtime/qq_plugin.py:606` | `QQPlugin._handler` | `json.JSONDecodeError` | continue | `data = json.loads(raw)` |
| 45 | `libs/qq_bot_runtime/realtime_voice.py:260` | `RealtimeSession._reader` | `json.JSONDecodeError` | continue | `evt = json.loads(raw)` |
| 46 | `libs/qq_bot_runtime/realtime_voice.py:746` | `LocalRealtimeSession.start` | `Exception` | pass | `self._last_app = (self._get_active_context() or {}).get("app` |
| 47 | `libs/qq_bot_runtime/realtime_voice.py:1010` | `LocalRealtimeSession._llm_stream_raw` | `Exception` | continue | `obj = json.loads(data)` |
| 48 | `libs/qq_bot_runtime/telemetry.py:57` | `_load` | `Exception` | pass | `if os.path.exists(_TELEMETRY_FILE)` |
| 49 | `libs/qq_bot_runtime/telemetry.py:172` | `_enqueue` | `Exception` | pass | `item = {"group": group, "key": key, "event": event,` |
| 50 | `libs/qq_bot_runtime/telemetry.py:192` | `_flush_once` | `(URLError, OSError, Exception)` | pass | `import json as _json` |
| 51 | `libs/qq_bot_runtime/tts_vox.py:145` | `_wait_ready` | `Exception` | pass | `health = await _check_health(timeout=2)` |
| 52 | `libs/qq_bot_runtime/tts_vox.py:295` | `VoxTTSPlugin._health_loop` | `asyncio.CancelledError` | pass | `while True` |
| 53 | `libs/qq_bot_runtime/vector_memory.py:395` | `_tokenize_cjk_ngram` | `ImportError` | pass | `import opencc` |
| 54 | `libs/qq_bot_runtime/voice_client.py:119` | `text_to_speech` | `FileNotFoundError` | pass | `with open(cache_path, "rb") as f` |
| 55 | `libs/qq_bot_runtime/vox_tts_server.py:202` | `_vram_free_mb` | `Exception` | pass | `import torch` |
| 56 | `libs/qq_bot_runtime/vox_tts_server.py:471` | `main` | `Exception` | pass | `_vox_log = open(os.path.join(BASE_DIR, "vox_tts_server.log")` |
| 57 | `libs/qq_bot_runtime/web_plugin.py:196` | `WebPlugin._open_browser` | `Exception` | pass | `webbrowser.open(f"http://127.0.0.1:{self.port}")` |
| 58 | `libs/qq_bot_runtime/web_plugin.py:252` | `WebPlugin.rel_path` | `Exception` | pass | `p = os.path.abspath(path)` |
| 59 | `libs/qq_bot_runtime/web_plugin.py:318` | `WebPlugin._api_plugins` | `Exception` | pass | `st = self.core.status()` |
| 60 | `libs/qq_bot_runtime/web_plugin.py:424` | `WebPlugin._api_telemetry` | `(URLError, OSError, Exception)` | pass | `with urlopen(url, timeout=2) as r` |
| 61 | `libs/qq_bot_runtime/web_plugin.py:738` | `_Handler._api_mc` | `Exception` | pass | `with open(hints_file, "r", encoding="utf-8") as f` |
| 62 | `libs/qq_bot_runtime/web_plugin.py:785` | `_Handler._api_system` | `Exception` | pass | `with open(log_path, "r", encoding="utf-8", errors="ignore") ` |
| 63 | `tests/audit_silent_except.py:282` | `count_quiet_calls` | `Exception` | continue | `with open(p, "r", encoding="utf-8", errors="replace") as f` |

### C 需人工判定（77 条）

| 序号 | 位置 | 函数 | except | 体 | try 首句 |
|---|---|---|---|---|---|
| 1 | `bridge/app_window.py:54` | `_AppearanceApi.set_title` | `Exception` | pass | `w.set_title(str(title)[:40])` |
| 2 | `bridge/app_window.py:189` | `_wait_for_close` | `KeyboardInterrupt` | pass | `while True` |
| 3 | `bridge/builder_api.py:286` | `list_disk_dirs` | `OSError` | continue | `if not e.is_dir()` |
| 4 | `bridge/builder_api.py:1442` | `set_settings` | `Exception` | pass | `s["web_max_chars"] = max(1000, min(int(patch["web_max_chars"` |
| 5 | `bridge/builder_api.py:1448` | `set_settings` | `Exception` | pass | `s["web_max_pages"] = max(1, min(int(patch["web_max_pages"]),` |
| 6 | `bridge/builder_api.py:1454` | `set_settings` | `Exception` | pass | `s["max_steps"] = max(1, min(int(patch["max_steps"]), 40))` |
| 7 | `bridge/core_bridge.py:191` | `CoreBridge.push` | `Exception` | pass | `q.put_nowait(event)` |
| 8 | `bridge/memory_api.py:334` | `export_memory` | `Exception` | pass | `out["sessions"] = _read_json(os.path.join(os.getcwd(), _MEMO` |
| 9 | `bridge/pkg_manager.py:380` | `PackageManager.load` | `Exception` | pass | `wrapper.on_config(settings_store.get_plugin_config(name))` |
| 10 | `bridge/pkg_manager.py:469` | `PackageManager.attach_to` | `Exception` | pass | `core.brains._plugins.pop(meta["plugin_key"], None)` |
| 11 | `bridge/plugins_api.py:223` | `plugin_config_save` | `Exception` | pass | `if _pkg is not None` |
| 12 | `bridge/plugins_api.py:343` | `market_action` | `Exception` | pass | `rescan(bridge)` |
| 13 | `bridge/plugins_api.py:374` | `market_action` | `Exception` | pass | `if bridge.core is not None` |
| 14 | `bridge/plugins_api.py:382` | `market_action` | `Exception` | pass | `rescan(bridge)` |
| 15 | `bridge/plugins_api.py:586` | `market_seek` | `Exception` | pass | `rescan(bridge)` |
| 16 | `bridge/sidecar_runner.py:26` | `_log_dir` | `Exception` | pass | `os.makedirs(d, exist_ok=True)` |
| 17 | `bridge/sidecar_runner.py:137` | `SidecarProcess.stop` | `subprocess.TimeoutExpired` | pass | `p.wait(timeout=5)` |
| 18 | `libs/qq_bot_runtime/agent_core.py:85` | `ProactiveSpeakerPlugin._hold` | `asyncio.CancelledError` | pass | `while True` |
| 19 | `libs/qq_bot_runtime/ai_provider.py:388` | `UnifiedLLM._call_openai_stream` | `Exception` | pass | `on_delta("think", rc)` |
| 20 | `libs/qq_bot_runtime/ai_provider.py:395` | `UnifiedLLM._call_openai_stream` | `Exception` | pass | `on_delta("content", c)` |
| 21 | `libs/qq_bot_runtime/bili_captions.py:165` | `stop` | `Exception` | pass | `_server.shutdown()` |
| 22 | `libs/qq_bot_runtime/bili_dm.py:148` | `BilibiliDmPlugin.stop` | `Exception` | pass | `await self._http.aclose()` |
| 23 | `libs/qq_bot_runtime/bili_learn_scheduler.py:89` | `BilibiliLearnScheduler.stop` | `Exception` | pass | `await self._http.aclose()` |
| 24 | `libs/qq_bot_runtime/bili_learn_scheduler.py:100` | `BilibiliLearnScheduler._loop` | `asyncio.CancelledError` | pass | `while True` |
| 25 | `libs/qq_bot_runtime/chat_service.py:539` | `ChatService._handle_message` | `ValueError` | pass | `count = int(raw[1])` |
| 26 | `libs/qq_bot_runtime/chat_service.py:811` | `ChatService._handle_message` | `ValueError` | pass | `idx = int(raw)` |
| 27 | `libs/qq_bot_runtime/chat_service.py:2190` | `_parse_mood_line` | `ValueError` | pass | `strength = max(1, min(int(parts[1]), 3))` |
| 28 | `libs/qq_bot_runtime/codebuddy_cli.py:245` | `request_edit_approval` | `RuntimeError` | pass | `loop = asyncio.get_event_loop()` |
| 29 | `libs/qq_bot_runtime/dxcam_capture.py:44` | `DXCamCapture.open` | `Exception` | pass | `self.camera.release()` |
| 30 | `libs/qq_bot_runtime/file_lock.py:92` | `file_lock` | `OSError` | pass | `msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)` |
| 31 | `libs/qq_bot_runtime/get_bili_cookie.py:262` | `do_qr` | `Exception` | pass | `os.startfile(png)` |
| 32 | `libs/qq_bot_runtime/launcher_core/bootstrap.py:29` | `_configure_stdio_utf8` | `Exception` | pass | `if hasattr(s, "reconfigure")` |
| 33 | `libs/qq_bot_runtime/launcher_core/bootstrap.py:43` | `_ensure_utf8_fs` | `OSError` | pass | `os.execv(sys.executable, [sys.executable] + sys.argv)` |
| 34 | `libs/qq_bot_runtime/launcher_core/runtime.py:185` | `Launcher.run` | `(ValueError, OSError)` | pass | `old[s] = signal.signal(s, self._on_signal)` |
| 35 | `libs/qq_bot_runtime/launcher_core/runtime.py:205` | `Launcher.run` | `Exception` | pass | `signal.signal(s, h)` |
| 36 | `libs/qq_bot_runtime/long_term_memory.py:317` | `merge_alias_history` | `OSError` | pass | `os.rename(alias_path, merged_path)` |
| 37 | `libs/qq_bot_runtime/main.py:101` | `_monitor_push_loop` | `Exception` | pass | `telemetry_save()` |
| 38 | `libs/qq_bot_runtime/main.py:159` | `run_bot` | `asyncio.CancelledError` | pass | `await asyncio.Future()` |
| 39 | `libs/qq_bot_runtime/mc_bot_brain.py:564` | `BotAgent._guard_warning` | `Exception` | pass | `_bridge_cmd("swim", timeout=8.0)` |
| 40 | `libs/qq_bot_runtime/mc_bot_brain.py:636` | `BotAgent._stuck_check` | `Exception` | pass | `_notify_qq(f"游戏里的我在 ({int(x)},{int(z)}) 一时出不去，不用管我——我会绕路/搭柱自` |
| 41 | `libs/qq_bot_runtime/mc_bot_brain.py:791` | `BotAgent._rule_reply` | `Exception` | pass | `_bridge_cmd("say", text=say, timeout=8.0)` |
| 42 | `libs/qq_bot_runtime/mc_bot_brain.py:975` | `BotAgent._observe` | `Exception` | pass | `sugg = self._routine_suggest(st)` |
| 43 | `libs/qq_bot_runtime/mc_bot_brain.py:995` | `BotAgent._brain_round` | `Exception` | pass | `if await self._pull_game_chat()` |
| 44 | `libs/qq_bot_runtime/mc_bot_brain.py:1035` | `BotAgent._brain_round` | `Exception` | pass | `_bridge_cmd("stop", timeout=8.0)` |
| 45 | `libs/qq_bot_runtime/mc_bot_brain.py:1043` | `BotAgent._brain_round` | `Exception` | pass | `await exec_task` |
| 46 | `libs/qq_bot_runtime/mc_bot_brain.py:1326` | `BotAgent._t_chest` | `Exception` | pass | `_bridge_cmd("container", op="close", timeout=8.0)` |
| 47 | `libs/qq_bot_runtime/mc_bot_brain.py:1351` | `BotAgent._t_furnace` | `Exception` | pass | `_bridge_cmd("furnace", op="close", timeout=8.0)` |
| 48 | `libs/qq_bot_runtime/mc_bot_brain.py:1430` | `BotAgent.stop_move` | `Exception` | pass | `_bridge_cmd("stop", timeout=8.0)` |
| 49 | `libs/qq_bot_runtime/mc_bot_brain.py:1463` | `BotAgent.is_owner_name` | `Exception` | pass | `if identity.enabled()` |
| 50 | `libs/qq_bot_runtime/mc_bot_run.py:48` | `main` | `asyncio.CancelledError` | pass | `await task` |
| 51 | `libs/qq_bot_runtime/memory.py:59` | `Memory._atexit_flush` | `Exception` | pass | `if self._dirty` |
| 52 | `libs/qq_bot_runtime/memory_client.py:78` | `_ModuleProxy.__getattr__._call` | `_ServerUnavailable` | pass | `return await _http_call(self._module, func, args, kwargs)` |
| 53 | `libs/qq_bot_runtime/monitor_server.py:138` | `main` | `KeyboardInterrupt` | pass | `httpd.serve_forever()` |
| 54 | `libs/qq_bot_runtime/qq_plugin.py:559` | `QQPlugin.start` | `Exception` | pass | `get_event_bus().on("code.edit_request", _on_edit_request)` |
| 55 | `libs/qq_bot_runtime/qq_plugin.py:575` | `QQPlugin.stop` | `Exception` | pass | `await self._server.wait_closed()` |
| 56 | `libs/qq_bot_runtime/realtime_voice.py:166` | `RealtimeSession._teardown_tasks` | `(asyncio.CancelledError, Exception)` | pass | `await task` |
| 57 | `libs/qq_bot_runtime/realtime_voice.py:404` | `RealtimeSession._queue_audio_outbound` | `asyncio.QueueFull` | pass | `self._send_q.put_nowait(("audio", data))` |
| 58 | `libs/qq_bot_runtime/realtime_voice.py:1039` | `LocalRealtimeSession._get_active_context` | `Exception` | pass | `app = psutil.Process(pid.value).name()` |
| 59 | `libs/qq_bot_runtime/run_agent.py:39` | `main` | `asyncio.CancelledError` | pass | `await asyncio.Future()` |
| 60 | `libs/qq_bot_runtime/service_host.py:49` | `_to_jsonable` | `Exception` | pass | `return obj.item()` |
| 61 | `libs/qq_bot_runtime/service_host.py:54` | `_to_jsonable` | `Exception` | pass | `return _to_jsonable(obj.tolist())` |
| 62 | `libs/qq_bot_runtime/service_host.py:165` | `serve` | `KeyboardInterrupt` | pass | `httpd.serve_forever()` |
| 63 | `libs/qq_bot_runtime/telemetry.py:202` | `_reporter_loop` | `Exception` | pass | `_flush_once()` |
| 64 | `libs/qq_bot_runtime/telemetry_server.py:212` | `main` | `KeyboardInterrupt` | pass | `httpd.serve_forever()` |
| 65 | `libs/qq_bot_runtime/vision_capture.py:163` | `VisionCapture.stop` | `asyncio.CancelledError` | pass | `await self._capture_task` |
| 66 | `libs/qq_bot_runtime/vision_capture.py:169` | `VisionCapture.stop` | `asyncio.CancelledError` | pass | `await self._recognition_task` |
| 67 | `libs/qq_bot_runtime/vision_capture.py:288` | `VisionCapture._recognition_loop` | `asyncio.TimeoutError` | pass | `wait = max(0.05, max_interval - (time.time() - self.last_rep` |
| 68 | `libs/qq_bot_runtime/voice_room.py:375` | `VoiceRoom._cleanup_hotkey` | `Exception` | pass | `if self._hotkey_handle is not None` |
| 69 | `libs/qq_bot_runtime/voice_room.py:439` | `VoiceRoom._schedule_loop` | `Exception` | pass | `asyncio.run_coroutine_threadsafe(coro, loop)` |
| 70 | `libs/qq_bot_runtime/web_plugin.py:208` | `WebPlugin.serve_no_core` | `KeyboardInterrupt` | pass | `while True` |
| 71 | `libs/qq_bot_runtime/web_plugin.py:225` | `WebPlugin.push` | `Exception` | pass | `q.put_nowait(event)` |
| 72 | `libs/qq_bot_runtime/web_plugin.py:304` | `WebPlugin.state` | `Exception` | pass | `st["status"] = self.core.status()` |
| 73 | `libs/qq_bot_runtime/web_plugin.py:910` | `main` | `KeyboardInterrupt` | pass | `WebPlugin(core=None, port=args.port).serve_no_core()` |
| 74 | `plugins/greeting_demo/plugin.py:51` | `GreetingDemoPlugin.apply_config` | `Exception` | pass | `self._cfg["interval_seconds"] = max(10, int(cfg["interval_se` |
| 75 | `plugins/greeting_demo/plugin.py:60` | `GreetingDemoPlugin.apply_config` | `Exception` | pass | `ev.set()` |
| 76 | `plugins/greeting_demo/plugin.py:84` | `GreetingDemoPlugin._loop` | `asyncio.TimeoutError` | pass | `await asyncio.wait_for(self._wake.wait(),` |
| 77 | `server.py:31` | `QuietServer.server_bind` | `OSError` | pass | `self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIV` |

---

重新生成：`python tests/audit_silent_except.py --md DEGRADE_AUDIT.md --json data/degrade_inventory.json`
