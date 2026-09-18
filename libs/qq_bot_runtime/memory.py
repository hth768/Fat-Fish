# -*- coding: utf-8 -*-
"""多轮对话记忆 + 会话管理（三级记忆：短期 + 摘要 + 按用户统一记忆 + 持久化）。

本模块已并入原 session_manager.py 的会话能力。合并动因：两者各缺一半——
    Memory         有持久化（memory_data.json + 防抖 + atexit 刷盘），但无会话切换；
    SessionManager 有会话切换（按 user_id 多会话 / 预热 / 热切换），但 Session 无持久化。
现让 Session 继承 Memory，从而同时具备「持久化 + 会话切换」：
    Memory                 —— 持久化三级记忆底座
    Session(Memory)        —— 持久化会话（原 Session 的全部方法签名保持不变）
    SessionManager         —— 按 user_id 的会话注册表 + 预热 + 热切换
    SessionManagerAdapter  —— ChatService.memory 接口（与 Memory 同构）
    get_session_manager()  —— 全局单例


- 短期：最近 MAX_HISTORY 条原始对话，直接给 AI
- 摘要：超出窗口的历史压缩成摘要，长期保留
- 记忆按 user_id 统一，私聊和群聊共享同一个人的记忆（跨场景互通）
- 持久化：记忆保存到磁盘 JSON 文件，重启后不丢失
"""
import asyncio
import atexit
import json
import os
import threading
import time
from collections import defaultdict, deque
from typing import Dict, Optional

import config
import agent_ctx
from quiet import attention

# 落盘防抖间隔（秒）：一次聊天回合会触发多次写入（user/assistant/topic），
# 合并成最多每 2 秒一次全量写，避免每条消息都重写整个记忆文件
_SAVE_DEBOUNCE = 2.0


