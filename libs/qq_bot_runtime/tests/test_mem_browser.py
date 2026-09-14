# -*- coding: utf-8 -*-
"""记忆浏览 Web 面板：聚合读取结构 + 校对（删除/清空）动作测试。"""
import importlib
import json
import os
import sys
import tempfile
import unittest

import web_plugin
import config


class WebMemoryBrowserTest(unittest.TestCase):
    def setUp(self):
        self.wp = web_plugin.WebPlugin(core=None)

    def test_browser_structure(self):
        d = self.wp._api_memory_browser()
        for k in ("reflections", "rules", "personas", "notes", "stats", "available"):
            self.assertIn(k, d)
        self.assertIsInstance(d["reflections"], list)
        self.assertIsInstance(d["available"], dict)

    def test_unknown_action_returns_false(self):
        r = self.wp._api_memory_browser_action({"action": "nope"})
        self.assertFalse(r.get("ok"))


class ReflectionDeleteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.tf = os.path.join(self.tmp, "refl.json")
        import reflection_memory as rm
        self.rm = rm
        self._orig_file = rm._reflection_file
        self._orig_cache = rm._reflection_cache
        rm._reflection_file = lambda: self.tf
        rm._reflection_cache = None

    def tearDown(self):
        self.rm._reflection_file = self._orig_file
        self.rm._reflection_cache = self._orig_cache

    def test_add_get_delete_reflection(self):
        self.rm._add_reflection({"type": "preference", "user_id": "u1",
                                 "content": "likes short", "ts": 111.0,
                                 "evidence": "x", "confidence": 0.8,
                                 "actionable": True})
        self.rm._add_reflection({"type": "style", "user_id": "u1",
                                 "content": "uses emoji", "ts": 222.0})
        self.assertEqual(len(self.rm.get_reflections()), 2)
        n = self.rm.delete_reflection(ts=111.0)
        self.assertEqual(n, 1)
        self.assertEqual(len(self.rm.get_reflections()), 1)
        self.rm.clear_reflections()
        self.assertEqual(len(self.rm.get_reflections()), 0)

    def test_delete_interaction_rule(self):
        self.rm._add_interaction_rule({"rule": "user likes nickname", "confidence": 0.6})
        self.assertEqual(len(self.rm.get_interaction_rules()), 1)
        n = self.rm.delete_interaction_rule(rule="user likes nickname")
        self.assertEqual(n, 1)
        self.assertEqual(len(self.rm.get_interaction_rules()), 0)


if __name__ == "__main__":
    unittest.main()
