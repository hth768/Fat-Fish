# -*- coding: utf-8 -*-
"""主动说话调度器：让肥鱼娘主动找话题、主动说话。

触发时机（后台循环，与聊天主循环并行）：
1. 游戏中有趣发现时：自主游戏代理发现钻石/村庄/怪物等，主动私聊/发群分享
2. 定时主动搭话：空闲时间主动找话题闲聊（结合当前 Minecraft 状态）
3. 群聊相关话题接话：在群里看到与自己相关的话题时主动发言（在 bot.py 中处理）

发送对象：
- 私聊你（config.PROACTIVE_PRIVATE_USER_ID）
- 指定群（config.PROACTIVE_GROUP_ID）
"""
import asyncio
import json
import random
import time
from typing import Dict, List, Optional

import config
from ai_provider import get_llm
from quiet import degrade

# 全局单例
_speaker_instance: Optional["ProactiveSpeaker"] = None


def get_speaker() -> "ProactiveSpeaker":
    global _speaker_instance
    if _speaker_instance is None:
        _speaker_instance = ProactiveSpeaker()
    return _speaker_instance


# 系统人设（复用 bot 的肥鱼娘人设）
_ACTIVE_SYSTEM_PROMPT = """你是一个可爱、傲娇、偶尔搞怪的萌娘「肥鱼娘」，正在 Minecraft 世界里自主游玩。
你现在要【主动】给同伴发一条消息，像真人朋友一样自然分享。

要求：
- 用口语化、俏皮、傲娇的语气，带点动作描写（如“唔...”、“嘛~”、“好麻烦啊”）
- 简短精炼，1-3 句话即可
- 根据提供的“当前游戏状态”，说一件你【真的】正在做或刚发现的趣事
- 只能说你【确实】知道的事（状态里有记录的），绝对不能编造游戏里没有的发现
- 如果是闲聊（没有特殊状态），就主动找个轻松的话题
- 不要说得太正式，不要列点
- 结尾不要加“懂了没”之类的教学语气"""

# 游戏没在运行时的主动消息提示词（不编造游戏发现，但可问游戏相关问题）
_NO_GAME_SYSTEM_PROMPT = """你是一个可爱、傲娇、偶尔搞怪的萌娘「肥鱼娘」。同伴现在【没有在玩 Minecraft】，
游戏没有运行，所以你【不知道】游戏世界里发生了什么。

你现在要【主动】给同伴发一条消息，像真人朋友一样自然。

要求：
- 用口语化、俏皮、傲娇的语气，带点动作描写（如“唔...”、“嘛~”、“好麻烦啊”）
- 简短精炼，1-3 句话即可
- 【绝对不要】编造任何游戏里发生的事（因为游戏没开，你没有游戏状态）
- 可以【主动问】对方关于游戏的问题，比如“你最近玩我的世界了吗？”“要不要我陪你一起玩”“想不想看我挖矿呀”
- 也可以聊聊别的轻松话题，但不要假装你正在游戏里做什么
- 结尾不要加“懂了没”之类的教学语气"""


