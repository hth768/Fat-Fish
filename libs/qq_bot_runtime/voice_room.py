# -*- coding: utf-8 -*-
"""实时语音房间（voice_room.py）：会话状态机 + 全局热键 + 转录落盘/记忆同步。

这是独立于 QQ 异步消息链路的第二条「实时」通道：
  - 引擎 realtime_voice.RealtimeSession 负责 GLM-Realtime 全双工传音；
  - 本模块负责：什么时候开/关（命令/热键）、free/ptt 两种说话方式、
    空闲自动挂断（防挂机计费）、转录日志、会话摘要写入主人 QQ 记忆。

用法（QQ 命令或控制台命令经 chat_service 调用）：
  from voice_room import get_voice_room
  await get_voice_room().start()   # 返回状态文本，直接回复调用方
  await get_voice_room().stop()
  await get_voice_room().status()

两种说话模式（config.REALTIME_TALK_MODE）：
  free —— 免提：热键单击开/关房间；开着就能说话，server_vad 自动切轮，说话即打断；
  ptt  —— 按住热键说话：按住开麦，松开提交；机器人说话时按住 = 打断并重说。
"""
import asyncio
import datetime
import json
import os
import time

import config
from quiet import degrade

# 实时版人设：口语短句、可打断；语音里读不出表情/链接，规则写死避免每轮浪费 token
_REALTIME_INSTRUCTIONS = (
    "你是「肥鱼娘」，一个可爱、傲娇又贴心的萌娘智能体（DeepSeek 拟人化）。"
    "现在正和本机的主人用语音实时对话。说话规则："
    "1. 每次回答保持 1~3 句口语短句，像真人说话，不要念标题、不要列条目、不要用书面语；"
    "2. 不要输出表情符号、颜文字、Markdown 或网址，因为要朗读；"
    "3. 不用每句都加口癖，自然一点，但可以保留偶尔的「唔」「嘛」这类语气开头；"
    "4. 听不清或没把握就直接说没听清或不知道，不要编；"
    "5. 主人说话随时可以打断你，被插话就停下来听他说，不要继续说；"
    "6. 如果话题需要查资料或看网络，告诉他「我去查一下，等会告诉你」，不要说假信息；"
    "7. 全程用简体中文。"
)


