# -*- coding: utf-8 -*-
"""Web 插件管理 API 测试：/api/plugins 聚合 + /api/plugins/control 守卫。"""
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import web_plugin
import launcher
from launcher_core.runtime import Launcher, SidecarSpec


class PluginsApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.plugin = web_plugin.WebPlugin(core=None)

    def tearDown(self):
        launcher.ACTIVE_LAUNCHER = None

    def test_api_plugins_without_launcher(self):
        launcher.ACTIVE_LAUNCHER = None
        d = self.plugin._api_plugins()
        self.assertFalse(d["launcher"])
        self.assertIsInstance(d["toggles"], list)
        keys = {t["key"] for t in d["toggles"]}
        self.assertIn("ENABLE_MEMORY_SERVER", keys)
        self.assertIn("ENABLE_WEB_PLUGIN", keys)

    def test_api_plugins_with_launcher(self):
        spec = SidecarSpec(name="memory", script="memory_server.py",
                           port=8766, required=True)
        launcher.ACTIVE_LAUNCHER = Launcher(specs=[spec], root=self.tmp)
        try:
            d = self.plugin._api_plugins()
            self.assertTrue(d["launcher"])
            self.assertIn("memory", [s["name"] for s in d["sidecars"]])
        finally:
            launcher.ACTIVE_LAUNCHER = None

    def test_control_restart_guard_when_no_launcher(self):
        launcher.ACTIVE_LAUNCHER = None
        r = self.plugin._api_control_plugins(
            {"action": "restart_sidecar", "target": "memory"})
        self.assertFalse(r["ok"])
        self.assertIn("launcher", r["error"])

    def test_control_web_plugin_not_stoppable(self):
        r = self.plugin._api_control_plugins(
            {"action": "stop_plugin", "target": "web"})
        self.assertFalse(r["ok"])

    def test_control_unknown_action(self):
        r = self.plugin._api_control_plugins(
            {"action": "frobnicate", "target": "x"})
        self.assertFalse(r["ok"])


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
