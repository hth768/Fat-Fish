# -*- coding: utf-8 -*-
"""兼容垫片：会话管理已并入 memory.py。

合并动因：原 session_manager.Session 与 memory.Memory 的读写方法逐方法同名同义，
但两者各缺一半——Memory 有持久化无会话切换，SessionManager 有会话切换而 Session
无持久化（重启即丢）。现让 Session 继承 Memory，同时具备「持久化 + 会话切换」。

对外符号保持不变（既有 `from session_manager import SessionManagerAdapter` 不破）：
    Session                —— 持久化会话（= Memory + 会话元信息 + 预热）
    SessionManager         —— 按 user_id 的会话注册表 + 热切换
    SessionManagerAdapter  —— ChatService.memory 接口
    get_session_manager()  —— 全局单例

新代码建议直接从 memory 导入。
"""
from memory import (                      # noqa: F401
    Session,
    SessionManager,
    SessionManagerAdapter,
    get_session_manager,
)

__all__ = [
    "Session",
    "SessionManager",
    "SessionManagerAdapter",
    "get_session_manager",
]
