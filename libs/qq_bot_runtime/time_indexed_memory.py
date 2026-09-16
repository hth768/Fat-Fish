# -*- coding: utf-8 -*-
"""时间索引记忆模块（参考 N.E.K.O 猫娘计划的时间索引设计）。

特性：
1. SQLite 时间索引 — 高效时间范围查询
2. Keyset pagination — 稳定读取，不受插入影响
3. per-user 数据库隔离 — 避免并发冲突
4. FTS5 全文索引 — 支持关键词搜索

存储结构：
- memory/time_indexed/<user_id>.db — 每个用户独立数据库
"""
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Dict, List, Optional, Tuple

import config
import agent_ctx


def _base_dir():
    """获取基础目录"""
    return os.path.dirname(os.path.abspath(__file__))


def _ns_base():
    return agent_ctx.ns_dir() or _base_dir()


def _memory_dir():
    """获取记忆存储目录（按智能体隔离）"""
    d = os.path.join(_ns_base(), "memory", "time_indexed")
    os.makedirs(d, exist_ok=True)
    return d


def _user_db_file(user_id: str) -> str:
    """获取用户数据库文件路径"""
    # 清理 user_id 中的非法字符
    safe_id = "".join(c if c.isalnum() or c in "-_" else "_" for c in str(user_id))
    return os.path.join(_memory_dir(), f"{safe_id}.db")


