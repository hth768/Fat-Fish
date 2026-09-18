# -*- coding: utf-8 -*-
"""静默降级留痕（quiet degrade）—— 统一的「异常吞掉但要有痕迹」出口。

## 为什么需要它

代码里有大量 `except ...: pass` 形式的**刻意降级**：可选依赖探测失败、配置缺失用默认值、
非关键副作用（写日志文件、上报遥测、清理临时文件）失败等。它们**不该**改成抛异常
（会打断正常流程），但也不该彻底无痕 —— 出问题时空手排查。

历史上踩过两次坑，这个模块把它们一次性解决：

1. **日志刷屏**：高频路径（每次轮询/每实例初始化）里无条件 print，一屏屏刷无关信息。
   → 这里默认**不打印**，只计数；`FEIYU_TRACE_DEGRADE=1` 才输出，且同站点 5s 内合并。
2. **无从排查**：`pass` 吞掉后零线索，用户报「功能没反应」时只能翻源码猜。
   → `snapshot()` 提供运行期计数与最近记录，可挂到 `/api/status` 或诊断页查看。

## 用法

    from quiet import degrade, attention

    try:
        profile = store.load_profile(uid)
    except Exception as e:
        degrade("chat_service.load_profile", e, "读档案失败，用空档案继续")
        profile = {}

    # 真问题（不是刻意降级）：始终输出一行，便于第一时间发现
    attention("pkg.parse_manifest", e, "manifest 解析失败")

## 约定

| 函数 | 语义 | 输出策略 |
|---|---|---|
| `degrade(where, exc=None, note="")` | 刻意降级（预期内） | 默认静默**计数**；开 `FEIYU_TRACE_DEGRADE=1` 才打印，同站点 5s 内合并 |
| `attention(where, exc=None, note="")` | 真问题（不该发生） | **始终**打印一行 `[QUIET][WARN]`，同站点 3s 内合并 |
| `snapshot()` | 运行期统计 | 返回 {total, sites: {...}, recent: [...]}，供诊断接口/界面 |
| `reset()` | 清空统计 | 测试用 |

`where` 建议用 `"模块.函数"` 或 `"文件:行"`，便于定位与聚合同一类问题。
"""

import os
import threading
import time
from collections import deque

# 开关：FEIYU_TRACE_DEGRADE=1 / true / yes 时输出降级明细（默认只计数）
_TRACE = (os.environ.get("FEIYU_TRACE_DEGRADE", "") or "").strip().lower() in (
    "1", "true", "yes", "on")

_DEGRADE_THROTTLE = 5.0     # 同一站点降级日志最小间隔（秒）
_ATTENTION_THROTTLE = 3.0   # 同一站点告警最小间隔（秒）
_RECENT_MAX = 200           # 最近记录保留条数

_lock = threading.Lock()
_counts = {}                # where -> 次数
_last = {}                  # where -> 上次输出时间戳
_recent = deque(maxlen=_RECENT_MAX)


def _record(where: str, exc, note: str, kind: str) -> int:
    """登记一次降级/告警，返回该站点的累计次数。"""
    key = str(where or "unknown")
    now = time.time()
    with _lock:
        n = _counts.get(key, 0) + 1
        _counts[key] = n
        _recent.append({"ts": now, "where": key, "kind": kind,
                        "exc": (str(exc)[:300] if exc is not None else ""),
                        "note": str(note or "")[:200], "count": n})
    return n


def _should_print(key: str, throttle: float, first_always: bool, n: int) -> bool:
    now = time.time()
    with _lock:
        last = _last.get(key, 0.0)
        if not (first_always and n == 1) and (now - last) < throttle:
            return False
        _last[key] = now
        return True


def degrade(where: str, exc=None, note: str = "") -> None:
    """登记一次**刻意降级**。默认完全不输出（只计数），避免刷屏。

    需要看明细时：启动前设环境变量 `FEIYU_TRACE_DEGRADE=1`。
    """
    n = _record(where, exc, note, "degrade")
    if not _TRACE:
        return
    key = str(where or "unknown")
    if _should_print(key, _DEGRADE_THROTTLE, True, n):
        detail = ("%s: %s" % (type(exc).__name__, exc)) if exc is not None else ""
        tail = ("  # " + note) if note else ""
        extra = ("  (累计 %d 次)" % n) if n > 1 else ""
        print(f"[QUIET][DEGRADE] {key} {detail}{tail}{extra}")


def attention(where: str, exc=None, note: str = "") -> None:
    """登记一次**真问题**（不该发生的异常）。始终输出一行，同站点节流。"""
    n = _record(where, exc, note, "attention")
    key = str(where or "unknown")
    if _should_print(key, _ATTENTION_THROTTLE, True, n):
        detail = ("%s: %s" % (type(exc).__name__, exc)) if exc is not None else ""
        tail = ("  # " + note) if note else ""
        extra = ("  (累计 %d 次)" % n) if n > 1 else ""
        print(f"[QUIET][WARN] {key} {detail}{tail}{extra}")


def snapshot(limit: int = 50) -> dict:
    """运行期统计快照：总次数、各站点次数、最近记录（供诊断接口/界面）。"""
    with _lock:
        sites = sorted(_counts.items(), key=lambda kv: -kv[1])
        recent = list(_recent)[-max(1, int(limit or 50)):]
    return {
        "total": sum(_counts.values()),
        "sites": [{"where": k, "count": v} for k, v in sites],
        "recent": recent,
        "tracing": _TRACE,
    }


def reset() -> None:
    """清空统计（测试或手动重置用）。"""
    with _lock:
        _counts.clear()
        _last.clear()
        _recent.clear()


def tracing() -> bool:
    """当前是否开启降级明细输出。"""
    return _TRACE
