# -*- coding: utf-8 -*-
"""统一启动器（launcher_core）回归测试：环境自愈、健康探测、就绪等待、崩溃重启。

不依赖真实业务进程：用临时 HTTP 服务 / 立即退出的临时脚本验证编排逻辑。
"""
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import launcher_core.bootstrap as bootstrap
from launcher_core.runtime import Launcher, SidecarSpec
import config


class _OKHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        body = b'{"ok":true}' if self.path.split("?")[0] == "/health" else b"x"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _start_mock_health():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _OKHandler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, port


class BootstrapTest(unittest.TestCase):
    def test_configure_runtime_env_no_crash(self):
        # 不应触发 reexec / 不抛异常（import 场景）
        bootstrap.configure_runtime_env()
        self.assertTrue(True)


class LauncherLogicTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.launcher = Launcher(specs=[], root=self.tmp)

    def test_is_ready_true_and_false(self):
        httpd, port = _start_mock_health()
        try:
            spec = SidecarSpec(name="mock", script="x", port=port, required=False)
            self.assertTrue(self.launcher.is_ready(spec))
        finally:
            httpd.shutdown()

    def test_is_ready_false_when_down(self):
        spec = SidecarSpec(name="down", script="x", port=59997, required=False)
        self.assertFalse(self.launcher.is_ready(spec))

    def test_wait_ready_times_out_for_nonrequired(self):
        spec = SidecarSpec(name="never", script="x", port=59996,
                           required=False, ready_timeout=0.6)
        self.launcher.specs = [spec]
        # 非必需：等待超时后返回且不抛异常
        self.launcher.wait_ready()
        self.assertFalse(self.launcher.is_ready(spec))

    def test_respawn_if_dead(self):
        # 脚本立即退出，模拟 sidecar 崩溃 -> 应自动重启出新进程
        script = os.path.join(self.tmp, "die.py")
        with open(script, "w", encoding="utf-8") as f:
            f.write("import sys\nsys.exit(0)\n")
        spec = SidecarSpec(name="fragile", script=os.path.basename(script),
                           port=59995, required=True, ready_timeout=1)
        self.launcher.specs = [spec]
        self.launcher.spawn(spec)
        time.sleep(0.4)  # 等其退出
        first = self.launcher.procs["fragile"]
        self.launcher.respawn_if_dead()
        second = self.launcher.procs["fragile"]
        self.assertIsNotNone(second)
        self.assertNotEqual(first.pid, second.pid)
        time.sleep(0.4)
        self.launcher.shutdown()


class RuntimeCmdOrderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.launcher = Launcher(specs=[], root=self.tmp)

    def test_build_cmd_uses_spec_python(self):
        spec = SidecarSpec(name="a", script="a.py", port=1,
                           python="venv_x/Scripts/python.exe")
        cmd = self.launcher._build_cmd(spec)
        self.assertEqual(cmd[0], "venv_x/Scripts/python.exe")
        self.assertIn("--port", cmd)

    def test_build_cmd_default_python_is_sys_executable(self):
        spec = SidecarSpec(name="a", script="a.py", port=1)
        cmd = self.launcher._build_cmd(spec)
        self.assertEqual(cmd[0], sys.executable)

    def test_spawn_all_respects_order(self):
        # mock spawn 仅记录顺序，不真正拉起进程（避免副作用）
        calls = []
        self.launcher.spawn = lambda s: calls.append(s.name)
        specs = [
            SidecarSpec(name="c", script="x", port=1, order=2, required=False),
            SidecarSpec(name="a", script="x", port=1, order=0, required=False),
            SidecarSpec(name="b", script="x", port=1, order=1, required=False),
        ]
        self.launcher.specs = specs
        self.launcher.spawn_all()
        self.assertEqual(calls, ["a", "b", "c"])


class SpecsBuildTest(unittest.TestCase):
    def _save(self, keys):
        return {k: getattr(config, k, None) for k in keys}

    def _restore(self, saved):
        for k, v in saved.items():
            setattr(config, k, v)

    def test_build_specs_includes_tts_when_enabled(self):
        import launcher
        saved = self._save(["ENABLE_TTS_SERVER", "VOXCPM_TTS_URL", "VOX_TTS_PYTHON"])
        try:
            config.ENABLE_TTS_SERVER = True
            config.VOXCPM_TTS_URL = "http://127.0.0.1:18765"
            specs = launcher._build_specs()
            names = [s.name for s in specs]
            self.assertIn("tts", names)
            tts = next(s for s in specs if s.name == "tts")
            self.assertIn("venv_vox", tts.python)
            self.assertFalse(tts.required)
            self.assertEqual(tts.order, 2)
            self.assertEqual(tts.port, 18765)
            self.assertEqual(tts.env.get("VOXCPM_FFMPEG"), config.FFMPEG_PATH)
        finally:
            self._restore(saved)

    def test_memory_monitor_tts_orders(self):
        import launcher
        saved = self._save(["ENABLE_MEMORY_SERVER", "ENABLE_MONITOR",
                            "ENABLE_TTS_SERVER"])
        try:
            config.ENABLE_MEMORY_SERVER = True
            config.ENABLE_MONITOR = True
            config.ENABLE_TTS_SERVER = True
            specs = launcher._build_specs()
            self.assertIn(("memory", 0), [(s.name, s.order) for s in specs])
            self.assertIn(("monitor", 1), [(s.name, s.order) for s in specs])
            self.assertIn(("tts", 2), [(s.name, s.order) for s in specs])
        finally:
            self._restore(saved)


class LauncherRestartTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.launcher = Launcher(specs=[], root=self.tmp)

    def test_restart_sidecar(self):
        script = os.path.join(self.tmp, "svc.py")
        with open(script, "w", encoding="utf-8") as f:
            f.write("import time\ntime.sleep(1.2)\n")
        spec = SidecarSpec(name="svc", script=os.path.basename(script),
                           port=50200, required=False)
        self.launcher.specs = [spec]
        self.launcher.spawn(spec)
        old = self.launcher.procs.get("svc")
        self.assertIsNotNone(old)
        self.assertTrue(old.poll() is None)
        ok = self.launcher.restart("svc")
        self.assertTrue(ok)
        new = self.launcher.procs.get("svc")
        self.assertIsNotNone(new)
        self.assertNotEqual(old.pid, new.pid)   # 重启后应为新进程
        self.launcher.shutdown()

    def test_restart_unknown_returns_false(self):
        self.assertFalse(self.launcher.restart("nope"))


if __name__ == "__main__":
    unittest.main()
