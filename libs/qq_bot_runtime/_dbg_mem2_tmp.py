# -*- coding: utf-8 -*-
"""临时：完全复现 launcher 拉起 memory 的方式（8766 端口），捕捉真实报错。"""
import os
import subprocess
import sys
import time

ROOT = r"f:\qq_bot"
PY = os.path.join(ROOT, "venv", "Scripts", "python.exe")
LOG = os.path.join(ROOT, "_dbg_mem2.log")
if os.path.exists(LOG):
    os.remove(LOG)

# 与 launcher._build_cmd 完全一致
cmd = [PY, "memory_server.py", "--host", "127.0.0.1", "--port", "8766",
       "--modules", "vector_memory"]
print("CMD:", cmd)

proc = subprocess.Popen(cmd, cwd=ROOT,
                        stdout=open(LOG, "w", encoding="utf-8"),
                        stderr=subprocess.STDOUT)
print("pid:", proc.pid)

for i in range(20):
    time.sleep(0.5)
    rc = proc.poll()
    if rc is not None:
        print(f"[!] 进程在 {i*0.5:.1f}s 退出，returncode={rc}")
        break
else:
    print("[i] 进程存活 10s")

if proc.poll() is None:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except Exception:
        proc.kill()

print("\n===== LOG =====")
txt = open(LOG, encoding="utf-8", errors="ignore").read()
print(txt if txt.strip() else "(日志为空)")
