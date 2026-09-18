# -*- coding: utf-8 -*-
"""quiet（降级留痕）与 audit_silent_except（审计/改写工具）的测试。

覆盖：
  · quiet：默认静默只计数、attention 会输出、节流去重、snapshot/reset
  · audit 工具：静默判定、分桶规则、改写正确性（缩进/嵌套/CRLF/末尾换行/语法自检）
"""
import ast
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENGINE = os.path.join(REPO, "libs", "qq_bot_runtime")
for _p in (REPO, os.path.join(REPO, "bridge"), ENGINE, os.path.join(REPO, "tests")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import quiet                     # noqa: E402
import audit_silent_except as A  # noqa: E402


class TestQuiet(unittest.TestCase):

    def setUp(self):
        quiet.reset()

    def test_degrade_is_silent_by_default(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            quiet.degrade("ut.a", ValueError("x"), "note")
            quiet.degrade("ut.a", ValueError("x"), "note")
        self.assertEqual(buf.getvalue(), "", "降级默认不应输出（防日志刷屏）")
        snap = quiet.snapshot()
        self.assertEqual(snap["total"], 2)
        self.assertEqual(snap["sites"][0]["where"], "ut.a")
        self.assertEqual(snap["sites"][0]["count"], 2)

    def test_attention_always_prints(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            quiet.attention("ut.b", RuntimeError("boom"), "真问题")
        out = buf.getvalue()
        self.assertIn("[QUIET][WARN]", out)
        self.assertIn("ut.b", out)
        self.assertIn("boom", out)

    def test_attention_throttled(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            for _ in range(5):
                quiet.attention("ut.c", None, "same site")
        lines = [l for l in buf.getvalue().splitlines() if l.strip()]
        self.assertEqual(len(lines), 1, "同站点 3s 内应合并为一行")

    def test_snapshot_recent_and_reset(self):
        quiet.degrade("ut.d", None, "a")
        quiet.attention("ut.e", None, "b")
        snap = quiet.snapshot(limit=10)
        self.assertEqual(len(snap["recent"]), 2)
        self.assertEqual([r["kind"] for r in snap["recent"]], ["degrade", "attention"])
        quiet.reset()
        self.assertEqual(quiet.snapshot()["total"], 0)

    def test_degrade_tolerates_none_exc(self):
        quiet.degrade("ut.f")           # 不传异常也不该炸
        self.assertEqual(quiet.snapshot()["sites"][0]["where"], "ut.f")


class TestAuditTool(unittest.TestCase):

    def _scan(self, code):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "m.py")
            with open(p, "w", encoding="utf-8") as f:
                f.write(code)
            return A.scan_file(p)

    def test_detects_silent_handlers(self):
        recs = self._scan(
            "def f(items):\n"
            "    try:\n"
            "        import optional_dep\n"
            "    except ImportError:\n"
            "        pass\n"
            "    for i in items:\n"
            "        try:\n"
            "            v = i.bit_length()\n"
            "        except AttributeError:\n"
            "            continue\n")
        self.assertEqual(len(recs), 2, "pass 与 continue 两种静默体都要被找到")
        self.assertEqual([r["body_kind"] for r in recs], ["pass", "continue"])

    def test_ignores_handlers_with_real_body(self):
        recs = self._scan(
            "def f():\n"
            "    try:\n"
            "        import x\n"
            "    except Exception as e:\n"
            "        print(e)\n"
            "    try:\n"
            "        import y\n"
            "    except Exception:\n"
            "        return None\n")
        self.assertEqual(recs, [], "有实际处理的 handler 不该被列为静默")

    def test_bucket_probe_vs_side_effect(self):
        recs = self._scan(
            "import os\n"
            "def probe():\n"
            "    try:\n"
            "        v = os.environ['X']\n"
            "    except KeyError:\n"
            "        pass\n"
            "def risky():\n"
            "    try:\n"
            "        open('a', 'w').write('x')\n"
            "    except Exception:\n"
            "        pass\n")
        got = {r["func"]: r["bucket"] for r in recs}
        self.assertEqual(got.get("probe"), "A", "env 探测应归 A")
        self.assertEqual(got.get("risky"), "B", "写文件吞异常应归 B（静默失败风险）")

    def test_continue_body_detected(self):
        recs = self._scan(
            "def f(items):\n"
            "    for i in items:\n"
            "        try:\n"
            "            v = i.strip()\n"
            "        except Exception:\n"
            "            continue\n")
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["body_kind"], "continue")

    # ---- 改写正确性 ----

    def _convert(self, code, buckets=("A",), nl="\n", final_nl=True):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "m.py")
            text = code.replace("\n", nl) + (nl if final_nl else "")
            with open(p, "w", encoding="utf-8", newline="") as f:
                f.write(text)
            r = A.convert_file(p, dry_run=False, buckets=buckets)
            with open(p, "r", encoding="utf-8", newline="") as f:
                out = f.read()
            return r, out

    def test_convert_produces_valid_python_and_keeps_indent(self):
        code = (
            "import os\n"
            "\n"
            "class C:\n"
            "    def m(self):\n"
            "        try:\n"
            "            v = os.environ['X']\n"
            "        except KeyError:\n"
            "            pass\n"
        )
        r, out = self._convert(code)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["changed"], 1)
        ast.parse(out)                                # 必须仍是合法 Python
        self.assertIn("from quiet import degrade", out)
        self.assertIn("        except KeyError as e:", out)     # 保留原缩进
        self.assertIn("            degrade(\"m.py:", out)       # 体缩进 +4
        self.assertIn("as e", out)

    def test_convert_handles_nested_handlers(self):
        code = (
            "import os\n"
            "def f(path):\n"
            "    total = 0\n"
            "    try:\n"
            "        for r, d, fs in os.walk(path):\n"
            "            for fn in fs:\n"
            "                try:\n"
            "                    total += os.path.getsize(fn)\n"
            "                except OSError:\n"
            "                    pass\n"
            "    except OSError:\n"
            "        pass\n"
            "    return total\n"
        )
        r, out = self._convert(code)
        self.assertEqual(r["changed"], 2, "内外两个 handler 都应改写")
        ast.parse(out)
        self.assertEqual(out.count("degrade("), 2)

    def test_convert_keeps_crlf_and_final_newline(self):
        code = (
            "import os\n"
            "def f():\n"
            "    try:\n"
            "        v = os.environ['X']\n"
            "    except KeyError:\n"
            "        pass\n"
        )
        r, out = self._convert(code, nl="\r\n")
        self.assertTrue(r["ok"], r)
        self.assertIn("\r\n", out, "CRLF 源文件必须保持 CRLF")
        self.assertNotIn("\n\n", out.replace("\r\n", ""))    # 不应混入裸 LF
        self.assertTrue(out.endswith("\r\n"), "末尾换行要保留")
        ast.parse(out)

    def test_convert_skips_bucket_b(self):
        code = (
            "def f():\n"
            "    try:\n"
            "        open('a', 'w').write('x')\n"
            "    except Exception:\n"
            "        pass\n"
        )
        r, out = self._convert(code, buckets=("A",))
        self.assertEqual(r["changed"], 0, "B 桶需人工判断，不该被自动改写")
        self.assertIn("pass", out)

    def test_convert_dry_run_does_not_write(self):
        code = (
            "import os\n"
            "def f():\n"
            "    try:\n"
            "        v = os.environ['X']\n"
            "    except KeyError:\n"
            "        pass\n"
        )
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "m.py")
            with open(p, "w", encoding="utf-8") as f:
                f.write(code)
            r = A.convert_file(p, dry_run=True, buckets=("A",))
            self.assertEqual(r["changed"], 1)
            with open(p, encoding="utf-8") as f:
                self.assertEqual(f.read(), code, "dry-run 不得落盘")


if __name__ == "__main__":
    unittest.main(verbosity=2)
