# -*- coding: utf-8 -*-
"""
统一遥测（对齐 N.E.K.O 的 telemetry / DO_NOT_TRACK 设计）。

设计要点：
- 默认开（ENABLE_TELEMETRY=True）。
- 关闭时不累计、不落盘、*_stats() 返回空（用户可一键 opt-out）。
- 累计落盘（JSON）到本地，重启不丢；不收集任何对话内容 / PII，
  只统计调用次数、延迟、Token 用量、错误（符合合规原则）。
- 进程退出时自动 flush；也提供 save() 供定时调用。
"""
import json
import os
import threading
import time
import hmac
import atexit
from urllib.request import Request, urlopen
from urllib.error import URLError

import config
from quiet import degrade

# 是否在 launcher 托管下向遥测 sidecar（telemetry_server.py）上报。
# 关闭时退回纯本地文件聚合（默认/旧行为，零回归）。
_ENABLE_SERVER = bool(getattr(config, "ENABLE_TELEMETRY_SERVER", False))
_SERVER_URL = getattr(config, "TELEMETRY_SERVER_URL",
                      f"http://127.0.0.1:{getattr(config, 'TELEMETRY_SERVER_PORT', 8771)}")
_HMAC_KEY = getattr(config, "TELEMETRY_HMAC_KEY", "") or ""
_REPORTER = None

_TELEMETRY_FILE = os.path.join(
    getattr(config, "DATA_DIR", os.path.dirname(os.path.abspath(__file__))),
    "telemetry.json",
)

_lock = threading.Lock()
_data = {"providers": {}, "vision": {}}  # 运行时累计（与磁盘合并）


def enabled() -> bool:
    return bool(getattr(config, "ENABLE_TELEMETRY", True))


def _blank():
    return {"calls": 0, "ok": 0, "fail": 0, "latency_ms": 0,
            "tokens_prompt": 0, "tokens_completion": 0, "last_error": None}


def _load():
    global _data
    try:
        if os.path.exists(_TELEMETRY_FILE):
            with open(_TELEMETRY_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            _data["providers"] = loaded.get("providers", {})
            _data["vision"] = loaded.get("vision", {})
    except Exception as e:
        degrade("libs/qq_bot_runtime/telemetry.py:58 _load", e, "降级：if os.path.exists(_TELEMETRY_FILE)")


def _save():
    if not enabled():
        return
    try:
        os.makedirs(os.path.dirname(_TELEMETRY_FILE), exist_ok=True)
        tmp = _TELEMETRY_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_data, f, ensure_ascii=False)
        os.replace(tmp, _TELEMETRY_FILE)
    except Exception as e:
        degrade("telemetry._save", e, "telemetry 落盘失败")


# 启动时合并磁盘数据（仅保留已知 key 的累计）
_load()
atexit.register(_save)


def _bucket(group: str, key: str) -> dict:
    bucket = _data.setdefault(group, {})
    return bucket.setdefault(key, _blank())


def record_call(group: str, key: str):
    if not enabled():
        return
    with _lock:
        _bucket(group, key)["calls"] += 1
    _enqueue(group, key, "call")


def record_ok(group: str, key: str, latency_ms: float,
              tokens_prompt: int = 0, tokens_completion: int = 0):
    if not enabled():
        return
    with _lock:
        b = _bucket(group, key)
        b["ok"] += 1
        b["latency_ms"] += latency_ms
        b["tokens_prompt"] += tokens_prompt
        b["tokens_completion"] += tokens_completion
    _enqueue(group, key, "ok", latency_ms=latency_ms,
             tokens_prompt=tokens_prompt, tokens_completion=tokens_completion)


def record_fail(group: str, key: str, error: str):
    if not enabled():
        return
    with _lock:
        b = _bucket(group, key)
        b["fail"] += 1
        b["last_error"] = str(error)[:200]
    _enqueue(group, key, "fail", error=error)


def snapshot(group: str) -> dict:
    """返回某组（providers/vision）的累计快照（含派生指标）。不收集对话内容。

    关闭 ENABLE_TELEMETRY 时返回空（看板为空、不展示任何历史累计）。
    """
    if not enabled():
        return {}
    with _lock:
        raw = dict(_data.get(group, {}))  # noqa
    out = {}
    for k, st in raw.items():
        calls = st.get("calls") or 1
        out[k] = {
            "calls": st.get("calls", 0),
            "ok": st.get("ok", 0),
            "fail": st.get("fail", 0),
            "fail_rate": round(st.get("fail", 0) / calls, 3),
            "avg_latency_ms": round(st.get("latency_ms", 0.0) / max(st.get("ok", 1), 1), 1),
            "tokens_prompt": st.get("tokens_prompt", 0),
            "tokens_completion": st.get("tokens_completion", 0),
            "tokens_total": st.get("tokens_prompt", 0) + st.get("tokens_completion", 0),
            "last_error": st.get("last_error"),
        }
    return out


def save():
    """显式落盘（供定时任务调用）。"""
    if not enabled():
        return
    with _lock:
        _save()


# ---- 向遥测 sidecar 异步上报（仅 ENABLE_TELEMETRY_SERVER 时启用）----
import socket
import hashlib
import base64

_q = []
_q_lock = threading.Lock()
_device = socket.gethostname()


def _enqueue(group: str, key: str, event: str, **metrics):
    if not _ENABLE_SERVER:
        return
    try:
        item = {"group": group, "key": key, "event": event,
                "ts": time.time(), "device": _device,
                "latency_ms": metrics.get("latency_ms", 0),
                "tokens_prompt": metrics.get("tokens_prompt", 0),
                "tokens_completion": metrics.get("tokens_completion", 0),
                "error": str(metrics.get("error", ""))[:200]}
        with _q_lock:
            _q.append(item)
    except Exception as e:
        degrade("libs/qq_bot_runtime/telemetry.py:173 _enqueue", e, "降级：item = {'group': group, 'key': key, 'event': event")


def _flush_once():
    with _q_lock:
        batch = _q[:]
        del _q[:]
    if not batch:
        return
    try:
        import json as _json
        payload = _json.dumps(batch, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if _HMAC_KEY:
            sig = hmac.new(_HMAC_KEY.encode("utf-8"), payload, hashlib.sha256).hexdigest()
            headers["X-Sig"] = sig
        req = Request(_SERVER_URL + "/report", data=payload, headers=headers,
                      method="POST")
        urlopen(req, timeout=3)
    except (URLError, OSError, Exception) as e:
        degrade("libs/qq_bot_runtime/telemetry.py:193 _flush_once", e, "降级：import json as _json")


def _reporter_loop():
    while True:
        time.sleep(5)
        try:
            _flush_once()
        except Exception as e:
            degrade("libs/qq_bot_runtime/telemetry.py:202 _reporter_loop", e, "降级：_flush_once()")


if _ENABLE_SERVER:
    _REPORTER = threading.Thread(target=_reporter_loop, name="telemetry-reporter",
                                 daemon=True)
    _REPORTER.start()
