# -*- coding: utf-8 -*-
"""构建助手 / 插件协议的核心纯逻辑测试（不联网、不调用 LLM、不污染仓库数据）。

运行：
    python -m unittest discover -s tests -v
或：
    tests\\run_tests.bat

覆盖：
  1. 工具契约（数量 / 名称 / schema 形状）—— 防止误删工具
  2. 权限闸门（5 档 × 各类写操作）+ 批准记忆
  3. 路径安全（越界 / 黑名单 / 白名单 / 自定义工作区）
  4. 上下文文件白名单与文档可读
  5. manifest 校验（合法 / 8 类问题包）
  6. 模块残留清理、磁盘目录浏览、来源链接提取等工具函数
"""
import os
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 路径顺序按真实部署：引擎层（qq_bot 运行时）与 App 层都在 sys.path 上。
# bridge/* 会 import 引擎模块（如 quiet / ai_provider），故必须加引擎目录。
ENGINE = os.path.join(REPO, "libs", "qq_bot_runtime")
for _p in (os.path.join(REPO, "bridge"), REPO, ENGINE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import bridge.builder_api as ba          # noqa: E402
import bridge.pkg_manager as pm          # noqa: E402
import bridge.sidecar_runner as sr       # noqa: E402


class _IsolatedDataMixin:
    """把设置 / 审批 / 规则 / 会话落到临时目录，避免污染仓库 data/。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = self._tmp.name
        self._orig = {
            "DATA_DIR": ba.DATA_DIR,
            "SETTINGS_PATH": ba.SETTINGS_PATH,
            "APPROVALS_PATH": ba.APPROVALS_PATH,
            "RULES_PATH": ba.RULES_PATH,
            "CHAT_DIR": ba.CHAT_DIR,
        }
        ba.DATA_DIR = root
        ba.SETTINGS_PATH = os.path.join(root, "settings.json")
        ba.APPROVALS_PATH = os.path.join(root, "approvals.json")
        ba.RULES_PATH = os.path.join(root, "rules.json")
        ba.CHAT_DIR = os.path.join(root, "chat")
        pm._warn_cache.clear()

    def tearDown(self):
        for k, v in self._orig.items():
            setattr(ba, k, v)
        self._tmp.cleanup()

    # 便捷：设置权限模式
    def set_mode(self, mode, **kw):
        patch = {"permission_mode": mode}
        patch.update(kw)
        r = ba.set_settings(patch)
        assert r.get("ok"), r
        return r


class TestToolContract(_IsolatedDataMixin, unittest.TestCase):
    """工具契约：数量与形状是前端与模型共同依赖的接口。"""

    def test_tool_count_and_names(self):
        tools = ba.builder_tools()
        names = [t["function"]["name"] for t in tools]
        # 工具数量/清单是前端与模型共同依赖的接口：变更时请同步 README 与系统提示词
        self.assertEqual(len(tools), 21, "工具数变化：实际 %d，期望 21" % len(tools))
        self.assertEqual(len(names), len(set(names)), "工具名不能重复")
        expected = {
            "list_dir", "read_file", "write_file", "diff_file", "search_code",
            "check_syntax", "list_agents", "list_plugins", "list_history",
            "get_builder_settings", "web_search", "fetch_url", "fetch_urls",
            "web_research", "download_file", "generate_agent", "generate_plugin",
            "improve_agent", "improve_plugin", "save_agent", "save_plugin",
        }
        self.assertEqual(set(names), expected)

    def test_tool_schema_shape(self):
        for t in ba.builder_tools():
            self.assertEqual(t.get("type"), "function")
            fn = t["function"]
            self.assertTrue(fn.get("name"))
            self.assertTrue(fn.get("description"))
            self.assertEqual(fn["parameters"].get("type"), "object")
            self.assertIn("properties", fn["parameters"])
            for req in fn["parameters"].get("required") or []:
                self.assertIn(req, fn["parameters"]["properties"],
                              "%s 的 required 字段 %s 未在 properties 中声明" % (fn["name"], req))

    def test_write_tools_under_gate(self):
        # 所有会改磁盘的工具都必须在闸门清单里，否则可绕过权限
        self.assertIn("write_file", ba.WRITE_TOOLS)
        self.assertIn("save_agent", ba.WRITE_TOOLS)
        self.assertIn("save_plugin", ba.WRITE_TOOLS)
        self.assertIn("download_file", ba.WRITE_TOOLS)

    def test_perm_modes_and_labels(self):
        self.assertEqual(set(ba.PERM_MODES),
                         {"plan", "default", "acceptEdits", "full", "bypassPermissions"})
        for m in ba.PERM_MODES:
            self.assertTrue(ba._MODE_LABEL.get(m), m)
            self.assertTrue(ba._MODE_DESC.get(m), m)


class TestPermissionGate(_IsolatedDataMixin, unittest.TestCase):
    """权限闸门：5 档模式 × 各类写操作的放行 / 排队 / 拒绝。"""

    NEW = {"path": "plugins/_ut_new.txt", "content": "x"}
    EXIST = {"path": "plugins/groups.json", "content": "{}"}
    CORE = {"path": "bridge/_ut_probe.py", "content": "x = 1\n"}

    def setUp(self):
        super().setUp()
        self.set_mode("default")
        ba.clear_approvals()
        ba.clear_rules()

    def test_plan_blocks_everything(self):
        self.set_mode("plan")
        for args in (self.NEW, self.EXIST, self.CORE):
            r = ba._gate_operation("write_file", dict(args), {"id": "s"})
            self.assertTrue(r and r.get("blocked"), args)
        r = ba._gate_operation("save_plugin", {"name": "x"}, {"id": "s"})
        self.assertTrue(r and r.get("blocked"))

    def test_default_queues_all(self):
        self.set_mode("default")
        for args in (self.NEW, self.EXIST, self.CORE):
            r = ba._gate_operation("write_file", dict(args), {"id": "s"})
            self.assertTrue(r and r.get("pending"), args)
            ba.reject_approval(r["approval_id"])
        self.assertEqual(ba.list_approvals()["count"], 0)

    def test_acceptEdits_only_high(self):
        self.set_mode("acceptEdits")
        self.assertIsNone(ba._gate_operation("write_file", dict(self.NEW), {"id": "s"}))
        for args in (self.EXIST, self.CORE):
            r = ba._gate_operation("write_file", dict(args), {"id": "s"})
            self.assertTrue(r and r.get("pending"), args)
            ba.reject_approval(r["approval_id"])

    def test_full_only_sensitive_and_install(self):
        self.set_mode("full")
        self.assertIsNone(ba._gate_operation("write_file", dict(self.NEW), {"id": "s"}))
        # 完全访问：覆盖普通文件也不问
        self.assertIsNone(ba._gate_operation("write_file", dict(self.EXIST), {"id": "s"}))
        for tool, args in (("write_file", dict(self.CORE)),
                           ("save_plugin", {"name": "z", "manifest": {}, "code": ""}),
                           ("save_agent", {"data": {"name": "z"}})):
            r = ba._gate_operation(tool, args, {"id": "s"})
            self.assertTrue(r and r.get("pending"), tool)
            ba.reject_approval(r["approval_id"])

    def test_bypass_allows_all(self):
        self.set_mode("bypassPermissions")
        for tool, args in (("write_file", dict(self.CORE)), ("save_plugin", {"name": "z"})):
            self.assertIsNone(ba._gate_operation(tool, args, {"id": "s"}))

    def test_risk_classification(self):
        s = ba.load_settings()
        self.assertEqual(ba.classify_operation("write_file", dict(self.NEW), s)["kind"], "create")
        ov = ba.classify_operation("write_file", dict(self.EXIST), s)
        self.assertEqual((ov["level"], ov["kind"]), ("high", "overwrite"))
        se = ba.classify_operation("write_file", dict(self.CORE), s)
        self.assertEqual((se["level"], se["kind"]), ("high", "sensitive"))
        inst = ba.classify_operation("save_plugin", {"name": "z"}, s)
        self.assertEqual((inst["level"], inst["kind"]), ("high", "install"))

    def test_confirm_switches_off(self):
        """关掉「覆盖需确认」后，覆盖普通文件不再排队（核心代码仍问）。"""
        self.set_mode("acceptEdits", confirm_overwrite=False)
        self.assertIsNone(ba._gate_operation("write_file", dict(self.EXIST), {"id": "s"}))
        r = ba._gate_operation("write_file", dict(self.CORE), {"id": "s"})
        self.assertTrue(r and r.get("pending"))
        ba.reject_approval(r["approval_id"])


class TestApprovalMemory(_IsolatedDataMixin, unittest.TestCase):
    """批准记忆：命中即跳过询问，范围（文件/目录/全部）要精确。"""

    def setUp(self):
        super().setUp()
        self.set_mode("acceptEdits")
        ba.clear_approvals()
        ba.clear_rules()

    def _rule_for(self, path, scope="file"):
        r = ba._gate_operation("write_file", {"path": path, "content": "x"}, {"id": "s"})
        self.assertTrue(r and r.get("pending"))
        ru = ba._add_rule("write_file", {"path": path}, scope)
        ba.reject_approval(r["approval_id"])
        return ru

    def test_file_scope_exact_only(self):
        ru = self._rule_for("bridge/_ut_a.py", "file")
        self.assertTrue(ru)
        self.assertIsNone(ba._gate_operation("write_file",
                                            {"path": "bridge/_ut_a.py", "content": "y"},
                                            {"id": "s"}))
        self.assertTrue(ba._gate_operation("write_file",
                                           {"path": "bridge/_ut_b.py", "content": "y"},
                                           {"id": "s"}))
        ba.clear_approvals()

    def test_dir_scope_covers_subtree(self):
        self._rule_for("bridge/_ut_c.py", "dir")
        self.assertIsNone(ba._gate_operation("write_file",
                                            {"path": "bridge/deep/_ut_d.py", "content": "y"},
                                            {"id": "s"}))
        self.assertTrue(ba._gate_operation("write_file",
                                           {"path": "webui/_ut_e.js", "content": "y"},
                                           {"id": "s"}))
        ba.clear_approvals()

    def test_all_scope_matches_any_file(self):
        ba._add_rule("write_file", {"path": "plugins/x.txt"}, "all")
        self.assertIsNone(ba._gate_operation("write_file",
                                            {"path": "webui/whatever.js", "content": "y"},
                                            {"id": "s"}))

    def test_switch_off_disables_memory(self):
        ba._add_rule("write_file", {"path": "plugins/x.txt"}, "all")
        self.set_mode("acceptEdits", remember_approvals=False)
        self.assertTrue(ba._gate_operation("write_file",
                                           {"path": "webui/whatever.js", "content": "y"},
                                           {"id": "s"}))

    def test_rules_crud(self):
        ba._add_rule("write_file", {"path": "plugins/a.txt"}, "file")
        ba._add_rule("save_plugin", {"name": "demo"}, "file")
        rules = ba.list_rules()["rules"]
        self.assertEqual(len(rules), 2)
        self.assertEqual(ba.delete_rule(rules[0]["id"])["count"], 1)
        self.assertEqual(ba.clear_rules()["count"], 0)


class TestPathSafety(_IsolatedDataMixin, unittest.TestCase):
    """路径安全：越界、黑名单、白名单（应用目录模式）。"""

    def setUp(self):
        super().setUp()
        self.set_mode("default", workspace="")

    def test_escape_rejected(self):
        for bad in ("../secret.txt", "..\\secret.txt", "plugins/../../secret.txt"):
            with self.assertRaises(ValueError):
                ba._resolve_rooted(bad)

    def test_denylist_rejected(self):
        for bad in ("data/x.json", ".git/config", "libs/qq_bot_runtime/ai_providers.json",
                    "libs/qq_bot_runtime/user_profiles.json"):
            with self.assertRaises(ValueError):
                ba._resolve_rooted(bad)

    def test_whitelist_enforced_in_app_mode(self):
        with self.assertRaises(ValueError):
            ba._resolve_rooted("some_random_dir/x.txt")
        self.assertTrue(ba._resolve_rooted("plugins/a.txt").endswith("a.txt"))
        self.assertTrue(ba._resolve_rooted("bridge/a.py").endswith("a.py"))

    def test_custom_workspace_allows_whole_tree(self):
        ws = os.path.join(self._tmp.name, "proj", "src")
        os.makedirs(ws)
        self.set_mode("default", workspace=os.path.join(self._tmp.name, "proj"))
        self.assertTrue(ba.is_custom_workspace())
        self.assertTrue(ba._resolve_rooted("src/x.txt").endswith("x.txt"))
        with self.assertRaises(ValueError):          # 黑名单依然生效
            ba._resolve_rooted("data/x.json")
        with self.assertRaises(ValueError):          # 越界依然生效
            ba._resolve_rooted("../outside.txt")

    def test_nonexistent_workspace_falls_back(self):
        r = ba.set_settings({"workspace": os.path.join(self._tmp.name, "nope")})
        self.assertFalse(r.get("ok"))
        self.assertFalse(ba.is_custom_workspace())

    def test_workspace_change_scoped(self):
        """相对路径解析随工作区根变化。"""
        ws = os.path.join(self._tmp.name, "ws1")
        os.makedirs(os.path.join(ws, "sub"))
        self.set_mode("default", workspace=ws)
        p = ba._resolve_rooted("sub/f.txt")
        self.assertTrue(os.path.normcase(p).startswith(os.path.normcase(ws)))


class TestContextFiles(_IsolatedDataMixin, unittest.TestCase):
    """上下文文件白名单：源码 + 根目录文档可读，私有数据不可读。"""

    def setUp(self):
        super().setUp()
        self.set_mode("default", workspace="")

    def test_docs_included(self):
        files = [f["path"] for f in ba.list_context_files()["files"]]
        for doc in ("README.md", "OVERVIEW.md", "PLUGINS.md"):
            self.assertIn(doc, files, "根目录文档应可作上下文：%s" % doc)
        self.assertIn("bridge/builder_api.py", files)

    def test_private_files_excluded(self):
        files = [f["path"] for f in ba.list_context_files()["files"]]
        for bad in ("libs/qq_bot_runtime/ai_providers.json",
                    "libs/qq_bot_runtime/user_profiles.json"):
            self.assertNotIn(bad, files)
        self.assertFalse([f for f in files if f.startswith("data/")])

    def test_read_doc_ok_and_private_denied(self):
        r = ba.read_context_file("PLUGINS.md")
        self.assertTrue(r["ok"])
        self.assertIn("app_bridge", r["content"])
        self.assertFalse(ba.read_context_file("data/builder_settings.json")["ok"])
        self.assertFalse(ba.read_context_file("../outside.txt")["ok"])


class TestManifestValidation(unittest.TestCase):
    """manifest 校验：合法包放行、8 类问题包给出人话错误。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = os.path.join(self.tmp.name, "pkg_x")
        os.makedirs(self.dir)

    def tearDown(self):
        self.tmp.cleanup()

    def _v(self, meta, with_plugin=True):
        if with_plugin:
            with open(os.path.join(self.dir, "plugin.py"), "w", encoding="utf-8") as f:
                f.write("def create_plugin(core):\n    return None\n")
        return pm.validate_manifest(meta, self.dir)

    def test_schema_version_and_kinds(self):
        self.assertEqual(pm.MANIFEST_SCHEMA_VERSION, 2)
        self.assertEqual(set(pm.VALID_KINDS),
                         {"platform", "feature", "brain", "sidecar", "local", "world"})

    def test_valid_package(self):
        errs, warns = self._v({"schema_version": 2, "name": "pkg_x", "title": "T",
                               "kind": "local", "version": "1.0.0"})
        self.assertEqual(errs, [])
        self.assertEqual(warns, [])

    def test_warns_are_non_blocking(self):
        errs, warns = self._v({"name": "pkg_x", "title": "T", "kind": "local"})
        self.assertEqual(errs, [])
        self.assertTrue(any("schema_version" in w for w in warns))
        self.assertTrue(any("version" in w for w in warns))

    def test_bad_kind(self):
        errs, _ = self._v({"name": "pkg_x", "title": "T", "kind": "wizard"})
        self.assertTrue(any("kind 非法" in e for e in errs))

    def test_world_requires_domain(self):
        # world 类型必须声明 world_domain（借鉴 Pal-AI-Lab 的 World 抽象）
        errs, _ = self._v({"name": "pkg_x", "title": "T", "kind": "world"})
        self.assertTrue(any("world_domain" in e for e in errs))
        errs, _ = self._v({"name": "pkg_x", "title": "T", "kind": "world",
                            "world_domain": "game"})
        self.assertEqual(errs, [])
        errs, _ = self._v({"name": "pkg_x", "title": "T", "kind": "world",
                            "world_domain": "space"})
        self.assertTrue(any("world_domain 非法" in e for e in errs))

    def test_pkg_name_must_be_snake(self):
        # 包名规范：小写蛇形，避免中文/大写跨平台导入异常
        errs, _ = self._v({"name": "MyPlugin", "title": "T", "kind": "local"})
        self.assertTrue(any("name（MyPlugin）必须小写蛇形" in e for e in errs))
        errs, _ = self._v({"name": "我的插件", "title": "T", "kind": "local"})
        self.assertTrue(any("name（我的插件）必须小写蛇形" in e for e in errs))
        # 合法：小写蛇形（但目录名是 pkg_x，需一致；用 pkg_x 验证放行路径）
        errs, _ = self._v({"name": "pkg_x", "title": "T", "kind": "local"})
        self.assertEqual(errs, [])

    def test_missing_name(self):
        errs, _ = self._v({"title": "T", "kind": "local"})
        self.assertTrue(any("name" in e for e in errs))

    def test_name_dir_mismatch(self):
        errs, _ = self._v({"name": "other", "title": "T", "kind": "local"})
        self.assertTrue(any("不一致" in e for e in errs))

    def test_sidecar_requires_script(self):
        errs, _ = self._v({"name": "pkg_x", "title": "T", "kind": "sidecar"}, with_plugin=False)
        self.assertTrue(any("sidecar" in e and "script" in e for e in errs), errs)

    def test_dep_list_type(self):
        errs, _ = self._v({"name": "pkg_x", "title": "T", "kind": "local",
                           "requires": "abc"})
        self.assertTrue(any("requires" in e for e in errs))

    def test_future_schema_rejected(self):
        errs, _ = self._v({"schema_version": 99, "name": "pkg_x", "title": "T",
                           "kind": "local"})
        self.assertTrue(any("高于本机支持" in e for e in errs))

    def test_missing_entry_blocks_load(self):
        errs, _ = self._v({"name": "pkg_x", "title": "T", "kind": "local"},
                          with_plugin=False)
        self.assertTrue(any("包装器" in e for e in errs))

    def test_bad_json_gives_human_error(self):
        with open(os.path.join(self.dir, "manifest.json"), "w", encoding="utf-8") as f:
            f.write("{ not json")
        meta, errs, _ = pm.load_manifest(self.dir)
        self.assertIsNone(meta)
        self.assertTrue(any("JSON" in e for e in errs))

    def test_config_schema_key_required(self):
        errs, _ = self._v({"name": "pkg_x", "title": "T", "kind": "local",
                           "config_schema": [{"label": "无 key"}]})
        self.assertTrue(any("config_schema" in e for e in errs))

    def test_purge_wrapper_modules(self):
        sys.modules["feiyu_pkg_ut_x"] = object()
        sys.modules["feiyu_pkg_ut_x.sub"] = object()
        sentinel = object()
        sys.modules["feiyu_pkg_ut_keep"] = sentinel
        removed = pm.purge_wrapper_modules("ut_x")
        self.assertEqual(sorted(removed), ["feiyu_pkg_ut_x", "feiyu_pkg_ut_x.sub"])
        self.assertIs(sys.modules.get("feiyu_pkg_ut_keep"), sentinel)
        sys.modules.pop("feiyu_pkg_ut_keep", None)


class TestMiscUtils(_IsolatedDataMixin, unittest.TestCase):
    """杂项：元数据剔除、来源链接提取、磁盘目录浏览、sidecar 就绪。"""

    def test_strip_meta(self):
        msgs = [{"role": "assistant", "content": "a", "_reasoning": "r"},
                {"role": "user", "content": "u"}]
        out = ba._strip_meta(msgs)
        self.assertNotIn("_reasoning", out[0])
        self.assertEqual(out[0]["content"], "a")
        self.assertIn("_reasoning", msgs[0], "不得修改入参")

    def test_extract_sources(self):
        data = {"output": [{"content": [{"annotations": [
            {"url": "https://a.example/1"}, {"url": "https://b.example/2"}]}]}]}
        urls = ba._extract_sources(data, "", 6)
        self.assertEqual(urls, ["https://a.example/1", "https://b.example/2"])
        # 无 annotations 时从正文提取
        urls2 = ba._extract_sources({}, "见 https://c.example/3 与 https://d.example/4")
        self.assertIn("https://c.example/3", urls2)
        self.assertEqual(ba._extract_sources({}, "", 6), [])

    def test_list_disk_dirs(self):
        r = ba.list_disk_dirs("")
        if os.name == "nt":
            self.assertTrue(r["ok"])
            self.assertTrue(r["dirs"], "应有盘符")
            for d in r["dirs"]:
                self.assertTrue(os.path.isdir(d["path"]))
            self.assertTrue(r["quick"])
        r2 = ba.list_disk_dirs(REPO)
        self.assertTrue(r2["ok"])
        self.assertTrue(all(os.path.isdir(d["path"]) for d in r2["dirs"]))
        self.assertFalse(ba.list_disk_dirs(os.path.join(REPO, "nope_dir_xyz"))["ok"])
        self.assertFalse(ba.list_disk_dirs(os.path.join(REPO, "README.md"))["ok"])
        self.assertTrue(ba.list_disk_dirs('"%s"' % REPO)["ok"], "应容忍带引号粘贴")

    def test_check_syntax(self):
        ok = ba._check_syntax("plugins/x.py", "a = 1\n")
        self.assertTrue(ok["ok"])
        bad = ba._check_syntax("plugins/x.py", "def f(:\n")
        self.assertFalse(bad["ok"])
        self.assertFalse(ba._check_syntax("plugins/x.txt", "x")["ok"])

    def test_sidecar_wait_ready_no_port(self):
        sc = sr.SidecarProcess(name="ut", script="nope.py", port=0)
        self.assertTrue(sc.wait_ready(0.3), "未声明端口视为无需探测")
        st = sc.status()
        for k in ("name", "running", "port", "last_error", "log", "exit_code"):
            self.assertIn(k, st)


if __name__ == "__main__":
    unittest.main(verbosity=2)
