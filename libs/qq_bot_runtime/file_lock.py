# -*- coding: utf-8 -*-
"""跨进程文件锁（memory/user_profiles/important_notes/identity 等整文件读改写防丢更新）。

核心进程（QQ 聊天大脑）与 MC bot 进程（mc_bot_run.py）会同时写
user_profiles.json / important_notes.json / identity_bindings.json，
load-modify-save 全程需要互斥，否则一方覆盖另一方的写入。

用法：
    with file_lock("user_profiles.json"):
        data = load(); data[...] = ...; save(data)
"""
import contextlib
import os
import threading
import time

try:
    import msvcrt  # Windows
except ImportError:  # pragma: no cover - 其他平台
    msvcrt = None

_LOCK_SUFFIX = ".lock"
_RETRY_INTERVAL = 0.05

# 进程内重入支持：同进程对同一文件多次加锁（如 seed 在调用方锁内触发）不重复上锁，
# 只计数；最外层退出时才真正释放。Windows 字节区锁对同进程第二个句柄会失败，
# 必须自己做重入管理。
_depth_lock = threading.Lock()
_lock_depth = {}  # lock_path -> 重入层数
_lock_handles = {}  # lock_path -> 最外层已上锁的句柄

try:
    _thread_local = threading.local()
except Exception:  # pragma: no cover
    _thread_local = None


def _lock_path(target_path: str) -> str:
    return str(target_path) + _LOCK_SUFFIX


@contextlib.contextmanager
def file_lock(target_path: str, timeout: float = 10.0):
    """对 target_path 对应的整文件读写加跨进程锁。

    - Windows: msvcrt.locking 锁 1 字节（需要文件至少 1 字节可锁区域）
    - 其他平台: 用原子创建锁文件模拟（fcntl 不可用时的兜底）
    - 进程内可重入（同路径嵌套只计数），跨进程互斥。
    超时抛 RuntimeError。
    """
    path = _lock_path(target_path)
    with _depth_lock:
        if path in _lock_depth:
            # 本进程已持有该锁：只加深计数，不再重复上锁
            _lock_depth[path] += 1
            try:
                yield
            finally:
                with _depth_lock:
                    _lock_depth[path] -= 1
                    if _lock_depth[path] <= 0:
                        _lock_depth.pop(path, None)
                        _lock_handles.pop(path, None)
            return

    # ---------- 真正加锁（最外层） ----------
    if msvcrt is not None:
        f = open(path, "a+b")
        try:
            if os.path.getsize(path) < 1:
                f.write(b"\x00")
                f.flush()
            f.seek(0)
            deadline = time.time() + timeout
            while True:
                try:
                    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.time() > deadline:
                        raise RuntimeError(f"等待文件锁超时: {path}")
                    time.sleep(_RETRY_INTERVAL)
            with _depth_lock:
                _lock_depth[path] = 1
                _lock_handles[path] = f
            try:
                yield
            finally:
                f.seek(0)
                try:
                    msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
                with _depth_lock:
                    _lock_depth.pop(path, None)
                    _lock_handles.pop(path, None)
        finally:
            f.close()
        return

    # 非 Windows 兜底：独占创建锁文件（带过期重试）
    deadline = time.time() + timeout
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except OSError:
            if time.time() > deadline:
                raise RuntimeError(f"等待文件锁超时: {path}")
            time.sleep(_RETRY_INTERVAL)
    with _depth_lock:
        _lock_depth[path] = 1
        _lock_handles[path] = fd
    try:
        os.write(fd, str(os.getpid()).encode())
        yield
    finally:
        os.close(fd)
        try:
            os.remove(path)
        except OSError:
            pass
        with _depth_lock:
            _lock_depth.pop(path, None)
            _lock_handles.pop(path, None)
