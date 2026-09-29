"""Cortico 协议兼容层（libs/qq_bot_runtime/cortico/）单元测试。

覆盖协议层与装配层（不依赖 Node）：
- manifest：package.json 的 cortico 字段解析 + api 版本闸门
- env_prompt：{{变量 | 缺省}} 模板渲染
- config_schema：点分路径读写、类型/范围校验、x-hot
- registry：挂载 / 停用 / 重启、工具撞名拒绝、事件落库与投递、环境提示词
- tool_loop：模型 -> 工具 -> 回执 -> 再问（假 LLM）

Node 运行时桥有单独用例，缺 Node/tsx/Cortico 仓库时自动跳过。
"""
import asyncio
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENGINE = os.path.join(ROOT, "libs", "qq_bot_runtime")
for p in (ROOT, ENGINE):
    if p not in sys.path:
        sys.path.insert(0, p)

from cortico import config_schema, env_prompt, manifest as M
from cortico.registry import MissingWorld, WorldAssembly
from cortico.types import (API_VERSION, ConfigGroup, EventEnvelope, PromptDocDecl, ToolDef,
                           World, WorldConsoleDecl, WorldDefinition)

CORTEX = r"D:\testing\Cortico"
DUNGEON = os.path.join(CORTEX, "extensions", "node_modules", "cortico-world-dungeon")


# ---------------------------------------------------------------------------
class TestManifest(unittest.TestCase):
    def test_read_and_guard(self):
        m = M.read_manifest(DUNGEON) if os.path.isdir(DUNGEON) else None
        if m is None:
            self.skipTest("本机没有 cortico-world-dungeon 包")
        self.assertEqual(m.kind, "world")
        self.assertEqual(m.api, API_VERSION)
        ok, why = m.compatible()
        self.assertTrue(ok, why)

    def test_version_gate(self):
        m = M.CorticoManifest(name="x", root="", kind="world", api=API_VERSION - 1)
        ok, why = m.compatible()
        self.assertFalse(ok)
        self.assertIn("协议版本不一致", why)

    def test_non_world_kind(self):
        m = M.CorticoManifest(name="x", root="", kind="provider", api=API_VERSION)
        self.assertFalse(m.compatible()[0])

    def test_read_missing_dir(self):
        self.assertIsNone(M.read_manifest(os.path.join(tempfile.gettempdir(), "__nope__")))


class TestEnvPrompt(unittest.TestCase):
    def test_plain_and_default(self):
        tpl = "世界：{{w.name | 未知}}，角色：{{w.who}}"
        out = env_prompt.render(tpl, {"w.name": "艾尔登", "w.who": "小鱼"})
        self.assertEqual(out, "世界：艾尔登，角色：小鱼")

    def test_missing_uses_default(self):
        out = env_prompt.render("{{a | 兜底}}{{b}}", {"b": "B"})
        self.assertEqual(out, "兜底B")

    def test_real_dungeon_template(self):
        if not os.path.isfile(os.path.join(DUNGEON, "src", "ENV_PROMPT.md")):
            self.skipTest("没有 dungeon 模板")
        text = env_prompt.render_file(os.path.join(DUNGEON, "src", "ENV_PROMPT.md"),
                                      {"dungeon.worldName": "W", "dungeon.character": "C",
                                       "dungeon.link": "已连接"})
        self.assertIn("W", text)
        self.assertNotIn("{{", text)

    def test_missing_file(self):
        self.assertEqual(env_prompt.render_file("/no/such/file.md", {}), "")


