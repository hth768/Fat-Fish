# -*- coding: utf-8 -*-
"""遥测 sidecar（telemetry_server.py）单元测试：HMAC / 防重放 / 限流 / 聚合。"""
import importlib
import json
import os
import sys
import unittest

# 测试用独立存储，避免污染线上 telemetry_server.json
_TEST_STORE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "..", "telemetry_server_test.json")
if "telemetry_server" in sys.modules:
    del sys.modules["telemetry_server"]
import telemetry_server as ts
ts.STORE_FILE = os.path.abspath(_TEST_STORE)
ts._agg = {}
ts._seen = {}
ts._rate = {}


class IngestTest(unittest.TestCase):
    def _payload(self, device="d1", ts=None, event="ok", **extra):
        p = {"group": "providers", "key": "deepseek", "event": event,
             "ts": ts if ts is not None else __import__("time").time(),
             "device": device, "latency_ms": 10, "tokens_prompt": 1,
             "tokens_completion": 2, "error": ""}
        p.update(extra)
        return p

    def test_ingest_basic_and_stats(self):
        r = ts._ingest(self._payload(device="u_basic"))
        self.assertTrue(r.get("ok"))
        st = ts._stats()
        self.assertIn("providers", st)
        self.assertIn("deepseek", st["providers"])
        self.assertEqual(st["providers"]["deepseek"]["calls"], 1)

    def test_replay_window_rejects(self):
        import time
        future = time.time() + 9999
        r = ts._ingest(self._payload(device="u_window", ts=future))
        self.assertFalse(r.get("ok"))
        self.assertIn("窗口", r.get("error", ""))

    def test_duplicate_ts_rejected(self):
        import time
        t = time.time()
        p = self._payload(device="u_dup", ts=t)
        self.assertTrue(ts._ingest(p).get("ok"))
        # 同一 device + 同一 ts 重复 -> 拒
        self.assertFalse(ts._ingest(p).get("ok"))

    def test_hmac_enforced(self):
        import hmac, hashlib
        ts.HMAC_KEY = "secret"
        try:
            body = json.dumps({"a": 1}).encode("utf-8")
            good = hmac.new(b"secret", body, hashlib.sha256).hexdigest()
            self.assertTrue(ts._verify_sig(body, good))
            self.assertFalse(ts._verify_sig(body, "deadbeef"))
            self.assertFalse(ts._verify_sig(body, ""))
        finally:
            ts.HMAC_KEY = ""   # 还原开发模式，避免影响其他用例

    def test_rate_limit(self):
        import time
        dev = "u_ratelimit"
        ts._rate[dev] = []
        for i in range(120):
            r = ts._ingest(self._payload(device=dev, key="k%d" % (i % 3)))
            self.assertTrue(r.get("ok"), "前 120 次应成功")
        # 第 121 次应被限流
        r = ts._ingest(self._payload(device=dev, key="kx"))
        self.assertFalse(r.get("ok"))
        self.assertIn("限流", r.get("error", ""))


class LauncherSpecTest(unittest.TestCase):
    def setUp(self):
        import config
        self.cfg = config
        self._saved = getattr(config, "ENABLE_TELEMETRY_SERVER", None)

    def tearDown(self):
        if self._saved is None:
            del self.cfg.ENABLE_TELEMETRY_SERVER
        else:
            self.cfg.ENABLE_TELEMETRY_SERVER = self._saved

    def test_build_specs_includes_telemetry(self):
        import launcher
        self.cfg.ENABLE_TELEMETRY_SERVER = True
        specs = launcher._build_specs()
        names = [s.name for s in specs]
        self.assertIn("telemetry", names)
        spec = next(s for s in specs if s.name == "telemetry")
        self.assertEqual(spec.order, 3)
        self.assertFalse(spec.required)
        self.assertEqual(spec.port, getattr(self.cfg, "TELEMETRY_SERVER_PORT", 8771))


if __name__ == "__main__":
    unittest.main()
