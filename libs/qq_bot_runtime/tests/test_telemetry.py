# -*- coding: utf-8 -*-
"""遥测合规与持久化（第4项）回归测试：opt-out、落盘、重启不丢、派生指标。

不发起网络请求；临时 telemetry.json 隔离真实数据，tearDown 还原内存累计，
确保进程退出时不会把测试数据写回真实文件。
"""
import json
import os
import tempfile
import unittest

import telemetry
import config


class TelemetryTest(unittest.TestCase):
    def setUp(self):
        self._orig_file = telemetry._TELEMETRY_FILE
        self._orig_data = json.loads(json.dumps(telemetry._data))  # 深拷贝
        self._orig_flag = getattr(config, "ENABLE_TELEMETRY", True)
        self.tmp = tempfile.mkdtemp()
        self.tmpfile = os.path.join(self.tmp, "telemetry.json")
        telemetry._TELEMETRY_FILE = self.tmpfile
        telemetry._data = {"providers": {}, "vision": {}}

    def tearDown(self):
        telemetry._TELEMETRY_FILE = self._orig_file
        # 还原运行时累计，避免 atexit 把测试数据写回真实文件
        telemetry._data.clear()
        telemetry._data.update(self._orig_data)
        config.ENABLE_TELEMETRY = self._orig_flag

    def test_opt_out_snapshot_empty_and_no_record(self):
        """关闭遥测：看板为空、不累计、不落盘（opt-out 友好）。"""
        config.ENABLE_TELEMETRY = False
        telemetry._data["providers"]["deepseek"] = {"calls": 99}
        telemetry.record_call("providers", "deepseek")
        telemetry.record_ok("providers", "deepseek", 1.0)
        self.assertEqual(telemetry.snapshot("providers"), {})
        self.assertEqual(telemetry._data["providers"]["deepseek"]["calls"], 99)
        telemetry.save()
        self.assertFalse(os.path.exists(self.tmpfile))

    def test_enabled_persist_and_reload(self):
        """开启遥测：累计落盘、重启不丢、派生指标正确。"""
        config.ENABLE_TELEMETRY = True
        telemetry.record_call("providers", "deepseek")
        telemetry.record_ok("providers", "deepseek", 10.0, 5, 3)
        telemetry.record_fail("providers", "deepseek", "boom")
        telemetry.save()

        with open(self.tmpfile, "r", encoding="utf-8") as f:
            on_disk = json.load(f)
        self.assertEqual(on_disk["providers"]["deepseek"]["calls"], 1)
        self.assertEqual(on_disk["providers"]["deepseek"]["ok"], 1)
        self.assertEqual(on_disk["providers"]["deepseek"]["fail"], 1)
        self.assertEqual(on_disk["providers"]["deepseek"]["tokens_prompt"], 5)

        # 模拟重启：清空内存后从磁盘重载
        telemetry._data.clear()
        telemetry._data.update({"providers": {}, "vision": {}})
        telemetry._load()
        self.assertEqual(telemetry._data["providers"]["deepseek"]["calls"], 1)

        snap = telemetry.snapshot("providers")
        self.assertEqual(snap["deepseek"]["calls"], 1)
        self.assertEqual(snap["deepseek"]["tokens_total"], 8)   # 5 + 3
        self.assertEqual(snap["deepseek"]["fail_rate"], 1.0)
        self.assertEqual(snap["deepseek"]["last_error"], "boom")


if __name__ == "__main__":
    unittest.main()