class ProactiveSpeaker:
    """主动说话调度器。"""

    def __init__(self):
        self.running = False
        self._task: Optional[asyncio.Task] = None
        self._llm = get_llm()
        self._ws = None          # 兼容旧接口（裸 ws）
        self._sender = None      # 新接口：统一 MessageSender（优先使用）
        # 去重：最近已发送的内容（避免重复说同样的话）
        self._recent_msgs: List[str] = []
        self._max_recent = 10
        # 游戏发现状态追踪
        self._last_agent_round = 0
        self._last_interesting_hash = ""
        # 统计
        self._sent_count = 0
        self._last_sent_time = 0.0
        # 私聊/群聊发送限制（避免刷屏）
        self._last_private_sent = 0.0
        self._last_group_sent = 0.0
        # 主动私聊频率状态机（初始化默认值，_loop 启动时会按配置刷新）
        self._state = "warmup"            # warmup / stable / silent
        self._state_start = time.time()
        self._unreplied = 0               # 连续未回复数
        self._last_msg_time = 0.0         # 最近一次主动消息时间
        self._warmup_msg_sent = False     # 高活跃期是否已至少发过1条（保证前3分钟至少说1句）
        self._warmup_duration = getattr(config, "PROACTIVE_WARMUP_DURATION", 180)
        self._warmup_prob = getattr(config, "PROACTIVE_WARMUP_PROB", 1.0 / 3.0)
        self._stable_prob = getattr(config, "PROACTIVE_STABLE_PROB", 1.0 / 30.0)
        self._unreplied_limit = getattr(config, "PROACTIVE_UNREPLIED_LIMIT", 3)
        self._silent_duration = getattr(config, "PROACTIVE_SILENT_DURATION", 21600)

    # ---- 状态 ----
    def is_running(self) -> bool:
        return self.running

    def get_stats(self) -> Dict:
        return {
            "running": self.running,
            "sent_count": self._sent_count,
            "last_sent": self._last_sent_time,
        }

    # ---- 控制 ----
    def start(self, ws):
        """启动主动说话循环。"""
        if self.running:
            self._ws = ws
            return True
        self._ws = ws
        self.running = True
        try:
            self._task = asyncio.create_task(self._loop())
        except RuntimeError:
            self.running = False
            return False
        print(f"[PROACTIVE] 主动说话调度器已启动，私聊目标={config.PROACTIVE_PRIVATE_USER_ID}，群={config.PROACTIVE_GROUP_ID}")
        return True

    def stop(self):
        self.running = False
        if self._task:
            self._task.cancel()
            self._task = None

    def set_ws(self, ws):
        """兼容旧接口：设置 ws。同时尝试从全局总线获取 sender。"""
        self._ws = ws
        try:
            from message_bus import get_sender
            s = get_sender()
            if s is not None:
                self._sender = s
        except Exception as e:
            degrade("libs/qq_bot_runtime/proactive_speaker.py:135 ProactiveSpeaker.set_ws", e, "降级：from message_bus import get_sender")

    def set_sender(self, sender):
        """新接口：注入统一 MessageSender（优先使用，替代裸 ws）。"""
        self._sender = sender

    # ---- 主动说话循环 ----
    async def _loop(self):
        """主循环：每分钟 tick 一次，按概率和状态机决定是否主动说话。"""
        # 配置
        self._warmup_duration = getattr(config, "PROACTIVE_WARMUP_DURATION", 180)   # 3分钟
        self._warmup_prob = getattr(config, "PROACTIVE_WARMUP_PROB", 1.0 / 3.0)     # 1/3
        self._stable_prob = getattr(config, "PROACTIVE_STABLE_PROB", 1.0 / 30.0)    # 1/30
        self._unreplied_limit = getattr(config, "PROACTIVE_UNREPLIED_LIMIT", 3)     # 连续3条不回
        self._silent_duration = getattr(config, "PROACTIVE_SILENT_DURATION", 21600) # 6小时

        # 初始化状态机
        self._state = "warmup"       # warmup / stable / silent
        self._state_start = time.time()
        self._unreplied = 0          # 连续未回复数
        self._last_msg_time = 0.0    # 最近一次主动消息时间
        self._warmup_msg_sent = False  # 高活跃期是否已发过至少1条

        while self.running:
            try:
                self._update_state()   # 同步状态机，不可 await（它是普通方法）
                await self._maybe_send()
                # 转发自主代理的求教请求（直接私聊，不走概率）
                await self._forward_help_requests()
            except Exception as e:
                print(f"[PROACTIVE] 主循环异常: {e}")
            # 每分钟 tick
            await self._sleep_safely(60)

    async def _sleep_safely(self, seconds):
        try:
            await asyncio.sleep(seconds)
        except asyncio.CancelledError:
            raise

    # ---- 状态机 ----
    def _update_state(self):
        """根据时间推进状态机。"""
        now = time.time()
        elapsed = now - self._state_start

        if self._state == "warmup":
            # 3分钟后进入稳定期
            if elapsed >= self._warmup_duration:
                self._state = "stable"
                self._state_start = now
                print(f"[PROACTIVE] 进入稳定期（1/{round(1/self._stable_prob)} 概率）")

        elif self._state == "stable":
            # 连续3条未回复 -> 静默期
            if self._unreplied >= self._unreplied_limit:
                self._state = "silent"
                self._state_start = now
                self._unreplied = 0
                print(f"[PROACTIVE] 连续{self._unreplied_limit}条未回复，进入静默期（停发{self._silent_duration/3600}小时）")

        elif self._state == "silent":
            # 6小时后恢复稳定期
            if elapsed >= self._silent_duration:
                self._state = "stable"
                self._state_start = now
                print(f"[PROACTIVE] 静默期结束，恢复稳定期（1/{round(1/self._stable_prob)} 概率）")

    def _current_prob(self) -> float:
        """当前状态对应的发送概率。"""
        if self._state == "warmup":
            return self._warmup_prob
        return self._stable_prob

    def notify_reply(self):
        """收到目标用户回复时调用，重置未回复计数。"""
        self._unreplied = 0

    # ---- 概率发送 ----
    async def _maybe_send(self):
        """每分钟按概率决定是否主动说话。"""
        if self._state == "silent":
            return  # 静默期不发送

        now = time.time()

        # 是否强制保底发送（前3分钟内至少说1句）
        force = False
        if self._state == "warmup" and not self._warmup_msg_sent:
            elapsed = now - self._state_start
            # 高活跃期只剩最后1分钟时，若还没发过，则强制发1条
            if elapsed >= self._warmup_duration - 60:
                force = True

        # 概率判断（强制保底时跳过概率）
        if not force and random.random() > self._current_prob():
            return

        # 频率兜底：至少间隔 PROACTIVE_PRIVATE_MIN_INTERVAL 秒
        min_interval = getattr(config, "PROACTIVE_PRIVATE_MIN_INTERVAL", 60)
        if now - self._last_msg_time < min_interval:
            return

        # 生成并发送主动消息
        msg = await self._generate_casual_or_game_msg()
        if msg:
            sent = await self._send(msg)
            if sent:
                self._last_msg_time = time.time()
                self._unreplied += 1
                if self._state == "warmup":
                    self._warmup_msg_sent = True

    async def _generate_casual_or_game_msg(self) -> str:
        """生成一条主动消息：根据游戏是否运行，决定内容类型。

        - 游戏在玩：优先真实游戏发现，否则基于真实状态闲聊
        - 游戏没玩：不编造游戏发现，改为主动问游戏相关问题
        """
        # 检测游戏是否在运行（FeiyuAPI 接口是否在线）
        game_up = False
        try:
            from mc_watcher import mc_watcher
            game_up = mc_watcher.is_http_api_up()
        except Exception:
            game_up = False

        if not game_up:
            # 游戏没开：用"问问题"模式，不编造游戏发现
            return await self._generate_no_game_msg()

        # 游戏在玩：先看有没有有趣的游戏发现
        game_msg = await self._try_game_discovery_msg()
        if game_msg:
            return game_msg
        # 否则基于真实状态闲聊
        state = self._fetch_mc_state()
        return await self._generate_message(state, [])

    async def _generate_no_game_msg(self) -> str:
        """游戏没在运行时的主动消息：不编造游戏发现，主动问游戏相关问题。"""
        # 注入实时时间，让 AI 知道当前时间（能说"早上好/晚安"等）
        time_ctx = ""
        try:
            from realtime import format_now
            time_ctx = format_now()
        except Exception as e:
            degrade("libs/qq_bot_runtime/proactive_speaker.py:282 ProactiveSpeaker._generate_no_game_msg", e, "降级：from realtime import format_now")
        
        # 注入屏幕感知上下文（如果启用）
        screen_ctx = ""
        try:
            from screen_awareness import get_screen_awareness
            awareness = get_screen_awareness()
            if awareness.enabled:
                screen_ctx = awareness.get_state_hint()
        except Exception as e:
            degrade("libs/qq_bot_runtime/proactive_speaker.py:292 ProactiveSpeaker._generate_no_game_msg", e, "降级：from screen_awareness import get_screen_awareness")
        
        user_content = f"当前真实时间：{time_ctx}"
        if screen_ctx:
            user_content += f"\n\n{screen_ctx}"
        user_content += "\n\n现在请你主动说一句话（不要任何前缀标记）："
        
        messages = [{"role": "system", "content": _NO_GAME_SYSTEM_PROMPT}]
        # 注入完整记忆库
        messages.extend(self._build_memory_messages())
        messages.append({"role": "user", "content": user_content})
        try:
            msg = await self._llm.chat(
                messages, capability="chat", role="proactive",
                model=getattr(config, "DEEPSEEK_MODEL", None), think=False,
            )
            msg = msg.strip()
            # 去重
            if any(msg == m for m in self._recent_msgs):
                return ""
            return msg
        except Exception as e:
            print(f"[PROACTIVE] 生成无游戏消息失败: {e}")
            return ""

    async def _try_game_discovery_msg(self) -> str:
        """尝试从自主代理获取有趣的游戏发现消息。无则返回空。"""
        try:
            from mc_agent import get_agent
            agent = get_agent()
            if not agent.is_running():
                return ""
            recent = agent.get_recent_actions(limit=6)
            if not recent:
                return ""
            interesting = self._detect_interesting(recent, {})
            if not interesting:
                return ""
            state = self._fetch_mc_state()
            return await self._generate_message(state, recent)
        except Exception:
            return ""

    # ---- 游戏发现检查 ----
    async def _check_game_discovery(self):
        """检查自主代理是否发现了有趣的事，有则主动分享。"""
        try:
            # 只在自主游戏运行时才看游戏发现
            from mc_agent import get_agent
            agent = get_agent()
            if not agent.is_running():
                return
            stats = agent.get_stats()
            round_num = stats.get("rounds", 0)
            if round_num == self._last_agent_round:
                return  # 没有新轮次

            recent = agent.get_recent_actions(limit=6)
            if not recent:
                self._last_agent_round = round_num
                return

            # 计算近期动作的"有趣度"摘要
            interesting = self._detect_interesting(recent, stats)
            if not interesting:
                self._last_agent_round = round_num
                return

            # 防止重复发送同样的内容
            if interesting == self._last_interesting_hash:
                return
            self._last_interesting_hash = interesting
            self._last_agent_round = round_num

            # 生成主动消息并发送
            state = self._fetch_mc_state()
            msg = await self._generate_message(state, recent)
            if msg and self._should_send():
                await self._send(msg)
        except Exception as e:
            print(f"[PROACTIVE] 游戏发现处理异常: {e}")

    async def _forward_help_requests(self):
        """转发自主代理的求教请求（直接私聊主人，不走概率）。"""
        try:
            from mc_agent import get_agent
            agent = get_agent()
            reqs = agent.pop_help_requests()
            if not reqs:
                return
            # 需要 sender 或 ws 才能发送
            if self._sender is None and self._ws is None:
                return
            priv_id = getattr(config, "PROACTIVE_PRIVATE_USER_ID", "")
            if not priv_id:
                return
            for msg in reqs:
                await self._send_private(priv_id, msg)
                print(f"[PROACTIVE] 转发自主代理求教: {msg[:40]}")
        except Exception as e:
            print(f"[PROACTIVE] 转发求教失败: {e}")

    def _detect_interesting(self, recent: List[str], stats: Dict) -> str:
        """从最近动作里检测"有趣发现"。返回一个去重用的哈希字符串，无趣则返回空。"""
        # 检查有没有关键资源/事件
        keywords_interesting = ["diamond", "钻石", "村庄", "village", "怪物", "zombie", "creeper",
                                "骷髅", "skeleton", "铁矿", "iron", "箱子", "chest", "洞穴", "cave",
                                "岩浆", "lava", "远古", "ancient", "金矿", "gold"]
        joined = " ".join(recent)
        hits = [k for k in keywords_interesting if k.lower() in joined.lower()]
        if hits:
            return "|".join(hits[:5])
        return ""

    def _fetch_mc_state(self) -> Dict:
        try:
            from mc_watcher import mc_watcher
            return mc_watcher.fetch_state() or {}
        except Exception:
            return {}

    # ---- 注入完整记忆库（统一走 memory_context，与所有场景一致）----
    def _build_memory_messages(self) -> list:
        """构建完整记忆库的 system 消息（统一模块）。"""
        try:
            import config as cfg
            target_user = getattr(cfg, "PROACTIVE_PRIVATE_USER_ID", "")
            from memory_context import build_memory_messages
            return build_memory_messages(target_user)
        except Exception as e:
            print(f"[PROACTIVE] 记忆注入失败: {e}")
            return []

    # ---- 主动生成消息 ----
    async def _generate_message(self, state: Dict, recent: List[str]) -> str:
        """调用 DeepSeek 生成一条主动分享的消息。"""
        # 汇总当前状态
        situation = self._summarize_state(state, recent)
        # 注入实时时间
        time_ctx = ""
        try:
            from realtime import format_now
            time_ctx = format_now()
        except Exception as e:
            degrade("libs/qq_bot_runtime/proactive_speaker.py:436 ProactiveSpeaker._generate_message", e, "降级：from realtime import format_now")
        # 注入完整记忆库（人物档案/重要信息/AI自我认知/历史）
        memory_msgs = self._build_memory_messages()
        messages = [{"role": "system", "content": _ACTIVE_SYSTEM_PROMPT}]
        messages.extend(memory_msgs)
        messages.append({"role": "user", "content": f"当前真实时间：{time_ctx}\n当前游戏状态：\n{situation}\n\n现在请你主动说一句话（不要任何前缀标记）："})
        try:
            msg = await self._llm.chat(
                messages, capability="chat", role="proactive",
                model=getattr(config, "DEEPSEEK_MODEL", None), think=False,
            )
            msg = msg.strip()
            # 去重
            if any(msg == m for m in self._recent_msgs):
                return ""
            return msg
        except Exception as e:
            print(f"[PROACTIVE] 生成消息失败: {e}")
            return ""

    def _summarize_state(self, state: Dict, recent: List[str]) -> str:
        """把状态压缩成给 AI 看的摘要。"""
        p = state.get("player") or {}
        w = state.get("world") or {}
        lines = []
        if p:
            lines.append(f"位置X={p.get('x','?')} Y={p.get('y','?')} Z={p.get('z','?')}，血量{p.get('health','?')}/{p.get('max_health','?')}，等级{p.get('level','?')}")
        if w:
            lines.append(f"第{w.get('day','?')}天，时间{w.get('time_of_day','?')}tick")
        entities = state.get("entities") or []
        if entities:
            names = ", ".join(e.get("type", "").split(":")[-1] for e in entities[:5])
            lines.append(f"附近有：{names}")
        blocks = state.get("valuable_blocks") or []
        if blocks:
            b = ", ".join(f"{x.get('block','?')}x{x.get('count',0)}" for x in blocks[:4])
            lines.append(f"附近资源：{b}")
        if recent:
            lines.append("我最近在：" + "；".join(recent[-3:]))
        return "\n".join(lines)

    # ---- 发送控制 ----
    def _should_send(self) -> bool:
        """发送频率控制：私聊和群聊各自限制。"""
        now = time.time()
        priv_interval = getattr(config, "PROACTIVE_PRIVATE_MIN_INTERVAL", 60)
        group_interval = getattr(config, "PROACTIVE_GROUP_MIN_INTERVAL", 300)
        ok_private = (now - self._last_private_sent) >= priv_interval
        ok_group = (now - self._last_group_sent) >= group_interval
        return ok_private or ok_group

    async def _send(self, msg: str) -> bool:
        """发送消息：私聊你 + 指定群。返回是否成功发送了私聊（供未回复计数）。"""
        if not self._ws:
            return False
        # 记录已发送
        self._recent_msgs.append(msg)
        if len(self._recent_msgs) > self._max_recent:
            self._recent_msgs = self._recent_msgs[-self._max_recent:]
        self._sent_count += 1
        self._last_sent_time = time.time()
        now = time.time()
        sent_private = False

        # 私聊
        priv_id = getattr(config, "PROACTIVE_PRIVATE_USER_ID", "")
        if priv_id:
            try:
                await self._send_private(priv_id, msg)
                self._last_private_sent = now
                sent_private = True
            except Exception as e:
                print(f"[PROACTIVE] 私聊发送失败: {e}")

        # 群聊（PROACTIVE_GROUP_ID 为空则只私聊）
        group_id = getattr(config, "PROACTIVE_GROUP_ID", "")
        if group_id and (now - self._last_group_sent) >= getattr(config, "PROACTIVE_GROUP_MIN_INTERVAL", 300):
            try:
                await self._send_group(group_id, msg)
                self._last_group_sent = now
            except Exception as e:
                print(f"[PROACTIVE] 群聊发送失败: {e}")

        return sent_private

    async def _send_private(self, user_id, msg: str):
        # 优先用统一 sender（解耦），否则回退到裸 ws（兼容）
        if self._sender is not None:
            return await self._sender.send_private(user_id, msg)
        if self._ws is None:
            return False
        payload = {
            "action": "send_msg",
            "params": {
                "message_type": "private",
                "user_id": int(user_id) if str(user_id).isdigit() else user_id,
                "message": [{"type": "text", "data": {"text": msg}}],
            },
            "echo": f"proactive-private-{time.time()}",
        }
        await self._ws.send(json.dumps(payload))
        return True

    async def _send_group(self, group_id, msg: str):
        # 优先用统一 sender（解耦），否则回退到裸 ws（兼容）
        if self._sender is not None:
            return await self._sender.send_group(group_id, msg)
        if self._ws is None:
            return False
        payload = {
            "action": "send_msg",
            "params": {
                "message_type": "group",
                "group_id": int(group_id) if str(group_id).isdigit() else group_id,
                "message": [{"type": "text", "data": {"text": msg}}],
            },
            "echo": f"proactive-group-{time.time()}",
        }
        await self._ws.send(json.dumps(payload))
        return True

    # ---- 对外：手动触发一次闲聊 ----
    async def trigger_casual(self):
        """手动触发一次主动闲聊（定时/命令用）。"""
        state = self._fetch_mc_state()
        msg = await self._generate_message(state, [])
        if msg:
            await self._send(msg)
        return msg


speaker = get_speaker()
