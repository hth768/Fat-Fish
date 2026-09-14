# -*- coding: utf-8 -*-
"""统一 LLM / 视觉路由（第1/2/3项）回归测试：能力过滤、角色路由、故障转移。

不依赖网络：用合成配置 + 桩函数替换 _call_once / 视觉驱动验证路由语义。
"""
import asyncio
import json
import os
import tempfile
import unittest

import ai_provider
import telemetry


SYNTH = {
    "AI_PROVIDERS": {
        "a": {"api_key": "k", "base_url": "http://a", "default_model": "a-def",
              "capabilities": ["chat", "vision"], "think_param": False, "models": {}},
        "b": {"api_key": "k", "base_url": "http://b", "default_model": "b-def",
              "capabilities": ["chat"], "think_param": False, "models": {}},
        "c": {"api_key": "", "base_url": "http://c", "default_model": "c-def",
              "capabilities": ["chat"], "think_param": False, "models": {}},
        "v": {"api_key": "k", "base_url": "http://v", "default_model": "v-def",
              "capabilities": ["vision"], "think_param": False,
              "models": {"vision": "v-vision", "chat": "v-chat"}},
    },
    "AI_CAPABILITY_ROUTING": {"chat": ["a", "b", "c"], "vision": ["a", "b"]},
    "AI_ROLE_ROUTING": {"judge": {"capability": "chat", "model": "m-judge"}},
}


class RoutingTest(unittest.TestCase):
    def setUp(self):
        self._orig_json = ai_provider._PROVIDER_JSON
        self._orig_cache = dict(ai_provider._cache)
        self._tele_orig = (telemetry.record_call, telemetry.record_ok, telemetry.record_fail)
        # 路由/故障转移测试不应污染真实遥测累计
        telemetry.record_call = lambda *a, **k: None
        telemetry.record_ok = lambda *a, **k: None
        telemetry.record_fail = lambda *a, **k: None

        self.tmp = tempfile.mkdtemp()
        self.json_path = os.path.join(self.tmp, "ai_providers.json")
        with open(self.json_path, "w", encoding="utf-8") as f:
            json.dump(SYNTH, f, ensure_ascii=False)
        ai_provider._PROVIDER_JSON = self.json_path
        ai_provider.load_provider_config(reset_cache=True)
        self.llm = ai_provider.UnifiedLLM()

    def tearDown(self):
        ai_provider._PROVIDER_JSON = self._orig_json
        ai_provider._cache = self._orig_cache
        telemetry.record_call, telemetry.record_ok, telemetry.record_fail = self._tele_orig

    def test_enabled_for_filters_key_and_capability(self):
        self.assertEqual(self.llm.enabled_for("chat"), ["a", "b"])   # c 无 key
        self.assertEqual(self.llm.enabled_for("vision"), ["a"])       # b 无 vision

    def test_model_for_resolution(self):
        a = SYNTH["AI_PROVIDERS"]["a"]
        self.assertEqual(self.llm._model_for(a, "chat", "explicit"), "explicit")
        self.assertEqual(self.llm._model_for(a, "chat", None), "a-def")
        v = SYNTH["AI_PROVIDERS"]["v"]
        self.assertEqual(self.llm._model_for(v, "vision", None), "v-vision")
        self.assertEqual(self.llm._model_for(v, "chat", None), "v-chat")
        # 找不到 reasoning 模型时回落 models["chat"]，再回落 default_model
        self.assertEqual(self.llm._model_for(v, "reasoning", None), "v-chat")

    def test_no_provider_raises(self):
        async def go():
            with self.assertRaises(ai_provider.ProviderError):
                await self.llm.chat([{"role": "user", "content": "hi"}],
                                    capability="nonexistent")
        asyncio.run(go())

    def test_role_routing_overrides_model_and_capability(self):
        captured = {}

        async def fake_call_once(self2, name, prov, messages, capability, model,
                                 think, tools, images, timeout, role=None):
            captured.update(name=name, capability=capability, model=model, role=role)
            return "ok"

        orig = ai_provider.UnifiedLLM._call_once
        ai_provider.UnifiedLLM._call_once = fake_call_once
        try:
            async def go():
                await self.llm.chat([{"role": "user", "content": "x"}],
                                    capability="reasoning", role="judge")
            asyncio.run(go())
        finally:
            ai_provider.UnifiedLLM._call_once = orig
        self.assertEqual(captured["capability"], "chat")   # role 覆盖 capability
        self.assertEqual(captured["model"], "m-judge")      # role 覆盖 model
        self.assertEqual(captured["name"], "a")             # chat 首个可用供应商
        self.assertEqual(captured["role"], "judge")         # role 透传到 call

    def test_inline_role_model_resolution(self):
        # ① 角色模型内联到供应商条目：被选中时优先模型 models[role]
        prov = {"models": {"judge": "j-model", "chat": "c-def"}, "default_model": "d-def"}
        self.assertEqual(self.llm._model_for(prov, "chat", None, "judge"), "j-model")
        self.assertEqual(self.llm._model_for(prov, "chat", None, "unknown"), "c-def")
        self.assertEqual(self.llm._model_for({"models": {}, "default_model": "d"},
                                             "chat", None, "judge"), "d")
        # 显式 model 始终优先（含角色场景）
        self.assertEqual(self.llm._model_for(prov, "chat", "explicit", "judge"), "explicit")

    def test_build_payload_extra_body_and_think(self):
        # ② extra_body 始终发送；think_body 仅思考时发送；think_param 兼容
        prov = {
            "extra_body": {"enable_search": True},
            "think_body": {"thinking": {"type": "enabled"}},
            "default_model": "m", "models": {},
        }
        msgs = [{"role": "user", "content": "hi"}]
        p_off = self.llm._build_payload(prov, msgs, "chat", None, False, None, None)
        self.assertTrue(p_off["enable_search"])
        self.assertNotIn("thinking", p_off)

        p_on = self.llm._build_payload(prov, msgs, "chat", None, True, None, None)
        self.assertEqual(p_on["thinking"], {"type": "enabled"})

        # 旧 think_param 行为：关闭时显式 disabled，开启时 enabled（零回归）
        prov2 = {"think_param": True, "default_model": "m", "models": {}}
        p2_off = self.llm._build_payload(prov2, msgs, "chat", None, False, None, None)
        self.assertEqual(p2_off["thinking"], {"type": "disabled"})
        p2_on = self.llm._build_payload(prov2, msgs, "chat", None, True, None, None)
        self.assertEqual(p2_on["thinking"], {"type": "enabled"})

    def test_fault_tolerance_falls_through(self):
        order = []

        def make(name):
            async def fn(self2, *args, **kwargs):
                order.append(name)
                raise RuntimeError(f"{name} boom")
            return fn

        orig = ai_provider.UnifiedLLM._call_once
        ai_provider.UnifiedLLM._call_once = make("stub")
        try:
            async def go():
                with self.assertRaises(ai_provider.ProviderError):
                    await self.llm.chat([{"role": "user", "content": "x"}],
                                        capability="chat")
            asyncio.run(go())
        finally:
            ai_provider.UnifiedLLM._call_once = orig
        self.assertGreaterEqual(len(order), 2)  # 逐家尝试直到耗尽


