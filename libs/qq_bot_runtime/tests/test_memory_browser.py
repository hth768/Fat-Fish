# -*- coding: utf-8 -*-
"""记忆浏览器（/api/memory_browser）后端契约测试。

通过 monkeypatch 各记忆模块的 _*_file() 将数据重定向到临时目录，
避免污染真实记忆数据。
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import reflection_memory as rm
import persona_memory as pm
import important_notes as inm

_TMP = tempfile.mkdtemp(prefix="mem_browser_test_")
rm._reflection_file = lambda: os.path.join(_TMP, "reflection_data.json")
pm._persona_file = lambda: os.path.join(_TMP, "persona_data.json")
inm._notes_file = lambda: os.path.join(_TMP, "important_notes.json")


class MemoryBrowserTests(unittest.TestCase):
    def setUp(self):
        for fn in ("reflection_data.json", "persona_data.json", "important_notes.json"):
            fp = os.path.join(_TMP, fn)
            if os.path.exists(fp):
                os.remove(fp)
        rm._reflection_cache = None
        pm._persona_cache = None

    def test_reflection_roundtrip(self):
        rm._add_reflection({"type": "preference", "user_id": "u1", "content": "喜欢简短回复"})
        self.assertEqual(len(rm.get_reflections()), 1)
        self.assertEqual(rm.delete_reflection(content="喜欢简短回复"), 1)
        self.assertEqual(rm.get_reflections(), [])

    def test_rules_roundtrip(self):
        rm._add_interaction_rule({"rule": "深夜不要用长文"})
        self.assertEqual(len(rm.get_interaction_rules()), 1)
        rm.clear_interaction_rules()
        self.assertEqual(rm.get_interaction_rules(), [])
        rm._add_interaction_rule({"rule": "abc"})
        self.assertEqual(rm.delete_interaction_rule(rule="abc"), 1)

    def test_persona_roundtrip(self):
        pm.update_persona("u2", ["爱开玩笑"], "轻松")
        self.assertIn("u2", pm.get_all_personas())
        self.assertEqual(pm.delete_persona("u2"), 1)
        self.assertNotIn("u2", pm.get_all_personas())

    def test_notes_roundtrip(self):
        inm.add_note("u3", "买牛奶", "todo")
        notes = inm.load_notes().get("u3", [])
        self.assertEqual(len(notes), 1)
        self.assertEqual(inm.delete_note("u3", index=1), 1)
        inm.add_note("u3", "x")
        self.assertGreaterEqual(inm.clear_notes("u3"), 1)

    def test_api_memory_browser_get(self):
        try:
            import web_plugin as wp
        except Exception as e:
            self.skipTest("web_plugin 不可用: " + str(e))
            return
        rm._add_reflection({"type": "style", "user_id": "u9", "content": "用中文"})
        d = wp._api_memory_browser()
        self.assertTrue(d["available"]["reflection"])
        self.assertIsInstance(d["reflections"], list)
        self.assertGreaterEqual(len(d["reflections"]), 1)
        self.assertIn("user_id", d["reflections"][0])
        self.assertIn("total_reflections", d["stats"])

    def test_api_memory_browser_actions(self):
        try:
            import web_plugin as wp
        except Exception as e:
            self.skipTest("web_plugin 不可用: " + str(e))
            return
        rm._add_reflection({"type": "pattern", "user_id": "uA", "content": "总问天气"})
        r = wp._api_memory_browser_action({"action": "delete_reflection", "content": "总问天气"})
        self.assertTrue(r.get("ok"))

        rm._add_interaction_rule({"rule": "x"})
        r = wp._api_memory_browser_action({"action": "clear_rules"})
        self.assertTrue(r.get("ok"))
        self.assertEqual(rm.get_interaction_rules(), [])

        pm.update_persona("uB", ["t"], "s")
        r = wp._api_memory_browser_action({"action": "delete_persona", "user_id": "uB"})
        self.assertTrue(r.get("ok"))

        inm.add_note("uC", "note1")
        r = wp._api_memory_browser_action({"action": "delete_note", "user_id": "uC", "index": 1})
        self.assertTrue(r.get("ok"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