class TimeIndexedMemory:
    """时间索引记忆管理器（per-user 隔离）。
    
    特性：
    - SQLite 时间索引
    - Keyset pagination（稳定读取）
    - FTS5 全文索引
    - 进程级缓存
    """
    
    def __init__(self):
        self._connections: Dict[str, sqlite3.Connection] = {}
        self._lock = threading.RLock()
        self._cache: Dict[str, List[dict]] = {}  # user_id -> recent records
    
    def _get_connection(self, user_id: str) -> sqlite3.Connection:
        """获取用户数据库连接（带缓存）"""
        uid = str(user_id)
        
        with self._lock:
            if uid not in self._connections:
                db_file = _user_db_file(uid)
                conn = sqlite3.connect(db_file, timeout=10, check_same_thread=False)
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA journal_mode=WAL")  # WAL 模式，提升并发性能
                conn.execute("PRAGMA synchronous=NORMAL")
                
                # 初始化表结构
                self._init_tables(conn)
                
                self._connections[uid] = conn
            
            return self._connections[uid]
    
    def _init_tables(self, conn: sqlite3.Connection):
        """初始化数据库表结构"""
        # 主历史表
        conn.execute("""
            CREATE TABLE IF NOT EXISTS history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                message_type TEXT DEFAULT '',
                timestamp REAL NOT NULL,
                time_str TEXT NOT NULL
            )
        """)
        
        # 时间索引
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_history_timestamp 
            ON history(timestamp DESC)
        """)
        
        # FTS5 全文索引表
        conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS history_fts USING fts5(
                content,
                content='history',
                content_rowid='id',
                tokenize='unicode61'
            )
        """)
        
        # 触发器：保持 FTS 与主表同步
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS history_ai AFTER INSERT ON history BEGIN
                INSERT INTO history_fts(rowid, content)
                VALUES (new.id, new.content);
            END
        """)
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS history_ad AFTER DELETE ON history BEGIN
                INSERT INTO history_fts(history_fts, rowid, content)
                VALUES ('delete', old.id, old.content);
            END
        """)
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS history_au AFTER UPDATE ON history BEGIN
                INSERT INTO history_fts(history_fts, rowid, content)
                VALUES ('delete', old.id, old.content);
                INSERT INTO history_fts(rowid, content)
                VALUES (new.id, new.content);
            END
        """)
        
        conn.commit()
    
    def append_history(self, user_id: str, role: str, content: str, message_type: str = ""):
        """追加一条对话历史"""
        uid = str(user_id)
        now = time.time()
        time_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
        
        conn = self._get_connection(uid)
        conn.execute("""
            INSERT INTO history (role, content, message_type, timestamp, time_str)
            VALUES (?, ?, ?, ?, ?)
        """, (role, content, message_type, now, time_str))
        conn.commit()
        
        # 清除缓存
        self._cache.pop(uid, None)
    
    def get_recent_history(self, user_id: str, limit: int = 200) -> List[dict]:
        """获取最近的对话历史（Keyset pagination）"""
        uid = str(user_id)
        
        # 检查缓存
        cache_key = f"{uid}:{limit}"
        if cache_key in self._cache:
            return self._cache[cache_key]
        
        conn = self._get_connection(uid)
        cursor = conn.execute("""
            SELECT id, role, content, message_type, timestamp, time_str
            FROM history
            ORDER BY timestamp DESC, id DESC
            LIMIT ?
        """, (limit,))
        
        records = [
            {
                "id": row["id"],
                "role": row["role"],
                "content": row["content"],
                "message_type": row["message_type"],
                "timestamp": row["timestamp"],
                "time": row["time_str"],
            }
            for row in cursor
        ]
        
        # 反转使时间升序（最新的在最后）
        records.reverse()
        
        # 更新缓存
        self._cache[cache_key] = records
        return records
    
    def get_history_by_time_range(
        self, 
        user_id: str, 
        start_time: float, 
        end_time: float,
        limit: int = 100
    ) -> List[dict]:
        """按时间范围查询历史"""
        uid = str(user_id)
        
        conn = self._get_connection(uid)
        cursor = conn.execute("""
            SELECT id, role, content, message_type, timestamp, time_str
            FROM history
            WHERE timestamp >= ? AND timestamp <= ?
            ORDER BY timestamp ASC, id ASC
            LIMIT ?
        """, (start_time, end_time, limit))
        
        return [
            {
                "id": row["id"],
                "role": row["role"],
                "content": row["content"],
                "message_type": row["message_type"],
                "timestamp": row["timestamp"],
                "time": row["time_str"],
            }
            for row in cursor
        ]
    
    def search_history(self, user_id: str, keyword: str, limit: int = 10) -> List[dict]:
        """使用 FTS5 全文搜索历史"""
        uid = str(user_id)
        
        conn = self._get_connection(uid)
        cursor = conn.execute("""
            SELECT h.id, h.role, h.content, h.message_type, h.timestamp, h.time_str,
                   rank
            FROM history_fts fts
            JOIN history h ON h.id = fts.rowid
            WHERE history_fts MATCH ?
            ORDER BY rank
            LIMIT ?
        """, (keyword, limit))
        
        return [
            {
                "id": row["id"],
                "role": row["role"],
                "content": row["content"],
                "message_type": row["message_type"],
                "timestamp": row["timestamp"],
                "time": row["time_str"],
                "rank": row["rank"],
            }
            for row in cursor
        ]
    
    def get_history_count(self, user_id: str) -> int:
        """获取用户的历史记录数量"""
        uid = str(user_id)
        
        conn = self._get_connection(uid)
        cursor = conn.execute("SELECT COUNT(*) as count FROM history")
        row = cursor.fetchone()
        return row["count"] if row else 0
    
    def clear_cache(self, user_id: str = ""):
        """清除缓存"""
        if user_id:
            uid = str(user_id)
            keys_to_remove = [k for k in self._cache if k.startswith(f"{uid}:")]
            for key in keys_to_remove:
                self._cache.pop(key, None)
        else:
            self._cache.clear()
    
    def close_all(self):
        """关闭所有数据库连接"""
        with self._lock:
            for conn in self._connections.values():
                try:
                    conn.close()
                except Exception:
                    pass
            self._connections.clear()
            self._cache.clear()


# 按 agent 分桶的单例：{ agent_id: TimeIndexedMemory }
_time_indexed_memory: dict = {}


def get_time_indexed_memory(agent_id: "str | None" = None) -> TimeIndexedMemory:
    """获取（按智能体隔离的）TimeIndexedMemory 实例。agent_id 为空时取当前 agent 上下文。"""
    aid = agent_id or agent_ctx.current_agent() or "__default__"
    if aid not in _time_indexed_memory:
        _time_indexed_memory[aid] = TimeIndexedMemory()
    return _time_indexed_memory[aid]