class VisionRoutingTest(unittest.TestCase):
    def setUp(self):
        self._orig_factories = dict(ai_provider._VISION_DRIVER_FACTORIES)
        self._tele_orig = (telemetry.record_call, telemetry.record_ok, telemetry.record_fail)
        telemetry.record_call = lambda *a, **k: None
        telemetry.record_ok = lambda *a, **k: None
        telemetry.record_fail = lambda *a, **k: None
        ai_provider._VISION_DRIVER_FACTORIES = {}  # 避免真实客户端导入/网络
        self.v = ai_provider.UnifiedVision()

    def tearDown(self):
        ai_provider._VISION_DRIVER_FACTORIES = self._orig_factories
        telemetry.record_call, telemetry.record_ok, telemetry.record_fail = self._tele_orig

    def test_dispatch_picks_first_available_driver(self):
        calls = []

        class Fake:
            async def describe_image(self, image_bytes, prompt=""):
                calls.append("fake")
                return "desc"

        self.v.drivers = {"fake": Fake(), "other": object()}
        self.v.routing = {"image": ["missing", "fake", "other"]}

        async def go():
            return await self.v.describe_image(b"img", "p")
        self.assertEqual(asyncio.run(go()), "desc")
        self.assertEqual(calls, ["fake"])  # 跳过 missing，命中 fake

    def test_dispatch_all_fail_raises(self):
        self.v.drivers = {}
        self.v.routing = {"image": ["x", "y"]}

        async def go():
            with self.assertRaises(ai_provider.ProviderError):
                await self.v.describe_image(b"img")
        asyncio.run(go())


if __name__ == "__main__":
    unittest.main()
