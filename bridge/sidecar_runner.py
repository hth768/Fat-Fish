# -*- coding: utf-8 -*-
"""Sidecar 子进程管理器：把 qq_bot 的独立进程服务作为可插拔插件包运行。

每个 sidecar 包（plugins/sidecar_*/manifest.json）声明 script/host/port，
本类负责 spawn / 终止 / 健康探测。子进程用当前解释器（venv python）+ qq_bot cwd 启动。
"""
import os
import subprocess
import sys
import time

# qq_bot 根目录：app.py 启动时已把 cwd 切到 f:/qq_bot（相对路径数据文件同位）。
# 这里运行时取 cwd，脚本存在性检查与子进程 cwd 都基于它。
def _qq_bot_dir() -> str:
    return os.getcwd()


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
        try:
            self.proc = subprocess.Popen(
                cmd, cwd=base,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            self.started_at = time.time()
            self.last_error = ""
            print(f"[SIDECAR] {self.name} 已启动 (pid={self.proc.pid}, port={self.port})")
            return {"ok": True, "pid": self.proc.pid}
        except Exception as e:
            self.last_error = repr(e)
            return {"ok": False, "error": self.last_error}

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
                except Exception:
                    pass
            print(f"[SIDECAR] {self.name} 已停止 (pid={pid})")
            return {"ok": True}
        except Exception as e:
            # 最后兜底：即使句柄异常也尝试 taskkill
            try:
                subprocess.run(["taskkill", "/pid", str(pid), "/f", "/t"], capture_output=True)
            except Exception:
                pass
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
        }