class TestConfigSchema(unittest.TestCase):
    def setUp(self):
        self.group = ConfigGroup.coerce({
            "id": "world:demo", "owner": "world:demo",
            "schema": {"type": "object", "title": "Demo", "properties": {
                "worlds.demo.url": {"type": "string", "title": "地址", "x-hot": False},
                "worlds.demo.timeoutMs": {"type": "integer", "title": "超时",
                                          "minimum": 1000, "maximum": 120000,
                                          "multipleOf": 1000, "x-scale": 1000, "x-suffix": "s"},
                "worlds.demo.hotFlag": {"type": "boolean", "title": "热开关", "x-hot": True},
            }},
        })
        self.cfg = {"worlds": {"demo": {"url": "http://x", "timeoutMs": 15000, "hotFlag": False}}}

    def test_get_set_path(self):
        self.assertEqual(config_schema.get_by_path(self.cfg, "worlds.demo.url"), "http://x")
        config_schema.set_by_path(self.cfg, "worlds.demo.url", "http://y")
        self.assertEqual(self.cfg["worlds"]["demo"]["url"], "http://y")

    def test_validate_ranges(self):
        self.assertTrue(config_schema.validate_value(
            self.group.schema.properties["worlds.demo.timeoutMs"], 20000)[0])
        ok, _, why = config_schema.validate_value(
            self.group.schema.properties["worlds.demo.timeoutMs"], 500)
        self.assertFalse(ok)
        self.assertIn("不能小于", why)

    def test_apply_patch(self):
        changed, errors = config_schema.apply_patch(
            self.cfg, self.group, {"worlds.demo.timeoutMs": 30000})
        self.assertEqual(changed, ["worlds.demo.timeoutMs"])
        self.assertEqual(errors, {})
        _, errors = config_schema.apply_patch(self.cfg, self.group, {"worlds.demo.nope": 1})
        self.assertIn("worlds.demo.nope", errors)

    def test_hot_flag(self):
        props = self.group.schema.properties
        self.assertTrue(config_schema.is_hot(props["worlds.demo.hotFlag"]))
        self.assertFalse(config_schema.is_hot(props["worlds.demo.url"]))

    def test_describe(self):
        view = config_schema.describe(self.group, self.cfg)
        self.assertEqual(view["id"], "world:demo")
        self.assertEqual(len(view["fields"]), 3)
        scale_field = [f for f in view["fields"] if f["path"].endswith("timeoutMs")][0]
        self.assertEqual(scale_field["scale"], 1000)


# ---------------------------------------------------------------------------
class DemoWorld(World):
    """测试用 World：一个工具、一个环境提示词、一条启动事件。"""

    def __init__(self, tmpdir: str, tool_name: str = "demo_echo"):
        self.id = "demo"
        self.tmpdir = tmpdir
        self.tool_name = tool_name
        self.started = False

    async def start(self, host):
        self.started = True
        await host.push_event(EventEnvelope(type="demo.hello", ts="2026-09-29T10:00:00+08:00",
                                            source="demo", text="世界已连接"))

    async def stop(self):
        self.started = False

    def tools(self):
        return [ToolDef(name=self.tool_name, description="回声",
                        parameters={"type": "object", "properties": {}},
                        tags=["read"], handler=lambda a, c: f"echo: {a.get('t', '')}")]

    def env_prompt_vars(self):
        return {"demo.name": "测试世界"}

    def console(self, language="zh"):
        p = os.path.join(self.tmpdir, "ENV.md")
        with open(p, "w", encoding="utf-8") as f:
            f.write("你在世界 {{demo.name | 无名}}。\n")
        return WorldConsoleDecl(prompt_docs=[PromptDocDecl(
            key="worlds.demo.envPrompt", title="Demo", path=p, role="envPrompt")])