class VoiceRoom:
    """实时语音会话的单一入口（单例）。同一时刻只允许一个会话。"""

    def __init__(self):
        self.state = "idle"            # idle | starting | active
        self.mode = getattr(config, "REALTIME_TALK_MODE", "free")
        if self.mode not in ("free", "ptt"):
            self.mode = "free"
        self.session = None
        self._loop = None
        self._started_at = 0.0
        self._last_activity = 0.0
        self._bot_talking = False
        self._turn_count = 0
        self._session_lines = []       # 本会话她说过的内容（留最近若干条做摘要）
        self._watch_task = None
        self._kb = None                # keyboard 模块（热键，可选）
        self._hotkey_handle = None
        self._hotkey_note = ""
        self._ptt_pressed = False      # keyboard 回调线程与事件循环共享的标志
        self._ptt_want_commit = False

    # ======================================================================
    # 对外命令（chat_service 调用，返回可直接回复的状态文本）
    # ======================================================================
    async def start(self) -> str:
        """开启语音对讲。成功/失败都返回文本（由调用方回复给 QQ/控制台）。"""
        if self.state == "active":
            return self.status()
        if self.state == "starting":
            return "语音对讲正在启动中，稍等片刻再试~"
        self.state = "starting"
        self._loop = asyncio.get_running_loop()
        self._last_activity = time.time()
        err = ""
        try:
            self._check_audio_devices()
            from realtime_voice import RealtimeSession, LocalRealtimeSession
            engine = str(getattr(config, "REALTIME_ENGINE", "glm")).lower()
            instructions, system_messages = self._build_voice_system()
            if engine == "local":
                # 本地管线：voxcpm2 流式 TTS（肥鱼娘声线）+ 云端 ASR/LLM
                sess = LocalRealtimeSession(
                    instructions=instructions, system_messages=system_messages,
                    mode=self.mode,
                    voice_desc=getattr(config, "REALTIME_VOICE_DESC", "") or config.VOXCPM_VOICE_DESC,
                    tts_base=getattr(config, "LOCAL_TTS_BASE", ""))
            else:
                sess = RealtimeSession(instructions=instructions, mode=self.mode)
            sess.on_state = self._on_engine_state
            sess.on_turn_end = self._on_turn_end
            await sess.start()
            self.session = sess
            # PTT 模式：默认不开麦，按下热键才采集
            if self.mode == "ptt":
                sess.set_capture(False)
        except Exception as e:
            err = str(e)
            self.session = None
            self.state = "idle"
            self._cleanup_hotkey()
            return f"语音对讲开启失败：{err}"

        self.state = "active"
        self._started_at = time.time()
        self._turn_count = 0
        self._session_lines = []
        self._install_hotkey()
        self._watch_task = asyncio.create_task(self._watchdog())
        self._notify("语音对讲已开启（本机扬声器/麦克风通道）", quiet=True)
        extra = ""
        if self._hotkey_note:
            extra = f"\n{self._hotkey_note}"
        return self.status() + extra

    async def stop(self, reason: str = "手动") -> str:
        """关闭语音对讲。"""
        if self.state == "idle":
            return "语音对讲本来就关着哦~"
        was_active = self.state == "active"
        self.state = "idle"
        self._bot_talking = False
        if self._watch_task:
            self._watch_task.cancel()
            self._watch_task = None
        self._cleanup_hotkey()
        sess, self.session = self.session, None
        if sess is not None:
            try:
                await sess.stop()
            except Exception as e:
                print(f"[VOICE-ROOM] 会话关闭异常: {e}")
        if was_active:
            self._remember_session(reason)
            self._notify(f"语音对讲已关闭（{reason}）", quiet=True)
        return f"语音对讲已关闭（{reason}）"

    def status(self) -> str:
        """当前状态文本。"""
        if self.state == "starting":
            return "语音对讲启动中…"
        if self.state != "active":
            return "语音对讲未开启。说「/语音对讲」开启（需要电脑有麦克风和音箱）。"
        mode_desc = "免提（直接说话即可）" if self.mode == "free" else "按住热键说话"
        tmo = int(getattr(config, "REALTIME_IDLE_TIMEOUT", 300) or 0)
        idle = "空闲自动挂断" if tmo > 0 else "不会自动挂断"
        hotkey = getattr(config, "REALTIME_TALK_HOTKEY", "f8") or "f8"
        dur = int(time.time() - self._started_at) if self._started_at else 0
        return (
            f"语音对讲运行中：{mode_desc}；本会话已对话 {self._turn_count} 轮，"
            f"持续 {dur} 秒；{idle}（{tmo}s）；热键 {hotkey}。"
            f"说「/语音对讲关」关闭。")

    # ======================================================================
    # 音频设备自检
    # ======================================================================
    def _check_audio_devices(self):
        """启动前自检输入输出设备，失败抛 RuntimeError(中文)。"""
        try:
            import sounddevice as sd
        except ImportError:
            raise RuntimeError("未安装 sounddevice，无法采集/播放音频")

        # 设备解析：中文名 + 多后端(WASAPI/DirectSound/MME/WDM-KS)会匹配到多个同名
        # 设备，query_devices(name) 会抛 "Multiple ... found"。改用 ASCII 子串 'LE202'
        # 定位单一索引，优先 WASAPI(hostapi==0)；返回 int 索引或 None(=系统默认)。
        def _resolve(name, kind):
            if not name:
                return None
            best = None
            for i, d in enumerate(sd.query_devices()):
                ok_in = kind == "input" and d["max_input_channels"] > 0
                ok_out = kind == "output" and d["max_output_channels"] > 0
                if "LE202" in d["name"] and (ok_in or ok_out):
                    best = i
                    if d["hostapi"] == 0:
                        return i
            return best

        try:
            if getattr(config, "REALTIME_INPUT_DEVICE", ""):
                idx = _resolve(config.REALTIME_INPUT_DEVICE, "input")
                if idx is None:
                    raise RuntimeError("找不到匹配的麦克风设备")
                sd.query_devices(idx, kind="input")
            else:
                sd.query_devices(kind="input")   # 无默认输入设备时抛错
        except Exception as e:
            raise RuntimeError(f"没有可用的麦克风输入设备：{e}")
        try:
            if getattr(config, "REALTIME_OUTPUT_DEVICE", ""):
                idx = _resolve(config.REALTIME_OUTPUT_DEVICE, "output")
                if idx is None:
                    raise RuntimeError("找不到匹配的音箱设备")
                sd.query_devices(idx, kind="output")
            else:
                sd.query_devices(kind="output")
        except Exception as e:
            raise RuntimeError(f"没有可用的音箱输出设备：{e}")

    def _voice_owner_qq(self) -> str:
        """实时对话对应的主人 QQ（记忆注入用）。

        优先用实时专属配置，回落到主配置/已知主人，确保即便没填也能注入记忆。
        """
        return (getattr(config, "REALTIME_OWNER_QQ", "")
                or getattr(config, "BOT_OWNER_QQ", "")
                or "2190720017")

    def _build_voice_system(self):
        """拼出实时语音用的完整 system：人设(基座) + 记忆上下文。

        返回 (instructions_str, system_messages_list)：
          - instructions_str 供 GLM-Realtime（单字符串字段）使用；
          - system_messages_list 供本地引擎（多 system 消息，结构更清晰）使用。

        人设基座直接复用主对话 config.SYSTEM_PROMPT（完整肥鱼娘人设，deepseek-v4-flash
        在 QQ 里就是靠它正常扮演的），再叠加「语音模式覆盖规则」去掉表情/Markdown/网址、
        限制短句口语——这些与主对话的文字场景冲突，但语音里会被原样念出来很怪。
        记忆部分复用 memory_context.build_memory_messages，与 QQ 聊天读同一套
        人物档案 / 重要信息 / AI 自我认知 / 反思 / 人格记忆（仅同步部分，不做异步历史检索）。
        """
        # ---- 1) 人设基座：复用主对话 SYSTEM_PROMPT + 语音覆盖规则 ----
        base = (getattr(config, "SYSTEM_PROMPT", "") or "").strip()
        voice_override = (
            "\n\n【语音实时对话模式·覆盖规则】你此刻正通过语音与主人实时对话，"
            "以下规则覆盖上面的一般设定：\n"
            "1. 只用纯口语说话，不要输出任何 QQ 表情代码（如[旺柴]）、颜文字、"
            "Markdown、网址或 [表情包:xxx] 标记，这些会被原样念出来很怪；\n"
            "2. 每次回答 1~3 句短句，像真人聊天，不要念标题、不要列大段条目；\n"
            "3. 不用每句都带口癖，自然一点；\n"
            "4. 听不清或不知道就直说，不要编。"
        )
        try:
            from realtime import format_now
            now_text = format_now()
        except Exception:
            now_text = ""
        if now_text:
            voice_override += f"\n当前时间：{now_text}。"
        base_full = (base + voice_override) if base else (_REALTIME_INSTRUCTIONS + voice_override)

        # ---- 2) 记忆上下文（同步部分：profile/notes/ai_profile/reflection/persona）----
        memory_msgs = []
        try:
            from memory_context import build_memory_messages
            owner = self._voice_owner_qq()
            memory_msgs = build_memory_messages(
                owner, include={"profile": True, "notes": True,
                                "ai_profile": True, "reflection": True,
                                "persona": True, "history": False})
        except Exception as e:
            print(f"[VOICE-ROOM] 记忆注入失败(可忽略): {e}")

        instructions = base_full + "\n\n" + "\n\n".join(m["content"] for m in memory_msgs)
        system_messages = [{"role": "system", "content": base_full}] + memory_msgs
        return instructions, system_messages

    # ======================================================================
    # 引擎事件回调（事件循环线程内执行）
    # ======================================================================
    def _on_engine_state(self, state: str, info: str):
        """引擎状态事件：更新活动时间/说话标志；异常断线则自动收摊。"""
        if self.state != "active":
            return
        now = time.time()
        if state in ("user_talking", "user_done", "bot_talking", "bot_done"):
            self._last_activity = now
        if state == "user_talking":
            self._bot_talking = False    # 主人开口了，视为打断
            print("[VOICE-ROOM] （听到主人说话…）")
        elif state == "bot_talking":
            self._bot_talking = True
        elif state == "bot_done":
            self._bot_talking = False
            self._turn_count += 1
        elif state == "stopped" and info:
            # 连接断开等非正常停止 → 自动收摊并通知
            print(f"[VOICE-ROOM] 会话意外停止: {info}")
            asyncio.ensure_future(self.stop(reason=info))

    async def _on_turn_end(self, text: str):
        """她说完一段：记日志 + 缓存会话内容（供结束摘要）。引擎会 await 本回调。"""
        text = text.strip()
        if not text:
            return
        self._log("bot", text)
        self._session_lines.append(text)
        if len(self._session_lines) > 12:
            self._session_lines = self._session_lines[-12:]

    # ======================================================================
    # 转录日志 + 记忆同步（异步、绝不阻塞对话）
    # ======================================================================
    def _log(self, role: str, text: str):
        if not getattr(config, "REALTIME_MEMORY_LOG", True):
            return
        try:
            day = datetime.date.today().strftime("%Y%m%d")
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "data", f"realtime_chat_{day}.jsonl")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            line = {"ts": datetime.datetime.now().isoformat(timespec="seconds"),
                    "role": role, "text": text}
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(line, ensure_ascii=False) + "\n")
        except Exception as e:
            print(f"[VOICE-ROOM] 转录日志失败: {e}")

    def _remember_session(self, reason: str):
        """会话结束时，把刚才说的话压缩成一条摘要写入主人 QQ 私聊记忆（无 LLM 参与）。"""
        owner = getattr(config, "REALTIME_OWNER_QQ", "") or ""
        if not owner or not self._session_lines:
            return
        try:
            digest = "；".join(self._session_lines[-6:])
            content = (f"（实时语音对讲摘要，原因:{reason}）刚才和主人在本机语音对讲，"
                       f"她说了 {self._turn_count} 轮，内容大致是：{digest}")
            from chat_service import get_chat_service
            cs = get_chat_service()
            if cs is not None and getattr(cs, "memory", None) is not None:
                cs.memory.add("private", "", owner, "assistant", content)
                print(f"[VOICE-ROOM] 会话摘要已写入 QQ 记忆({owner})")
        except Exception as e:
            print(f"[VOICE-ROOM] 记忆同步失败: {e}")

    # ======================================================================
    # 空闲看门狗：超过阈值自动挂断（防挂机计费）
    # ======================================================================
    async def _watchdog(self):
        tmo = float(getattr(config, "REALTIME_IDLE_TIMEOUT", 300) or 0)
        if tmo <= 0:
            return
        while self.state == "active":
            await asyncio.sleep(5)
            if self.state != "active":
                break
            if time.time() - self._last_activity > tmo:
                print("[VOICE-ROOM] 空闲超时，自动挂断")
                await self.stop(reason="空闲超时自动挂断")

    # ======================================================================
    # 全局热键（keyboard 库，可选能力；不可用则仅靠 QQ 命令开关）
    # ======================================================================
    def _install_hotkey(self):
        self._cleanup_hotkey()
        hotkey = (getattr(config, "REALTIME_TALK_HOTKEY", "") or "f8").lower()
        try:
            import keyboard
            self._kb = keyboard
        except Exception:
            self._hotkey_note = "keyboard 库不可用：热键关闭，只能用 QQ 命令「/语音对讲」开关。"
            return
        try:
            if self.mode == "free":
                self._hotkey_handle = keyboard.add_hotkey(hotkey, self._free_key_press)
                self._hotkey_note = f"热键 {hotkey}：单击开关对讲。"
            else:
                keyboard.on_press_key(hotkey, self._ptt_key_down)
                keyboard.on_release_key(hotkey, self._ptt_key_up)
                self._hotkey_note = f"热键 {hotkey}：按住说话，松开提交。"
        except Exception as e:
            self._hotkey_note = f"热键注册失败({e})：用 QQ 命令「/语音对讲」开关。"

    def _cleanup_hotkey(self):
        if self._kb is None:
            return
        try:
            if self._hotkey_handle is not None:
                self._kb.remove_hotkey(self._hotkey_handle)
                self._hotkey_handle = None
            else:
                # ptt 模式用 on_press/on_release 钩子，宽松清理（仅本进程钩子）
                self._kb.unhook_all()
        except Exception as e:
            degrade("libs/qq_bot_runtime/voice_room.py:375 VoiceRoom._cleanup_hotkey", e, "降级：if self._hotkey_handle is not None")

    def _free_key_press(self):
        """free 模式：单击切换开/关。keyboard 钩子线程 → 事件循环。"""
        self._schedule_loop(self._toggle_from_hotkey())

    async def _toggle_from_hotkey(self):
        try:
            if self.state == "active":
                text = await self.stop(reason="热键关闭")
                print(f"[VOICE-ROOM] {text}")
            elif self.state == "idle":
                text = await self.start()
                print(f"[VOICE-ROOM] {text.splitlines()[0]}")
        except Exception as e:
            print(f"[VOICE-ROOM] 热键切换异常: {e}")

    def _ptt_key_down(self, _evt=None):
        if self._ptt_pressed:
            return                     # 长按自动重复，忽略
        self._ptt_pressed = True
        self._schedule_loop(self._ptt_press())

    def _ptt_key_up(self, _evt=None):
        if not self._ptt_pressed:
            return
        self._ptt_pressed = False
        self._schedule_loop(self._ptt_release())

    async def _ptt_press(self):
        """按住说话：确保房间开着 → 若她在说则打断 → 开麦采集。"""
        try:
            if self.state == "idle":
                text = await self.start()
                if self.state != "active":
                    print(f"[VOICE-ROOM] {text.splitlines()[0]}")
                    return
            sess = self.session
            if sess is None:
                return
            if self._bot_talking:
                await sess.cancel_response()   # 打断她，轮到主人说
            sess.set_capture(True)
        except Exception as e:
            print(f"[VOICE-ROOM] 开麦异常: {e}")

    async def _ptt_release(self):
        """松开：提交这一句（commit + response.create）。"""
        sess = self.session
        if sess is None:
            return
        try:
            await sess.commit_turn()
        except Exception as e:
            print(f"[VOICE-ROOM] 提交语音异常: {e}")

    def _schedule_loop(self, coro):
        """keyboard 钩子线程 → 事件循环。"""
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            asyncio.run_coroutine_threadsafe(coro, loop)
        except Exception as e:
            degrade("libs/qq_bot_runtime/voice_room.py:439 VoiceRoom._schedule_loop", e, "降级：asyncio.run_coroutine_threadsafe(coro, loop)")

    # ======================================================================
    # 通知：控制台 + 主人 QQ 私聊（自动事件用）
    # ======================================================================
    def _notify(self, text: str, quiet: bool = False):
        print(f"[VOICE-ROOM] {text}")
        if quiet:
            return
        owner = getattr(config, "REALTIME_OWNER_QQ", "") or ""
        if not owner:
            return
        try:
            from message_bus import get_sender
            sender = get_sender()
            if sender is not None:
                asyncio.run_coroutine_threadsafe(
                    sender.send_private(owner, text), self._loop)

        except Exception as e:
            print(f"[VOICE-ROOM] QQ 通知失败: {e}")


_room: VoiceRoom = None


def get_voice_room() -> VoiceRoom:
    """全局单例。"""
    global _room
    if _room is None:
        _room = VoiceRoom()
    return _room
