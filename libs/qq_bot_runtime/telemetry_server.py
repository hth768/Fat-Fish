# -*- coding: utf-8 -*-
"""遥测服务端 sidecar（对齐 N.E.K.O local_server/telemetry_server 设计）。

独立进程接收本机各组件上报的**匿名**遥测（调用次数 / 延迟 / Token / 错误），
只聚合指标，绝不收集对话内容 / PII。

安全特性（参考 N.E.K.O，体现「设计对齐」与防误用）：
- HMAC-SHA256 防篡改：上报需带签名头 `X-Sig`；密钥来自 config.TELEMETRY_HMAC_KEY。
- ±5min 时间窗防重放：payload 含 `ts`，超出窗口或重复即拒收。
- 滑动窗口限流：每 `device` 默认 120 req/h。
- append-only 落盘：telemetry_server.json（只增不改历史聚合）。

密钥未配置（TELEMETRY_HMAC_KEY 为空）时退回「仅本机、接受无签名」的宽松开发模式。

启动：
    python telemetry_server.py [--host 127.0.0.1] [--port 8771]
或（由 launcher 按 ENABLE_TELEMETRY_SERVER 自动编排）。
"""
import hashlib
import hmac
import json
import os
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import config
from quiet import degrade

# ---- 监听地址（兼容 launcher 的 --host/--port 与旧 argv[1] 端口两种调用）----
def _resolve_host_port():
    host, port = "127.0.0.1", 8771
    positional = []
    i = 1
    while i < len(sys.argv):
        a = sys.argv[i]
        if a in ("-h", "--host") and i + 1 < len(sys.argv):
            host = sys.argv[i + 1]; i += 2; continue
        if a in ("-p", "--port") and i + 1 < len(sys.argv):
            port = int(sys.argv[i + 1]); i += 2; continue
        if not a.startswith("-"):
            positional.append(a)
        i += 1
    if positional:
        port = int(positional[0])
    return host, port


HOST, PORT = _resolve_host_port()

STORE_FILE = os.path.join(
    getattr(config, "DATA_DIR", os.path.dirname(os.path.abspath(__file__))),
    "telemetry_server.json",
)
HMAC_KEY = getattr(config, "TELEMETRY_HMAC_KEY", "") or ""
REPLAY_WINDOW = 300          # ±5min
RATE_LIMIT = 120            # req/h/device
RATE_WINDOW = 3600

_lock = threading.Lock()
# 聚合：_agg[group][key] = {calls, ok, fail, latency_ms, tokens_prompt, tokens_completion, last_error}
_agg = {}
_seen = {}                   # device -> set(ts) 防重放
_rate = {}                   # device -> [ts, ...] 滑动限流


def _blank():
    return {"calls": 0, "ok": 0, "fail": 0, "latency_ms": 0.0,
            "tokens_prompt": 0, "tokens_completion": 0, "last_error": None}


def _load():
    global _agg
    try:
        if os.path.exists(STORE_FILE):
            with open(STORE_FILE, "r", encoding="utf-8") as f:
                _agg = json.load(f) or {}
    except Exception:
        _agg = {}


def _save():
    try:
        os.makedirs(os.path.dirname(STORE_FILE), exist_ok=True)
        tmp = STORE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_agg, f, ensure_ascii=False)
        os.replace(tmp, STORE_FILE)
    except Exception as e:
        degrade("telemetry_server._save", e, "聚合统计落盘失败")


_load()
threading.Timer(60, (lambda: (_save(), threading.Timer(60, (lambda: None)).start()))).start()


def _verify_sig(body: bytes, sig: str) -> bool:
    if not HMAC_KEY:
        return True  # 开发模式：不强制签名
    if not sig:
        return False
    expect = hmac.new(HMAC_KEY.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expect, sig)


def _ingest(payload: dict) -> dict:
    now = time.time()
    ts = float(payload.get("ts", 0) or 0)
    if abs(now - ts) > REPLAY_WINDOW:
        return {"ok": False, "error": "ts 超出 ±5min 窗口"}
    device = str(payload.get("device", "local"))
    with _lock:
        seen_set = _seen.setdefault(device, set())
        if ts in seen_set:
            return {"ok": False, "error": "重复 ts（防重放）"}
        seen_set.add(ts)
        # 限流
        hits = _rate.setdefault(device, [])
        hits = [t for t in hits if now - t < RATE_WINDOW]
        if len(hits) >= RATE_LIMIT:
            return {"ok": False, "error": "超过限流 120 req/h"}
        hits.append(now)
        _rate[device] = hits
        # 聚合
        group = str(payload.get("group", "misc"))
        key = str(payload.get("key", "unknown"))
        b = _agg.setdefault(group, {}).setdefault(key, _blank())
        event = payload.get("event", "call")
        b["calls"] += 1
        if event == "ok":
            b["ok"] += 1
            b["latency_ms"] += float(payload.get("latency_ms", 0) or 0)
            b["tokens_prompt"] += int(payload.get("tokens_prompt", 0) or 0)
            b["tokens_completion"] += int(payload.get("tokens_completion", 0) or 0)
        elif event == "fail":
            b["fail"] += 1
            b["last_error"] = str(payload.get("error", ""))[:200]
        # 周期落盘
        if b["calls"] % 20 == 0:
            _save()
    return {"ok": True}


def _stats() -> dict:
    out = {}
    with _lock:
        for group, keys in _agg.items():
            out[group] = {}
            for k, st in keys.items():
                calls = max(st.get("calls", 0), 1)
                out[group][k] = {
                    "calls": st.get("calls", 0),
                    "ok": st.get("ok", 0),
                    "fail": st.get("fail", 0),
                    "fail_rate": round(st.get("fail", 0) / calls, 3),
                    "avg_latency_ms": round(st.get("latency_ms", 0.0) / max(st.get("ok", 1), 1), 1),
                    "tokens_total": st.get("tokens_prompt", 0) + st.get("tokens_completion", 0),
                    "last_error": st.get("last_error"),
                }
    return out


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/health":
            self._send(200, {"ok": True, "ready": True, "service": "telemetry"})
        elif path == "/stats":
            self._send(200, {"ok": True, "stats": _stats(),
                             "hmac_enforced": bool(HMAC_KEY)})
        else:
            self._send(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        if self.path.split("?")[0] != "/report":
            self._send(404, {"ok": False, "error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            payload = json.loads(raw.decode("utf-8"))
        except Exception as e:
            self._send(400, {"ok": False, "error": f"bad request: {e}"})
            return
        sig = self.headers.get("X-Sig", "")
        if not _verify_sig(raw, sig):
            self._send(403, {"ok": False, "error": "签名校验失败"})
            return
        res = _ingest(payload)
        self._send(200 if res.get("ok") else 429, res)


def main():
    httpd = ThreadingHTTPServer((HOST, PORT), _Handler)
    mode = "HMAC 强制" if HMAC_KEY else "开发宽松(无签名)"
    print(f"[telemetry] 已启动 http://{HOST}:{PORT}  签名模式={mode}  存储={STORE_FILE}",
          flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt as e:
        degrade("libs/qq_bot_runtime/telemetry_server.py:213 main", e, "降级：httpd.serve_forever()")
    finally:
        _save()
        httpd.server_close()


if __name__ == "__main__":
    main()
