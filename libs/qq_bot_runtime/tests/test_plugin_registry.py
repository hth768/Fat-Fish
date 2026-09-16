# -*- coding: utf-8 -*-
"""统一插件注册表（plugin_registry）回归测试。

覆盖：版本比较、版本约束匹配、清单完整性、依赖/循环/顺序校验、
      与 PluginManager / launcher sidecar 的一致性（防止清单与现实漂移）。
"""
import unittest

import config
import plugin_registry as reg


class VersionMatchTest(unittest.TestCase):
    def test_operators(self):
        self.assertTrue(reg.version_matches("1.0.0", ">=1.0"))
        self.assertFalse(reg.version_matches("0.9.0", ">=1.0"))
        self.assertTrue(reg.version_matches("1.5.0", ">=1.0,<2.0"))
        self.assertFalse(reg.version_matches("2.0.0", ">=1.0,<2.0"))
        self.assertTrue(reg.version_matches("1.2.3", "==1.2.3"))
        self.assertFalse(reg.version_matches("1.2.3", "!=1.2.3"))
        self.assertTrue(reg.version_matches("1.2.3", ">1.2.0"))
        self.assertFalse(reg.version_matches("1.2.3", ">1.2.3"))

    def test_wildcard_and_empty(self):
        self.assertTrue(reg.version_matches("1.2.3", "1.2.*"))
        self.assertFalse(reg.version_matches("1.3.0", "1.2.*"))
        self.assertTrue(reg.version_matches("1.0.0", ""))     # 无约束
        self.assertTrue(reg.version_matches("1.0.0", "*"))

    def test_different_segment_length(self):
        self.assertTrue(reg.version_matches("1.0", ">=1.0.0"))
        self.assertFalse(reg.version_matches("2.0", "<2"))


class RegistryStructureTest(unittest.TestCase):
    def test_required_entries_exist(self):
        for name in ("qq", "console", "web", "bilibili", "vox_tts",
                     "memory", "monitor", "tts", "telemetry",
                     "chat"):
            self.assertIsNotNone(reg.get_spec(name), f"清单缺少条目 {name}")

    def test_names_unique(self):
        names = [s.name for s in reg.SPECS]
        self.assertEqual(len(names), len(set(names)), "清单存在重名条目")

    def test_every_entry_has_version_and_kind(self):
        valid_kinds = {"platform", "feature", "brain", "sidecar"}
        for s in reg.SPECS:
            self.assertIn(s.kind, valid_kinds, f"{s.name} kind 非法")
            self.assertTrue(s.version, f"{s.name} 缺 version")
            self.assertTrue(reg.version_matches(s.version, f"<{reg.CORE_VERSION}")
                            or s.version == reg.CORE_VERSION
                            or reg._cmp_version(s.version, reg.CORE_VERSION) <= 0,
                            f"{s.name} 版本 {s.version} 高于核心 {reg.CORE_VERSION}")

    def test_to_dict_shape(self):
        d = reg.to_dict()
        self.assertEqual(len(d), len(reg.SPECS))
        for e in d:
            for k in ("name", "kind", "version", "enabled", "requires", "order"):
                self.assertIn(k, e)


