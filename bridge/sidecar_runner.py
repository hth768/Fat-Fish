# -*- coding: utf-8 -*-
"""Sidecar 子进程管理器：把 qq_bot 的独立进程服务作为可插拔插件包运行。

每个 sidecar 包（plugins/sidecar_*/manifest.json）声明 script/host/port，
本类负责 spawn / 终止 / 健康探测。子进程用当前解释器（venv python）+ qq_bot cwd 启动。
"""
import os
import socket
import subprocess
import sys
import time

from quiet import attention, degrade

# qq_bot 根目录：app.py 启动时已把 cwd 切到 f:/qq_bot（相对路径数据文件同位）。
# 这里运行时取 cwd，脚本存在性检查与子进程 cwd 都基于它。
def _qq_bot_dir() -> str:
    return os.getcwd()


def _log_dir() -> str:
    """sidecar 日志目录：<qq_bot>/logs（`.log` 已在 .gitignore 内）。"""
    d = os.path.join(_qq_bot_dir(), "logs")
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    return d


class SidecarProcess:
    """一个 sidecar 服务的子进程句柄。"""

    def __init__(self, name: str, script: str, host: str = "127.0.0.1",
                 port: int = 0, title: str = ""):
        self.name = name
        self.title = title or name
        self.script = script                      # 相对 qq_bot 根目录
        self.host = host
        self.port = int(port)
        self.proc: subprocess.Popen = None
        self.started_at = 0.0
        self.last_error = ""
        self.log_path = ""
        self._log_fp = None

    # ---- 生命周期 ----
    def start(self) -> dict:
        if self.alive():
            return {"ok": True, "hint": "已在运行"}
        base = _qq_bot_dir()
        script_path = os.path.join(base, self.script)
        if not os.path.isfile(script_path):
            self.last_error = f"脚本不存在: {script_path}"
            return {"ok": False, "error": self.last_error}
        cmd = [sys.executable, self.script, "--host", self.host]
        if self.port:
            cmd += ["--port", str(self.port)]
        # 输出落盘到 logs/sidecar_<name>.log：原先把 stdout/stderr 丢 DEVNULL，
        # 子进程崩溃时没有任何线索，排障只能靠猜。
        try:
            self.log_path = os.path.join(_log_dir(), f"sidecar_{self.name}.log")
            self._log_fp = open(self.log_path, "a", encoding="utf-8", errors="replace")
            self._log_fp.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} 启动 "
                               f"(cmd={' '.join(cmd)}, cwd={base}) =====\n")
            self._log_fp.flush()
            out = self._log_fp
        except Exception:
            out = subprocess.DEVNULL
            self.log_path = self.log_path or ""
        try:
            self.proc = subprocess.Popen(
                cmd, cwd=base, stdout=out, stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            self.started_at = time.time()
            self.last_error = ""
            print(f"[SIDECAR] {self.name} 已启动 (pid={self.proc.pid}, port={self.port}, "
                  f"log={self.log_path})")
            return {"ok": True, "pid": self.proc.pid, "log": self.log_path}
        except Exception as e:
            self.last_error = repr(e)
            return {"ok": False, "error": self.last_error}

    def wait_ready(self, timeout: float = 15.0, interval: float = 0.4) -> bool:
        """等待服务端口可连接（进程起来 ≠ 服务就绪）。

        port=0（未声明端口）时无法探测，按「就绪」处理以免误判。
        进程中途退出会提前返回 False 并记录退出码。
        """
        if not self.port:
            return True
        deadline = time.time() + max(1.0, float(timeout))
        while time.time() < deadline:
            if not self.alive():
                self.last_error = (self.last_error
                                   or f"进程已退出（returncode={self.proc.poll() if self.proc else None}）")
                return False
            try:
                with socket.create_connection((self.host, self.port), timeout=1.0):
                    return True
            except Exception:
                time.sleep(interval)
        self.last_error = self.last_error or f"端口 {self.host}:{self.port} 在 {timeout:.0f}s 内未就绪"
        return False

    def tail_log(self, n: int = 40) -> list:
        """读取日志尾部若干行（供界面排障）。"""
        if not self.log_path or not os.path.isfile(self.log_path):
            return []
        try:
            with open(self.log_path, "r", encoding="utf-8", errors="replace") as f:
                return f.read().splitlines()[-max(1, int(n)):]
        except Exception:
            return []

    def _close_log(self):
        try:
            if self._log_fp is not None:
                self._log_fp.flush()
                self._log_fp.close()
        except Exception as e:
            degrade("sidecar_runner._close_log", e, "关日志句柄失败")
        self._log_fp = None

    def stop(self) -> dict:
        p = self.proc
        self.proc = None
        if p is None:
            return {"ok": True, "hint": "未在运行"}
        pid = p.pid
        try:
            if p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
            # Windows 兜底：terminate 对个别服务进程不生效时，taskkill 强杀整个进程树
            if p.poll() is None:
                try:
                    subprocess.run(["taskkill", "/pid", str(pid), "/f", "/t"],
                                   capture_output=True,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                    p.wait(timeout=5)
                except Exception as e:
                    attention("sidecar_runner.stop.taskkill", e,
                              "进程终止失败（sidecar=%s pid=%s 可能残留）" % (self.name, pid))
            print(f"[SIDECAR] {self.name} 已停止 (pid={pid})")
            self._close_log()
            return {"ok": True}
        except Exception as e:
            # 最后兜底：即使句柄异常也尝试 taskkill
            try:
                subprocess.run(["taskkill", "/pid", str(pid), "/f", "/t"], capture_output=True)
            except Exception as e2:
                attention("sidecar_runner.stop.fallback", e2,
                          "兜底 taskkill 失败（sidecar=%s pid=%s）" % (self.name, pid))
            return {"ok": False, "error": repr(e)}

    def restart(self) -> dict:
        self.stop()
        time.sleep(0.5)
        return self.start()

    # ---- 状态 ----
    def alive(self) -> bool:
        return bool(self.proc and self.proc.poll() is None)

    def status(self) -> dict:
        return {
            "name": self.name,
            "title": self.title,
            "script": self.script,
            "host": self.host,
            "port": self.port,
            "running": self.alive(),
            "pid": self.proc.pid if self.alive() else None,
            "uptime_sec": int(time.time() - self.started_at) if self.alive() else 0,
            "last_error": self.last_error,
            "log": self.log_path,
            "exit_code": (self.proc.poll() if (self.proc and not self.alive()) else None),
        }
