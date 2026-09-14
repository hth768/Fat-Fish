# -*- coding: utf-8 -*-
"""记忆 + 会话合并（session_manager -> memory）回归测试。

守护要点：
  1. Session 继承 Memory，从而同时具备「持久化 + 会话切换」；
  2. 原 session_manager 对外契约（SessionManager / SessionManagerAdapter）签名零漂移；
  3. 读写/溢出/摘要/话题/预热等行为与合并前等价；
  4. session_manager.py 垫片仍可正常 re-export；
  5. 无事件循环时预热不产生未 await 协程（降级同步）。
"""
import asyncio
import inspect
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config

config.MAX_HISTORY = 6
config.SYSTEM_PROMPT = "SYS"


class MemorySessionContractTest(unittest.TestCase):
    def test_session_inherits_memory(self):
        """Session 必须继承 Memory（这是「获得持久化」的核心）。"""
        from memory import Memory, Session
        self.assertTrue(issubclass(Session, Memory))

    def test_session_has_read_write_methods(self):
        """原 Session 的读写方法在合并后仍可调用（由 Memory 提供）。"""
        from memory import Session
        for name in ("add", "get", "is_full", "overflow_items", "overflow_items_force",
                     "set_summary", "get_summary", "set_topic", "get_topic",
                     "clear_short_term", "warm_up", "is_warmed_up"):
            self.assertTrue(hasattr(Session, name), f"Session 缺少 {name}")

    def test_session_add_signature_unchanged(self):
        """add 签名必须保持 (message_type, group_id, user_id, role, content)。"""
        from memory import Session
        params = list(inspect.signature(Session.add).parameters)[1:]
        self.assertEqual(params, ["message_type", "group_id", "user_id", "role", "content"])

    def test_adapter_async_methods(self):
        """热切换相关方法必须仍是 async（原契约）。"""
        from memory import SessionManagerAdapter
        self.assertTrue(inspect.iscoroutinefunction(SessionManagerAdapter.hot_swap))
        self.assertTrue(inspect.iscoroutinefunction(
            SessionManagerAdapter.prepare_next_session))

    def test_stats_fields_match_original(self):
        from memory import SessionManagerAdapter
        st = SessionManagerAdapter().get_stats()
        self.assertEqual(set(st), {"total_sessions", "active_users", "preparing_sessions"})

    def test_shim_reexports(self):
        """session_manager.py 垫片仍导出全部原有符号。"""
        from session_manager import (Session, SessionManager,            # noqa: F401
                                     SessionManagerAdapter, get_session_manager)
        from memory import Session as MSession
        self.assertIs(Session, MSession)

    def test_shared_singleton(self):
        from memory import get_session_manager
        self.assertIs(get_session_manager(), get_session_manager())


class MemoryBehaviorTest(unittest.TestCase):
    def setUp(self):
        from memory import Session
        self.tmp = os.path.join(os.environ.get("TEMP", "."), "_t_mem_session.json")
        if os.path.exists(self.tmp):
            os.remove(self.tmp)
        self.s = Session("unit_test")
        # 隔离：切到临时文件并清空，避免污染真实 memory_data.json
        self.s._file = self.tmp
        self.s._store.clear()
        self.s._summary.clear()
        self.s._topic.clear()

    def tearDown(self):
        if os.path.exists(self.tmp):
            os.remove(self.tmp)

    def test_add_get_returns_system_head(self):
        self.s.add("private", None, "u", "user", "hi")
        ctx = self.s.get("private", None, "u")
        self.assertEqual(ctx[0]["content"], "SYS")
        self.assertEqual(len(ctx), 2)

    def test_short_term_respects_maxlen(self):
        for i in range(config.MAX_HISTORY + 4):
            self.s.add("private", None, "u", "user", f"m{i}")
        ctx = self.s.get("private", None, "u")
        self.assertEqual(len(ctx) - 1, config.MAX_HISTORY)

    def test_overflow_moves_half_out(self):
        for i in range(config.MAX_HISTORY + 2):
            self.s.add("private", None, "u", "user", f"m{i}")
        self.assertTrue(self.s.is_full("private", None, "u"))
        ov = self.s.overflow_items("private", None, "u")
        self.assertGreater(len(ov), 0)
        self.assertEqual(len(self.s.get("private", None, "u")) - 1,
                         config.MAX_HISTORY // 2)

    def test_summary_injected_into_context(self):
        self.s.set_summary("private", None, "u", "SUMMARY")
        self.assertEqual(self.s.get_summary("private", None, "u"), "SUMMARY")
        ctx = self.s.get("private", None, "u")
        self.assertTrue(any("SUMMARY" in (m.get("content") or "") for m in ctx))

    def test_topic_roundtrip(self):
        self.s.set_topic("private", None, "u", "T")
        self.assertEqual(self.s.get_topic("private", None, "u"), "T")

    def test_clear_short_term_keeps_summary(self):
        self.s.add("private", None, "u", "user", "a")
        self.s.set_summary("private", None, "u", "SUM")
        self.s.clear_short_term("private", None, "u")
        ctx = self.s.get("private", None, "u")
        self.assertEqual(len(ctx), 2)             # system + 摘要
        self.assertIn("SUM", ctx[1]["content"])

    def test_persistence_roundtrip(self):
        """合并带来的新能力：会话可落盘并按需加载。"""
        self.s.add("private", None, "u", "user", "persist_me")
        self.s.save(force=True)
        self.assertTrue(os.path.exists(self.tmp))
        from memory import Session
        s2 = Session("unit_test2")
        s2._file = self.tmp
        s2.load()
        self.assertTrue(any(m.get("content") == "persist_me"
                            for m in s2.get("private", None, "u")))

    def test_warm_up_without_event_loop(self):
        """无事件循环时预热应同步完成，且不留未 await 协程。"""
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("error", RuntimeWarning)
            self.s.warm_up()
        self.assertTrue(self.s.is_warmed_up())

    def test_manager_get_current_session_stable(self):
        """同一 user_id 多次获取应为同一会话。"""
        from memory import SessionManager
        m = SessionManager()
        a = m.get_current_session("u1")
        b = m.get_current_session("u1")
        self.assertIs(a, b)

    def test_adapter_save_returns_count(self):
        from memory import SessionManagerAdapter
        ad = SessionManagerAdapter()
        ad.add("private", None, "save_u", "user", "x")
        self.assertGreaterEqual(ad.save(force=True), 1)

    def test_hot_swap_flow(self):
        """预热 -> 切换：切换后活动会话应变更。

        注意 should_swap 的语义是「当前会话短期记忆是否已满」，故需先填满。
        """
        from memory import SessionManager
        m = SessionManager()
        old = m.get_current_session("u9")
        for i in range(config.MAX_HISTORY):
            old.add("private", None, "u9", "user", f"m{i}")
        self.assertTrue(m.should_swap("u9", "private", None, "u9"))

        m.prepare_next_session("u9")

        async def go():
            await m.hot_swap("u9")

        asyncio.run(go())
        self.assertIsNot(m.get_current_session("u9"), old)
        # 切换后新会话是空的（记忆按会话隔离）
        self.assertEqual(len(m.get_current_session("u9").get("private", None, "u9")), 1)


if __name__ == "__main__":
    unittest.main()