class ValidationTest(unittest.TestCase):
    def test_baseline_valid(self):
        rep = reg.validate()
        self.assertTrue(rep["ok"], f"基线校验不应有错: {rep['errors']}")

    def test_counts(self):
        rep = reg.validate()
        self.assertEqual(rep["total"], len(reg.SPECS))
        self.assertGreater(rep["counts"].get("platform", 0), 0)
        self.assertGreater(rep["counts"].get("brain", 0), 0)

    def test_missing_dependency_detected(self):
        spec = reg.EntrySpec(name="__tmp_a", kind="feature", version="1.0.0",
                             requires={"__not_exist__": ">=1.0"})
        reg.SPECS.append(spec)
        reg._BY_NAME[spec.name] = spec
        try:
            rep = reg.validate()
            self.assertFalse(rep["ok"])
            self.assertTrue(any("__not_exist__" in e for e in rep["errors"]))
        finally:
            reg.SPECS.remove(spec)
            reg._BY_NAME.pop(spec.name, None)

    def test_version_unsatisfied_detected(self):
        spec = reg.EntrySpec(name="__tmp_b", kind="feature", version="1.0.0",
                             requires={"qq": ">=99.0"})
        reg.SPECS.append(spec)
        reg._BY_NAME[spec.name] = spec
        try:
            rep = reg.validate()
            self.assertFalse(rep["ok"])
            self.assertTrue(any("版本不满足" in e for e in rep["errors"]))
        finally:
            reg.SPECS.remove(spec)
            reg._BY_NAME.pop(spec.name, None)

    def test_cycle_detected(self):
        a = reg.EntrySpec(name="__tmp_c", kind="feature", version="1.0.0",
                          requires={"__tmp_d": ">=1.0"})
        b = reg.EntrySpec(name="__tmp_d", kind="feature", version="1.0.0",
                          requires={"__tmp_c": ">=1.0"})
        for s in (a, b):
            reg.SPECS.append(s)
            reg._BY_NAME[s.name] = s
        try:
            rep = reg.validate()
            self.assertFalse(rep["ok"])
            self.assertTrue(any("循环依赖" in e for e in rep["errors"]))
        finally:
            for s in (a, b):
                reg.SPECS.remove(s)
                reg._BY_NAME.pop(s.name, None)

    def test_order_conflict_detected(self):
        a = reg.EntrySpec(name="__tmp_e", kind="feature", version="1.0.0", order=50)
        b = reg.EntrySpec(name="__tmp_f", kind="feature", version="1.0.0", order=40,
                          requires={"__tmp_e": ">=1.0"})
        for s in (a, b):
            reg.SPECS.append(s)
            reg._BY_NAME[s.name] = s
        try:
            rep = reg.validate()
            self.assertFalse(rep["ok"])
            self.assertTrue(any("顺序冲突" in e for e in rep["errors"]))
        finally:
            for s in (a, b):
                reg.SPECS.remove(s)
                reg._BY_NAME.pop(s.name, None)

    def test_render_is_ascii_safe(self):
        """错误文本必须能写进 GBK 控制台（曾因 ✗ 抛 UnicodeEncodeError）。"""
        for issue in (reg.Issue("error", "x", "boom"), reg.Issue("warning", "y", "hm")):
            issue.render().encode("gbk")   # 不抛异常即通过

    def test_summary_runs(self):
        text = reg.summary()
        self.assertIn("插件注册表", text)
        text.encode("gbk")                 # 控制台可打印


class ConsistencyTest(unittest.TestCase):
    """清单必须与核心注册行为一致（防漂移）。"""

    def test_core_registers_exactly_listed_plugins(self):
        import agent_core
        core = agent_core.AgentCore()
        core.register_builtin_plugins(platforms=["console"])
        registered = {p.name for p in core.plugins.all()}
        listed = {s.name for s in reg.SPECS if s.kind in ("platform", "feature")}
        # 已注册的必须在清单里（反过来允许：清单里关闭的可以不注册）
        self.assertTrue(registered <= listed,
                        f"核心注册了未登记的插件: {registered - listed}")

    def test_enabled_plugins_are_registered(self):
        """清单开关启用的功能插件必须被注册；请求的平台必须被注册（平台另受 platforms 约束）。"""
        import agent_core
        core = agent_core.AgentCore()
        core.register_builtin_plugins(platforms=["console"])
        registered = {p.name for p in core.plugins.all()}
        for s in reg.enabled_specs("feature"):
            self.assertIn(s.name, registered, f"清单标记启用的功能插件 {s.name} 未注册")
        # 显式请求的平台
        self.assertIn("console", registered, "请求的平台 console 未注册")

    def test_registry_validation_for_registered(self):
        import agent_core
        core = agent_core.AgentCore()
        core.register_builtin_plugins(platforms=["console"])
        rep = core.plugins.validate_registry(log=False)
        self.assertTrue(rep["ok"], f"已注册插件校验应通过: {rep['errors']}")

    def test_launcher_sidecars_match_registry(self):
        """launcher 生成的 sidecar 规格应与清单一致（脚本/端口/required/order）。"""
        import launcher
        for flag in ("ENABLE_MEMORY_SERVER", "ENABLE_MONITOR",
                     "ENABLE_TTS_SERVER", "ENABLE_TELEMETRY_SERVER"):
            setattr(config, flag, True)
        try:
            specs = launcher._build_specs()
        finally:
            for flag in ("ENABLE_MEMORY_SERVER", "ENABLE_MONITOR",
                         "ENABLE_TTS_SERVER", "ENABLE_TELEMETRY_SERVER"):
                setattr(config, flag, False)
        names = {s.name for s in specs}
        listed = {s.name for s in reg.by_kind("sidecar")}
        self.assertEqual(names, listed, "sidecar 清单与 launcher 构建结果不一致")
        # order 必须与清单一致（编排顺序来源统一）
        by_name = {s.name: s for s in reg.by_kind("sidecar")}
        for s in specs:
            self.assertEqual(s.order, by_name[s.name].order,
                             f"{s.name} 的 order 与清单不一致")
            self.assertTrue(s.script.endswith(".py"))

    def test_plugin_classes_expose_version(self):
        """插件类应能提供 version（基类默认 1.0.0，可覆盖）。"""
        from plugin_base import Plugin
        self.assertTrue(getattr(Plugin, "version", None))


if __name__ == "__main__":
    unittest.main()
