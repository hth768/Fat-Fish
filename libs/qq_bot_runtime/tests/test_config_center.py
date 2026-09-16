# -*- coding: utf-8 -*-
"""配置中心（第5项）回归测试：覆盖层合并、${ENV} 解析、零回归回落、热重载。

不依赖网络 / 真实密钥：通过临时 ai_providers.json 覆盖层验证合并语义。
"""
import json
import os
import tempfile
import unittest

import ai_provider
import config


def _write_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)


class ConfigCenterTest(unittest.TestCase):
    def setUp(self):
        self._orig_json = ai_provider._PROVIDER_JSON
        self._orig_cache = dict(ai_provider._cache)
        self._orig_env = {}
        self.tmp = tempfile.mkdtemp()
        self.json_path = os.path.join(self.tmp, "ai_providers.json")

    def tearDown(self):
        ai_provider._PROVIDER_JSON = self._orig_json
        ai_provider._cache = self._orig_cache
        for k in list(self._orig_env.keys()):
            if self._orig_env[k] is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = self._orig_env[k]

    def _set_env(self, k, v):
        self._orig_env[k] = os.environ.get(k)
        os.environ[k] = v

    def test_fallback_no_json(self):
        """无 JSON 时完全回落 config.py 默认值（零回归）。"""
        ai_provider._PROVIDER_JSON = os.path.join(self.tmp, "nope.json")
        cfg = ai_provider.load_provider_config(reset_cache=True)
        self.assertEqual(cfg["providers"], {})   # 默认无写死供应商，全靠注册表
        self.assertEqual(
            cfg["capability_routing"]["chat"],
            config.AI_CAPABILITY_ROUTING["chat"],
        )
        self.assertEqual(cfg["role_routing"], config.AI_ROLE_ROUTING)
        # 四个键都规整为 dict
        for k in ("providers", "capability_routing", "vision_routing", "role_routing"):
            self.assertIsInstance(cfg[k], dict)

    def test_overlay_provider_merge_and_env(self):
        """覆盖层逐供应商合并 + ${ENV} 解析；未写的键/路由保留默认。"""
        self._set_env("MY_TEST_KEY", "sk-secret")
        overlay = {
            "AI_PROVIDERS": {
                "deepseek": {"api_key": "${MY_TEST_KEY}", "base_url": "http://ds",
                             "default_model": "ds", "models": {}},
                "glm": {"api_key": "${MY_TEST_KEY}", "base_url": "http://glm",
                        "models": {"chat": "glm-chat"}},
            },
            "AI_CAPABILITY_ROUTING": {"chat": ["glm", "deepseek"]},
        }
        _write_json(self.json_path, overlay)
        ai_provider._PROVIDER_JSON = self.json_path
        cfg = ai_provider.load_provider_config(reset_cache=True)

        ds = cfg["providers"]["deepseek"]
        self.assertEqual(ds["api_key"], "sk-secret")   # ${ENV} 解析成功
        self.assertEqual(ds["base_url"], "http://ds")  # 覆盖层提供的键保留
        self.assertIn("glm", cfg["providers"])          # 覆盖层新增的供应商生效
        self.assertEqual(cfg["providers"]["glm"]["api_key"], "sk-secret")
        self.assertEqual(cfg["capability_routing"]["chat"], ["glm", "deepseek"])
        # 覆盖层未改的能力路由保持原样
        self.assertEqual(
            cfg["capability_routing"]["reasoning"],
            config.AI_CAPABILITY_ROUTING["reasoning"],
        )

    def test_env_unset_resolves_empty(self):
        """${ENV} 未设置时回落空串（不会报错、不会残留占位符）。"""
        overlay = {"AI_PROVIDERS": {"deepseek": {"api_key": "${NOT_EXIST_VAR_XYZ}"}}}
        _write_json(self.json_path, overlay)
        ai_provider._PROVIDER_JSON = self.json_path
        cfg = ai_provider.load_provider_config(reset_cache=True)
        self.assertEqual(cfg["providers"]["deepseek"]["api_key"], "")

    def test_reload_updates_routing(self):
        """热重载：改文件后 reload 反映新路由，并保留既有遥测累计槽位。"""
        base = {
            "AI_PROVIDERS": {
                "deepseek": {"api_key": "k", "capabilities": ["chat"],
                             "base_url": "http://d", "default_model": "d", "models": {}},
                "gemini": {"api_key": "k", "capabilities": ["chat"],
                           "base_url": "http://g", "default_model": "g", "models": {}},
            },
            "AI_CAPABILITY_ROUTING": {"chat": ["deepseek"]},
        }
        _write_json(self.json_path, base)
        ai_provider._PROVIDER_JSON = self.json_path
        ai_provider.load_provider_config(reset_cache=True)
        llm = ai_provider.UnifiedLLM()
        self.assertEqual(llm.enabled_for("chat"), ["deepseek"])

        base["AI_CAPABILITY_ROUTING"]["chat"] = ["gemini", "deepseek"]
        _write_json(self.json_path, base)
        llm.reload()
        self.assertEqual(llm.enabled_for("chat"), ["gemini", "deepseek"])
        self.assertIn("deepseek", llm.stats)
        self.assertIn("gemini", llm.stats)


if __name__ == "__main__":
    unittest.main()
