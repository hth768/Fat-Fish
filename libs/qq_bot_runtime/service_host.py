# -*- coding: utf-8 -*-
"""通用本地 RPC 服务骨架（肥鱼娘 sidecar 基础设施，不属于任何业务层）。

把任意 Python 模块的函数暴露为本机 HTTP RPC，供主进程经 *_client 远程调用。
设计上面向任意「服务进程」：各服务（如 memory_server）只声明要托管的模块 + 超时，
骨架本身不 import 任何业务模块——依赖方向始终是「业务 sidecar -> service_host」，
本文件可被移动到任意基础设施目录而不影响使用者。

当前唯一的使用者是 memory_server（托管 `vector_memory`）；knowledge / agent 若将来
需要独立进程，可直接复用同一套骨架（对齐 N.E.K.O 的 main / agent / memory / monitor
多服务拆分，避免每个子系统重写一遍 HTTP 服务）。

设计要点（与 vox_tts_server 一致）：
    - 仅监听 127.0.0.1，禁止外网访问。
    - stdlib ThreadingHTTPServer，无额外依赖。
    - 后台 asyncio 循环 + run_coroutine_threadsafe，统一支持 sync / async 函数。
    - 结果递归转 JSON 安全类型（tuple->list、numpy/torch 标量->python）。

端点：
    GET  /health  -> {"ok": true, "ready": true, "modules": [...]}
    POST /call    -> {"module","func","args","kwargs"} -> {"ok": true, "result": ...}
"""
import argparse
import asyncio
import importlib
import inspect
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# 单调用超时默认值（通用）：普通 RPC 足够用。需要给「首次懒加载重模型」留时间的
# 服务（如 memory 的 sentence-transformers）请在 serve(timeout=...) 里显式传大值。
RPC_TIMEOUT = 120


def _to_jsonable(obj):
    """递归把结果转换成严格 JSON 可序列化类型。"""
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, (list, tuple)):
        return [_to_jsonable(x) for x in obj]
    if isinstance(obj, dict):
        return {str(k): _to_jsonable(v) for k, v in obj.items()}
    if hasattr(obj, "item"):  # numpy / torch 标量
        try:
            return obj.item()
        except Exception:
            pass
    if hasattr(obj, "tolist"):
        try:
            return _to_jsonable(obj.tolist())
        except Exception:
            pass
    return str(obj)


class _Dispatch:
    """在独立 asyncio 循环里实际调用目标模块函数（支持 sync/async）。"""

    def __init__(self, modules):
        self.modules = list(modules)
        self._loop = None

    def start_loop(self):
        self._loop = asyncio.new_event_loop()

        async def _idle():
            while True:
                await asyncio.sleep(3600)

        def _run():
            asyncio.set_event_loop(self._loop)
            self._loop.run_until_complete(_idle())

        threading.Thread(target=_run, name="svc-loop", daemon=True).start()
        while self._loop and not self._loop.is_running():
            time.sleep(0.01)

    async def run(self, module, func, args, kwargs):
        mod = importlib.import_module(module)
        f = getattr(mod, func)
        r = f(*(args or []), **(kwargs or {}))
        if inspect.iscoroutine(r):
            r = await r
        return _to_jsonable(r)


class RpcHandler(BaseHTTPRequestHandler):
    dispatch = None
    timeout = RPC_TIMEOUT   # 由 serve(timeout=...) 注入

    def log_message(self, *a):  # 静默访问日志
        pass

    def _send(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.split("?")[0] == "/health":
            self._send(200, {"ok": True, "ready": True, "modules": self.dispatch.modules})
        else:
            self._send(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        if self.path.split("?")[0] != "/call":
            self._send(404, {"ok": False, "error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            body = json.loads(raw.decode("utf-8"))
        except Exception as e:
            self._send(400, {"ok": False, "error": f"bad request: {e}"})
            return

        module = body.get("module")
        func = body.get("func")
        args = body.get("args")
        kwargs = body.get("kwargs")
        if not module or not func:
            self._send(400, {"ok": False, "error": "missing module/func"})
            return
        if module not in self.dispatch.modules:
            self._send(400, {"ok": False, "error": f"module '{module}' not hosted"})
            return

        try:
            fut = asyncio.run_coroutine_threadsafe(
                self.dispatch.run(module, func, args, kwargs), self.dispatch._loop
            )
            result = fut.result(timeout=self.timeout)
            self._send(200, {"ok": True, "result": result})
        except Exception as e:  # 后端异常原样回传，由客户端抛出
            self._send(500, {"ok": False, "error": f"{type(e).__name__}: {e}"})


def serve(modules, host="127.0.0.1", port=8766, banner="rpc-service", timeout=None):
    """启动一个通用 RPC 服务，托管给定模块。

    timeout: 单次调用超时（秒）。默认用 RPC_TIMEOUT；托管「首次懒加载重模型」的模块
             （如 vector_memory 的 embedding 模型）时请显式传大值（如 600）。
    """
    modules = [m.strip() for m in modules if m.strip()]
    dispatch = _Dispatch(modules)
    dispatch.start_loop()
    for m in modules:  # 预导入（轻量），尽早暴露导入错误
        try:
            importlib.import_module(m)
        except Exception as e:
            print(f"[{banner}] 预导入 {m} 失败（调用时仍可触发）: {e}", file=sys.stderr)

    RpcHandler.dispatch = dispatch
    RpcHandler.timeout = RPC_TIMEOUT if timeout is None else timeout
    httpd = ThreadingHTTPServer((host, port), RpcHandler)
    print(f"[{banner}] 已启动 http://{host}:{port}  托管模块: {modules}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


def read_args(default_modules):
    """通用 CLI 解析（--host/--port/--modules），供各 RPC 服务脚本复用。"""
    p = argparse.ArgumentParser(description="肥鱼娘 RPC sidecar")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8766)
    p.add_argument("--modules", default=",".join(default_modules))
    p.add_argument("--timeout", type=float, default=None,
                   help="单次调用超时（秒），默认用 RPC_TIMEOUT")
    return p.parse_args()