class Memory:
    def __init__(self):
        # key -> deque 短期记忆（原始对话）
        self._store = defaultdict(lambda: deque(maxlen=config.MAX_HISTORY))
        # key -> str 摘要记忆（超窗口历史压缩后的摘要）
        self._summary = defaultdict(str)
        # key -> str 当前话题（用于话题切换检测）
        self._topic = defaultdict(str)
        # 持久化文件路径
        self._file = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "memory_data.json"
        )
        # 防抖状态
        self._dirty = False
        self._last_save = 0.0
        self.load()
        # 进程退出时把未落盘的记忆强制写回
        atexit.register(self._atexit_flush)

    def _atexit_flush(self):
        try:
            if self._dirty:
                self._flush()
        except Exception as e:
            degrade("libs/qq_bot_runtime/memory.py:60 Memory._atexit_flush", e, "降级：if self._dirty")

    def _key(self, message_type: str, group_id, user_id) -> str:
        # 统一按「用户」记忆：私聊和群聊共享同一个人的记忆，跨场景互通
        return f"user:{user_id}"

    # ---------------- 持久化 ----------------
    def save(self, force: bool = False):
        """落盘（防抖）。force=True 跳过防抖立即写；普通写入 2 秒内合并。"""
        now = time.time()
        if not force and now - self._last_save < _SAVE_DEBOUNCE:
            self._dirty = True
            return
        self._flush()

    def _flush(self):
        """把记忆保存到磁盘 JSON 文件。"""
        try:
            data = {
                "store": {k: list(v) for k, v in self._store.items() if v},
                "summary": {k: v for k, v in self._summary.items() if v},
                "topic": {k: v for k, v in self._topic.items() if v},
            }
            with open(self._file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            self._dirty = False
            self._last_save = time.time()
        except OSError as e:
            print(f"[WARN] 记忆保存失败: {e}")

    def load(self):
        """从磁盘加载记忆。"""
        if not os.path.exists(self._file):
            return
        try:
            with open(self._file, "r", encoding="utf-8") as f:
                data = json.load(f)
            for k, v in data.get("store", {}).items():
                self._store[k] = deque(v, maxlen=config.MAX_HISTORY)
            for k, v in data.get("summary", {}).items():
                self._summary[k] = v
            for k, v in data.get("topic", {}).items():
                self._topic[k] = v
            print(f"[INFO] 记忆已加载: {len(self._store)} 个用户")
        except (json.JSONDecodeError, OSError) as e:
            print(f"[WARN] 记忆加载失败: {e}")

    # ---------------- 查询 ----------------
    def get(self, message_type: str, group_id, user_id) -> list[dict]:
        """获取完整上下文：system + 摘要 + 短期历史。"""
        key = self._key(message_type, group_id, user_id)
        history = [{"role": "system", "content": config.SYSTEM_PROMPT}]

        # 摘要记忆（旧对话的压缩）
        if self._summary[key]:
            history.append({
                "role": "system",
                "content": f"【之前的对话摘要】{self._summary[key]}"
            })

        history.extend(self._store[key])
        return history

    # ---------------- 写入 ----------------
    def add(self, message_type: str, group_id, user_id, role: str, content: str):
        """添加一条对话。"""
        key = self._key(message_type, group_id, user_id)
        self._store[key].append({"role": role, "content": content})
        self.save()

    def is_full(self, message_type: str, group_id, user_id) -> bool:
        """短期记忆是否已满（达到 maxlen）。"""
        key = self._key(message_type, group_id, user_id)
        return len(self._store[key]) >= config.MAX_HISTORY

    def overflow_items(self, message_type: str, group_id, user_id) -> list[dict]:
        """取出需要压缩进摘要的旧条目（前一半），并从短期记忆移除。"""
        key = self._key(message_type, group_id, user_id)
        items = list(self._store[key])
        if len(items) <= config.MAX_HISTORY // 2:
            return []

        # 取出前一半（最旧的），保留后一半
        overflow = items[:len(items) - config.MAX_HISTORY // 2]
        remaining = items[len(items) - config.MAX_HISTORY // 2:]
        self._store[key] = deque(remaining, maxlen=config.MAX_HISTORY)
        self.save()
        return overflow

    def overflow_items_force(self, message_type: str, group_id, user_id) -> list[dict]:
        """强制取出全部短期记忆条目（用于话题切换时压缩），并清空。"""
        key = self._key(message_type, group_id, user_id)
        items = list(self._store[key])
        self._store[key].clear()
        self.save()
        return items

    def set_summary(self, message_type: str, group_id, user_id, summary: str):
        """设置摘要记忆。"""
        key = self._key(message_type, group_id, user_id)
        self._summary[key] = summary
        self.save()

    def get_summary(self, message_type: str, group_id, user_id) -> str:
        key = self._key(message_type, group_id, user_id)
        return self._summary[key]

    def set_topic(self, message_type: str, group_id, user_id, topic: str):
        key = self._key(message_type, group_id, user_id)
        self._topic[key] = topic
        self.save()

    def get_topic(self, message_type: str, group_id, user_id) -> str:
        key = self._key(message_type, group_id, user_id)
        return self._topic[key]

    def clear_short_term(self, message_type: str, group_id, user_id):
        """清空短期记忆（话题切换时用），摘要保留。"""
        key = self._key(message_type, group_id, user_id)
        self._store[key].clear()
        self.save()


# ======================================================================
# 会话管理（原 session_manager.py 并入）
# ----------------------------------------------------------------------
# 原 Session 与 Memory 的读写方法逐方法同名同义，唯一区别是 Session 无持久化。
# 因此让 Session 继承 Memory：既保留原签名，又获得落盘能力（重启后历史不丢）。
# SessionManager / SessionManagerAdapter / get_session_manager 行为完全不变。
# ======================================================================

def _has_running_loop() -> bool:
    """是否处于运行中的事件循环（决定能否安全 create_task）。"""
    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False


class Session(Memory):
    """单个会话：= Memory 的持久化三级记忆 + 会话元信息 + 预热状态。

    继承 Memory 后自动获得落盘（memory_data.json）与全部读写方法；
    本类只补「会话标识 / 预热」两件事，读写方法签名与原 Session 完全一致。
    """

    def __init__(self, session_id: str, persistent: bool = True, agent_id: str = None, **kw):
        # 先建结构、设好会话专属落盘路径，再触发加载（避免先读全局文件）
        self.session_id = session_id
        self.agent_id = agent_id
        self._store = defaultdict(lambda: deque(maxlen=config.MAX_HISTORY))
        self._summary = defaultdict(str)
        self._topic = defaultdict(str)
        self._dirty = False
        self._last_save = 0.0
        if persistent:
            safe = "".join(ch if (ch.isalnum() or ch in "-_") else "_"
                           for ch in str(session_id))
            self._file = os.path.join(
                agent_ctx.agent_storage_dir(os.path.dirname(os.path.abspath(__file__)), agent_id),
                f"memory_session_{safe}.json")
        else:
            self._file = os.path.join(
                os.path.dirname(os.path.abspath(__file__)), "memory_data.json")
        self.load()
        atexit.register(self._atexit_flush)
        self._created_at = time.time()
        self._warmed_up = False
        self._warm_up_event = asyncio.Event()

    # ---- 原 Session 的读写方法（签名与语义保持不变）----
    def add(self, message_type: str, group_id, user_id, role: str, content: str):
        """添加一条对话（委托 Memory.add，自动触发防抖落盘）。"""
        super().add(message_type, group_id, user_id, role, content)

    # ---- 会话元信息 ----
    def info(self) -> dict:
        return {
            "session_id": self.session_id,
            "created_at": self._created_at,
            "warmed_up": self._warmed_up,
            "user_count": len(self._store),
        }

    # ---- 预热（原 Session 能力，保留）----
    def warm_up(self):
        """预加载会话（预留：可在此预热模型/回放历史）。"""
        time.sleep(0.1)
        self._warmed_up = True
        self._warm_up_event.set()

    async def wait_for_warm_up(self):
        """等待预热完成。"""
        if not self._warmed_up:
            await self._warm_up_event.wait()

    def is_warmed_up(self) -> bool:
        return self._warmed_up


class SessionManager:
    """会话管理器（支持热切换）—— 行为与原 session_manager.SessionManager 一致。"""

    def __init__(self, agent_id: str = "feiyu"):
        self.agent_id = agent_id
        self._sessions: Dict[str, Session] = {}
        self._current_session_id: Dict[str, str] = {}   # user_id -> session_id
        self._next_session_id: Dict[str, str] = {}      # user_id -> next_session_id
        self._lock = threading.RLock()
        self._session_counter = 0

    def _generate_session_id(self, user_id: str) -> str:
        """生成新的会话 ID。"""
        with self._lock:
            self._session_counter += 1
            return f"session_{user_id}_{self._session_counter}_{int(time.time())}"

    def get_current_session(self, user_id: str) -> Session:
        """获取当前会话（不存在则创建并异步预热）。"""
        user_id = str(user_id)

        with self._lock:
            if user_id not in self._current_session_id:
                session_id = self._generate_session_id(user_id)
                session = Session(session_id, agent_id=self.agent_id)
                self._sessions[session_id] = session
                self._current_session_id[user_id] = session_id
                if _has_running_loop():
                    asyncio.create_task(asyncio.to_thread(session.warm_up))
                else:
                    # 无运行中的事件循环（如脚本/测试）：同步预热
                    session.warm_up()

            session_id = self._current_session_id[user_id]
            return self._sessions[session_id]

    def prepare_next_session(self, user_id: str):
        """预热下一个会话（后台准备）。"""
        user_id = str(user_id)

        with self._lock:
            if user_id in self._next_session_id:
                return

            session_id = self._generate_session_id(user_id)
            session = Session(session_id, agent_id=self.agent_id)
            self._sessions[session_id] = session
            self._next_session_id[user_id] = session_id

            if _has_running_loop():
                asyncio.create_task(asyncio.to_thread(session.warm_up))
            else:
                session.warm_up()
            print(f"[SESSION] 开始预热新会话: {session_id}")

    async def hot_swap(self, user_id: str):
        """热切换会话（等待预热后原子切换）。"""
        user_id = str(user_id)

        with self._lock:
            if user_id not in self._next_session_id:
                print("[SESSION] 没有待切换的会话")
                return

            next_session_id = self._next_session_id[user_id]
            next_session = self._sessions[next_session_id]

        await next_session.wait_for_warm_up()
        print(f"[SESSION] 会话 {next_session_id} 预热完成")

        with self._lock:
            old_session_id = self._current_session_id.get(user_id)
            self._current_session_id[user_id] = next_session_id
            del self._next_session_id[user_id]

            if old_session_id and old_session_id in self._sessions:
                if len(self._sessions) > 3:
                    del self._sessions[old_session_id]
                    print(f"[SESSION] 清理旧会话: {old_session_id}")

        print(f"[SESSION] 热切换完成: {old_session_id} -> {next_session_id}")

    def should_swap(self, user_id: str, message_type: str, group_id,
                    user_id_for_check: str) -> bool:
        """检查是否应该切换会话（当前会话短期记忆已满）。"""
        user_id = str(user_id)

        try:
            session = self.get_current_session(user_id)
            return session.is_full(message_type, group_id, user_id_for_check)
        except Exception as e:
            print(f"[SESSION] 检查切换条件失败: {e}")
            return False

    def get_session_stats(self) -> dict:
        """获取会话统计信息。"""
        with self._lock:
            return {
                "total_sessions": len(self._sessions),
                "active_users": len(self._current_session_id),
                "preparing_sessions": len(self._next_session_id),
            }


# 全局单例
_session_managers: Dict[str, SessionManager] = {}


def get_session_manager(agent_id: str = None) -> SessionManager:
    """获取（按 agent 隔离的）会话管理器单例。

    agent_id 省略时回落到默认 "feiyu"（既有主 bot），保持与旧调用兼容。
    不同 bot 拥有完全独立的会话集合与落盘文件。
    """
    aid = agent_id or "feiyu"
    mgr = _session_managers.get(aid)
    if mgr is None:
        mgr = SessionManager(agent_id=aid)
        _session_managers[aid] = mgr
    return mgr


class SessionManagerAdapter:
    """SessionManager 适配器，提供与 Memory 类相同的 API。"""

    def __init__(self, agent_id: str = "feiyu"):
        self.agent_id = agent_id
        self._manager = get_session_manager(agent_id)

    def add(self, message_type: str, group_id, user_id, role: str, content: str):
        """添加一条对话。"""
        session = self._manager.get_current_session(str(user_id))
        session.add(message_type, group_id, user_id, role, content)

    def get(self, message_type: str, group_id, user_id) -> list:
        """获取完整上下文。"""
        session = self._manager.get_current_session(str(user_id))
        return session.get(message_type, group_id, user_id)

    def is_full(self, message_type: str, group_id, user_id) -> bool:
        """短期记忆是否已满。"""
        session = self._manager.get_current_session(str(user_id))
        return session.is_full(message_type, group_id, user_id)

    def overflow_items(self, message_type: str, group_id, user_id) -> list:
        """取出需要压缩进摘要的旧条目。"""
        session = self._manager.get_current_session(str(user_id))
        return session.overflow_items(message_type, group_id, user_id)

    def overflow_items_force(self, message_type: str, group_id, user_id) -> list:
        """强制取出所有条目（用于话题切换）。"""
        session = self._manager.get_current_session(str(user_id))
        return session.overflow_items_force(message_type, group_id, user_id)

    def set_summary(self, message_type: str, group_id, user_id, summary: str):
        """设置摘要记忆。"""
        session = self._manager.get_current_session(str(user_id))
        session.set_summary(message_type, group_id, user_id, summary)

    def get_summary(self, message_type: str, group_id, user_id) -> str:
        """获取摘要记忆。"""
        session = self._manager.get_current_session(str(user_id))
        return session.get_summary(message_type, group_id, user_id)

    def set_topic(self, message_type: str, group_id, user_id, topic: str):
        """设置当前话题。"""
        session = self._manager.get_current_session(str(user_id))
        session.set_topic(message_type, group_id, user_id, topic)

    def get_topic(self, message_type: str, group_id, user_id) -> str:
        """获取当前话题。"""
        session = self._manager.get_current_session(str(user_id))
        return session.get_topic(message_type, group_id, user_id)

    def clear_short_term(self, message_type: str, group_id, user_id):
        """清空短期记忆。"""
        session = self._manager.get_current_session(str(user_id))
        session.clear_short_term(message_type, group_id, user_id)

    async def prepare_next_session(self, user_id):
        """预热下一个会话。"""
        self._manager.prepare_next_session(str(user_id))

    async def hot_swap(self, user_id):
        """热切换会话。"""
        await self._manager.hot_swap(str(user_id))

    def get_stats(self) -> dict:
        """获取会话统计信息。"""
        return self._manager.get_session_stats()

    # ---- 新增：持久化入口（Session 合并后具备落盘能力，此处暴露给上层）----
    def save(self, force: bool = True):
        """把所有活动会话刷盘（默认强制，跳过防抖）。"""
        saved = 0
        for sess in self._manager._sessions.values():
            try:
                sess.save(force=force)
                saved += 1
            except Exception as e:
                attention("memory.SessionManagerAdapter.save", e, "会话落盘失败（可能丢聊天记录）")
        return saved
