# -*- coding: utf-8 -*-
"""临时：后台启动 memory_server，探测端口与真实报错。"""
import os
import subprocess
import sys
import time
import urllib.request

ROOT = r"f:\qq_bot"
PY = os.path.join(ROOT, "venv", "Scripts", "python.exe")
LOG = os.path.join(ROOT, "_dbg_mem.log")

if os.path.exists(LOG):
    os.remove(LOG)

proc = subprocess.Popen(
    [PY, "memory_server.py", "--host", "127.0.0.1", "--port", "8799",
     "--modules", "vector_memory"],
    cwd=ROOT, stdout=open(LOG, "w", encoding="utf-8"),
    stderr=subprocess.STDOUT,
)

ready = False
for i in range(60):                      # 最多等 30s
    time.sleep(0.5)
    if proc.poll() is not None:
        print(f"[!] 进程已退出，returncode={proc.returncode}，耗时 {i*0.5:.1f}s")
        break
    try:
        with urllib.request.urlopen("http://127.0.0.1:8799/health", timeout=1) as r:
            print("[OK] /health ->", r.read().decode())
            ready = True
            break
    except Exception:
        pass
else:
    print("[!] 30s 内未就绪，但进程仍在运行")

if not ready and proc.poll() is None:
    print("[-] 进程存活但端口未监听，可能是线程/绑定问题")

# 拿 /call 试一次（触发 vector_memory 懒加载）
if ready:
    try:
        import json
        payload = json.dumps({"module": "vector_memory", "func": "stats",
                              "args": [], "kwargs": {}}).encode()
        req = urllib.request.Request("http://127.0.0.1:8799/call", data=payload,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=120) as r:
            print("[OK] /call stats ->", r.read().decode())
    except Exception as e:
        print("[!] /call 失败:", type(e).__name__, e)

proc.terminate()
try:
    proc.wait(timeout=5)
except Exception:
    proc.kill()

print("\n===== LOG =====")
print(open(LOG, encoding="utf-8", errors="ignore").read())
