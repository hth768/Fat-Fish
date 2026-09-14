# -*- coding: utf-8 -*-
"""多 sidecar 编排运行时（对齐 N.E.K.O launcher_core.runtime）。

生命周期（与 N.E.K.O 一致）：
    spawn all servers -> wait until ready -> start main -> monitor state
并在此基础上增加：
    - 崩溃自动重启（required sidecar 退出后重拉）；
    - 优雅退出（SIGINT/SIGTERM -> 终止所有子进程）。

这是纯编排层：具体能力由 memory_server / monitor_server 等 sidecar 脚本提供，
新增能力只需往 specs 里加一条 SidecarSpec（与 N.E.K.O 的 local_server/* 对应）。
"""
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional
from urllib.error import URLError
from urllib.request import urlopen


@dataclass
class SidecarSpec:
    name: str
    script: str
    python: str = ""            # 解释器路径；空=用主 venv(sys.executable)。
                                 # TTS 等重依赖跑在隔离 venv（如 venv_vox），对应 N.E.K.O local_server/*。
    env: dict = field(default_factory=dict)   # 注入到该 sidecar 子进程的环境变量
    host: str = "127.0.0.1"
    port: int = 0
    extra_args: List[str] = field(default_factory=list)
    required: bool = True           # 退出后是否自动重启；非必需仅告警
    order: int = 0              # 编排顺序：越小越先 spawn / 越先等待就绪（supervisord 式依赖）
    ready_timeout: float = 30.0      # 等待 /health 就绪的超时（秒）
    health_path: str = "/health"


@dataclass
class Launcher:
    specs: List[SidecarSpec]
    root: str = field(default_factory=os.getcwd)
    env: dict = field(default_factory=dict)
    poll_interval: float = 5.0
    stop: threading.Event = field(default_factory=threading.Event)
    procs: dict = field(default_factory=dict)
    _monitor_thread: Optional[threading.Thread] = None

    # ---- 健康探测 ----
    def health_url(self, spec: SidecarSpec) -> str:
        return f"http://{spec.host}:{spec.port}{spec.health_path}"

    def is_ready(self, spec: SidecarSpec) -> bool:
        try:
            with urlopen(self.health_url(spec), timeout=2):
                return True
        except (URLError, OSError):
            return False

    # ---- 拉起 / 退出 ----
    def _build_cmd(self, spec: SidecarSpec) -> List[str]:
        exe = spec.python or sys.executable   # 支持 TTS 等隔离 venv 运行时
        cmd = [exe, spec.script, "--host", spec.host,
               "--port", str(spec.port)]
        if spec.extra_args:
            cmd += list(spec.extra_args)
        return cmd

    def spawn(self, spec: SidecarSpec) -> Optional[subprocess.Popen]:
        if not spec.port:
            return None
        log_path = os.path.join(self.root, f"{spec.name}_server.log")
        try:
            logf = open(log_path, "a", encoding="utf-8")
            proc = subprocess.Popen(
                self._build_cmd(spec), cwd=self.root,
                stdout=logf, stderr=subprocess.STDOUT,
                env={**os.environ, **self.env, **spec.env},
            )
            self.procs[spec.name] = proc
            print(f"[launcher] 已拉起 {spec.name} sidecar pid={proc.pid} -> "
                  f"{self.health_url(spec)}", flush=True)
            return proc
        except Exception as e:
            print(f"[launcher][WARN] {spec.name} 拉起失败: {e}", flush=True)
            return None

    def spawn_all(self):
        for spec in sorted(self.specs, key=lambda s: s.order):
            self.spawn(spec)

    def wait_ready(self):
        """等待各 sidecar 就绪；required 未就绪则抛 RuntimeError。

        按 order 升序等待：依赖项（order 小）先就绪，被依赖项随后。
        """
        for spec in sorted(self.specs, key=lambda s: s.order):
            if not spec.port:
                continue
            end = time.time() + spec.ready_timeout
            while time.time() < end:
                if self.stop.is_set():
                    return
                if self.is_ready(spec):
                    print(f"[launcher] {spec.name} 就绪 ({self.health_url(spec)})",
                          flush=True)
                    break
                time.sleep(0.5)
            else:
                msg = f"{spec.name} 未在 {spec.ready_timeout}s 内就绪"
                if spec.required:
                    raise RuntimeError(msg)
                print(f"[launcher][WARN] {msg}，继续（非必需）", flush=True)

    def respawn_if_dead(self):
        for spec in self.specs:
            if not spec.required:
                continue
            proc = self.procs.get(spec.name)
            if proc is None or proc.poll() is not None:
                print(f"[launcher] 检测到 {spec.name} 退出，自动重启…", flush=True)
                self._spawn_one(spec)

    def _spawn_one(self, spec):
        """去重拉起：同名进程仍存活则跳过（避免 monitor 线程与手动 restart 双拉）。"""
        existing = self.procs.get(spec.name)
        if existing is not None and existing.poll() is None:
            return existing
        return self.spawn(spec)

    def restart(self, name):
        """重启指定 sidecar：终止旧进程后重新拉起，不依赖 monitor 线程。"""
        spec = next((s for s in self.specs if s.name == name), None)
        if spec is None:
            return False
        proc = self.procs.get(name)
        if proc is not None:
            try:
                proc.terminate()
            except Exception:
                pass
            try:
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        self.procs.pop(name, None)
        self._spawn_one(spec)
        return True

    def _monitor_loop(self):
        while not self.stop.is_set():
            self.respawn_if_dead()
            self.stop.wait(self.poll_interval)
        self.respawn_if_dead()  # 退出前最后兜底

    def shutdown(self):
        self.stop.set()
        for name, proc in list(self.procs.items()):
            if proc.poll() is None:
                try:
                    proc.terminate()
                except Exception:
                    pass
        for proc in self.procs.values():
            try:
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        print("[launcher] 所有 sidecar 已停止", flush=True)

    # ---- 主流程（拉起 -> 等就绪 -> 启主程序 -> 监控）----
    def run(self, main_entry: Callable[[], None]):
        old = {}
        for s in (signal.SIGINT, signal.SIGTERM):
            try:
                old[s] = signal.signal(s, self._on_signal)
            except (ValueError, OSError):
                pass  # 非主线程 / 平台不支持

        try:
            self.spawn_all()
            self.wait_ready()
            self._monitor_thread = threading.Thread(
                target=self._monitor_loop, name="launcher-monitor", daemon=True)
            self._monitor_thread.start()
            print("[launcher] 全部 sidecar 就绪，启动主程序…", flush=True)
            main_entry()
        except BaseException as e:  # 含 KeyboardInterrupt
            if not isinstance(e, KeyboardInterrupt):
                print(f"[launcher][ERR] 主程序异常退出: {e!r}", flush=True)
            raise
        finally:
            self.shutdown()
            for s, h in old.items():
                try:
                    signal.signal(s, h)
                except Exception:
                    pass

    def _on_signal(self, signum, frame):
        if self.stop.is_set():
            return
        print(f"[launcher] 收到信号 {signum}，准备优雅退出…", flush=True)
        self.stop.set()
        raise KeyboardInterrupt()