class TestAssembly(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.delivered = []
        self.asm = WorldAssembly(
            cfg={"worlds": {}},
            deliver=lambda e, t: self.delivered.append((e.type, t)),
            state_path=os.path.join(self.tmp, "worlds.json"),
        )

    def tearDown(self):
        asyncio.run(self.asm.stop_all())

    def _register_demo(self, tool_name="demo_echo", declared=True):
        return self.asm.register(
            WorldDefinition(id="demo", label="Demo", defaults=lambda: {"enabled": False},
                            create=lambda ctx: DemoWorld(self.tmp, tool_name)),
            declared=declared)

    def test_declared_world_defaults_enabled(self):
        self._register_demo()
        self.assertTrue(self.asm.section("demo")["enabled"])

    def test_mount_and_event(self):
        self._register_demo()
        receipts = asyncio.run(self.asm.start_enabled())
        self.assertIn("demo", [s.id for s in self.asm.mounted()])
        self.assertTrue(any("已启用" in r for r in receipts))
        self.assertEqual(len(self.delivered), 1)
        self.assertEqual(self.delivered[0][0], "demo.hello")
        self.assertGreaterEqual(self.asm.store.latest(), 1)

    def test_tool_clash_refused(self):
        self._register_demo()
        self.asm.register(
            WorldDefinition(id="clash", label="Clash", defaults=lambda: {"enabled": True},
                            create=lambda ctx: DemoWorld(self.tmp, "demo_echo")),
            declared=True)
        asyncio.run(self.asm.start_enabled())
        self.assertNotIn("clash", [s.id for s in self.asm.mounted()])
        self.assertTrue(any(m.id == "clash" for m in self.asm.missing))

    def test_reserved_tool_names(self):
        self._register_demo(tool_name="memory_save")
        self.asm.set_reserved_tool_names(["memory_save"])
        receipts = asyncio.run(self.asm.start_enabled())
        self.assertTrue(any("拒绝挂载" in r for r in receipts))
        self.assertEqual(self.asm.mounted(), [])

    def test_deactivate_restart(self):
        self._register_demo()
        asyncio.run(self.asm.start_enabled())
        self.assertIn("已停用", asyncio.run(self.asm.deactivate("demo")))
        self.assertEqual(self.asm.mounted(), [])
        self.assertIn("已启用", asyncio.run(self.asm.activate("demo")))
        self.assertIn("已重启", asyncio.run(self.asm.restart("demo")))

    def test_env_prompt_segments(self):
        self._register_demo()
        asyncio.run(self.asm.start_enabled())
        segs = asyncio.run(self.asm.env_prompt_segments())
        self.assertEqual(len(segs), 1)
        self.assertIn("测试世界", segs[0]["text"])

    def test_tools_collected(self):
        self._register_demo()
        asyncio.run(self.asm.start_enabled())
        tools = self.asm.tools()
        self.assertEqual([t.name for t in tools], ["demo_echo"])
        self.assertEqual(tools[0].handler({"t": "hi"}, None), "echo: hi")


class TestToolLoop(unittest.TestCase):
    def test_calls_tool_then_answers(self):
        import cortico.tool_loop as tl

        tool = ToolDef(name="demo_echo", description="回声", parameters={}, tags=["read"],
                       handler=lambda a, c: f"echo: {a.get('t', '')}")

        class FakeLLM:
            def __init__(self):
                self.n = 0

            async def chat(self, messages, capability="chat", tools=None, role=None, **kw):
                self.n += 1
                if self.n == 1:
                    return {"role": "assistant", "content": "",
                            "tool_calls": [{"id": "c1", "type": "function",
                                            "function": {"name": "demo_echo",
                                                         "arguments": '{"t":"你好"}'}}]}
                return {"role": "assistant", "content": "收到"}

        import ai_provider
        orig = ai_provider.get_llm
        ai_provider.get_llm = lambda: FakeLLM()
        try:
            text = asyncio.run(tl.run_tool_turn([{"role": "user", "content": "说"}], [tool]))
        finally:
            ai_provider.get_llm = orig
        self.assertEqual(text, "收到")

    def test_no_tools_plain_chat(self):
        import cortico.tool_loop as tl

        class FakeLLM:
            async def chat(self, messages, capability="chat", tools=None, role=None, **kw):
                return {"role": "assistant", "content": "直接回答"}

        import ai_provider
        orig = ai_provider.get_llm
        ai_provider.get_llm = lambda: FakeLLM()
        try:
            text = asyncio.run(tl.run_tool_turn([{"role": "user", "content": "hi"}], []))
        finally:
            ai_provider.get_llm = orig
        self.assertEqual(text, "直接回答")


class TestNodeBridge(unittest.TestCase):
    """Node 运行时桥：缺 Node / tsx / Cortico 仓库 / dungeon 包时跳过。"""

    def test_bridge_loads_dungeon(self):
        import shutil
        if not (shutil.which("node") or shutil.which("node.exe")):
            self.skipTest("没有 node")
        if not os.path.isdir(DUNGEON) or not os.path.isdir(CORTEX):
            self.skipTest("没有 Cortico 仓库或 dungeon 包")

        import config
        config.CORTICO_CORE_ROOT = CORTEX
        config.CORTICO_NODE_CWD = CORTEX

        from cortico.node_bridge import NodeWorldBridge
        m = M.read_manifest(DUNGEON)
        self.assertTrue(m.compatible()[0])

        async def main():
            bridge = NodeWorldBridge(
                manifest=m, cfg={"enabled": True, "serverUrl": "http://127.0.0.1:9",
                                 "requestTimeoutMs": 5000, "reconnectMaxBackoffMs": 1000},
                timezone="Asia/Shanghai", bot_name="test",
                host_callbacks={"host.log": lambda p: None},
                core_root=CORTEX)
            await bridge.start()
            try:
                tools = await bridge.tools()
                cons = await bridge.console("zh")
                return [t.name for t in tools], cons
            finally:
                await bridge.close()

        names, cons = asyncio.run(main())
        self.assertIn("dungeon_look", names)
        self.assertEqual(len(names), 9)
        self.assertTrue(any(d.role == "envPrompt" for d in cons.prompt_docs))
        self.assertEqual([g.id for g in cons.config], ["world:dungeon"])


if __name__ == "__main__":
    unittest.main()
