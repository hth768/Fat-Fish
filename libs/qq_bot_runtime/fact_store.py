# -*- coding: utf-8 -*-
"""事实存储模块（参考 N.E.K.O 猫娘计划的 FactStore 设计）。

性能优化：
1. SQLite FTS5 全文索引 — 支持语义搜索事实
2. 原子提交协议 — archive-first，crash-safe
3. SHA-256 去重 — 避免重复事实
4. 进程级缓存 — 减少磁盘 IO

存储结构：
- facts.db: SQLite 数据库，含 FTS5 虚拟表
- facts_archive.db: 归档数据库（原子提交时先写这里）
"""
import hashlib
import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Dict, List, Optional, Tuple

import config
import agent_ctx
from quiet import degrade


def _base_dir():
    return os.path.dirname(os.path.abspath(__file__))


def _ns_base():
    d = agent_ctx.ns_dir() or _base_dir()
    os.makedirs(d, exist_ok=True)
    return d


def _facts_db_file():
    return os.path.join(_ns_base(), "facts.db")


def _facts_archive_db_file():
    return os.path.join(_ns_base(), "facts_archive.db")


class FactStore:
    """事实存储管理器（per-user 隔离）。
    
    特性：
    - SQLite FTS5 全文索引
    - 原子提交（archive-first，crash-safe）
    - SHA-256 去重
    - 进程级缓存
    """
    
    def __init__(self):
        self._db_file = _facts_db_file()
        self._archive_file = _facts_archive_db_file()
        self._lock = threading.RLock()
        self._cache: Dict[str, List[dict]] = {}  # user_id -> facts
        self._init_db()
    
    def _init_db(self):
        """初始化数据库表结构。"""
        with self._get_connection() as conn:
            # 主事实表
            conn.execute("""
                CREATE TABLE IF NOT EXISTS facts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    fact_hash TEXT NOT NULL UNIQUE,
                    fact_text TEXT NOT NULL,
                    confidence REAL DEFAULT 1.0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
            """)
            
            # FTS5 全文索引表
            conn.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5(
                    user_id,
                    fact_text,
                    content='facts',
                    content_rowid='id',
                    tokenize='unicode61'
                )
            """)
            
            # 触发器：保持 FTS 与主表同步
            conn.execute("""
                CREATE TRIGGER IF NOT EXISTS facts_ai AFTER INSERT ON facts BEGIN
                    INSERT INTO facts_fts(rowid, user_id, fact_text)
                    VALUES (new.id, new.user_id, new.fact_text);
                END
            """)
            conn.execute("""
                CREATE TRIGGER IF NOT EXISTS facts_ad AFTER DELETE ON facts BEGIN
                    INSERT INTO facts_fts(facts_fts, rowid, user_id, fact_text)
                    VALUES ('delete', old.id, old.user_id, old.fact_text);
                END
            """)
            conn.execute("""
                CREATE TRIGGER IF NOT EXISTS facts_au AFTER UPDATE ON facts BEGIN
                    INSERT INTO facts_fts(facts_fts, rowid, user_id, fact_text)
                    VALUES ('delete', old.id, old.user_id, old.fact_text);
                    INSERT INTO facts_fts(rowid, user_id, fact_text)
                    VALUES (new.id, new.user_id, new.fact_text);
                END
            """)
            
            # 索引：加速按用户查询
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_facts_user_id 
                ON facts(user_id, updated_at DESC)
            """)
    
    @contextmanager
    def _get_connection(self):
        """获取数据库连接（上下文管理器）。"""
        conn = sqlite3.connect(self._db_file, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")  # WAL 模式，提升并发性能
        conn.execute("PRAGMA synchronous=NORMAL")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    
    def _compute_hash(self, user_id: str, fact_text: str) -> str:
        """计算事实的 SHA-256 哈希（用于去重）。"""
        content = f"{user_id}:{fact_text}"
        return hashlib.sha256(content.encode("utf-8")).hexdigest()
    
    def get_facts(self, user_id: str) -> List[dict]:
        """获取用户的所有事实（带缓存）。"""
        uid = str(user_id)
        
        # 检查缓存
        if uid in self._cache:
            return self._cache[uid]
        
        with self._get_connection() as conn:
            cursor = conn.execute("""
                SELECT fact_text, confidence, created_at, updated_at
                FROM facts
                WHERE user_id = ?
                ORDER BY updated_at DESC
            """, (uid,))
            
            facts = [
                {
                    "text": row["fact_text"],
                    "confidence": row["confidence"],
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                }
                for row in cursor
            ]
        
        # 更新缓存
        self._cache[uid] = facts
        return facts
    
    def add_facts(self, user_id: str, fact_texts: List[str]):
        """添加事实（原子提交，SHA-256 去重）。
        
        原子提交协议：
        1. 先写入归档数据库
        2. 归档成功后，再写入主数据库
        3. 如果中途失败，主数据库不受影响
        """
        uid = str(user_id)
        now = time.time()
        
        # 准备要插入的事实
        new_facts = []
        for text in fact_texts:
            text = text.strip()
            if not text:
                continue
            fact_hash = self._compute_hash(uid, text)
            new_facts.append({
                "hash": fact_hash,
                "text": text,
                "created_at": now,
                "updated_at": now,
            })
        
        if not new_facts:
            return
        
        # 原子提交：先写归档，再写主库
        try:
            self._archive_insert(uid, new_facts)
            self._main_insert(uid, new_facts)
        except Exception as e:
            print(f"[FACT_STORE] 添加事实失败: {e}")
            raise
        
        # 清除缓存
        self._cache.pop(uid, None)
    
    def _archive_insert(self, user_id: str, facts: List[dict]):
        """写入归档数据库（原子提交第一步）。"""
        archive_conn = sqlite3.connect(self._archive_file, timeout=10)
        try:
            archive_conn.execute("""
                CREATE TABLE IF NOT EXISTS archive_facts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    fact_hash TEXT NOT NULL,
                    fact_text TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    archived_at REAL NOT NULL
                )
            """)
            
            for fact in facts:
                archive_conn.execute("""
                    INSERT OR IGNORE INTO archive_facts
                    (user_id, fact_hash, fact_text, created_at, archived_at)
                    VALUES (?, ?, ?, ?, ?)
                """, (user_id, fact["hash"], fact["text"], fact["created_at"], time.time()))
            
            archive_conn.commit()
        finally:
            archive_conn.close()
    
    def _main_insert(self, user_id: str, facts: List[dict]):
        """写入主数据库（原子提交第二步）。"""
        with self._get_connection() as conn:
            for fact in facts:
                try:
                    conn.execute("""
                        INSERT OR IGNORE INTO facts
                        (user_id, fact_hash, fact_text, confidence, created_at, updated_at)
                        VALUES (?, ?, ?, 1.0, ?, ?)
                    """, (user_id, fact["hash"], fact["text"], fact["created_at"], fact["updated_at"]))
                except sqlite3.IntegrityError as e:
                    degrade("fact_store.FactStore._main_insert", e, "唯一约束冲突，已忽略重复事实")
    
    def update_fact_confidence(self, user_id: str, fact_text: str, confidence: float):
        """更新事实的置信度。"""
        uid = str(user_id)
        fact_hash = self._compute_hash(uid, fact_text)
        
        with self._get_connection() as conn:
            conn.execute("""
                UPDATE facts
                SET confidence = ?, updated_at = ?
                WHERE user_id = ? AND fact_hash = ?
            """, (confidence, time.time(), uid, fact_hash))
        
        # 清除缓存
        self._cache.pop(uid, None)
    
    def search_facts(self, user_id: str, query: str, limit: int = 10) -> List[dict]:
        """使用 FTS5 全文搜索事实。"""
        uid = str(user_id)
        
        with self._get_connection() as conn:
            # FTS5 搜索
            cursor = conn.execute("""
                SELECT f.fact_text, f.confidence, f.updated_at,
                       rank
                FROM facts_fts fts
                JOIN facts f ON f.id = fts.rowid
                WHERE facts_fts MATCH ? AND f.user_id = ?
                ORDER BY rank
                LIMIT ?
            """, (query, uid, limit))
            
            results = [
                {
                    "text": row["fact_text"],
                    "confidence": row["confidence"],
                    "updated_at": row["updated_at"],
                    "rank": row["rank"],
                }
                for row in cursor
            ]
        
        return results
    
    def delete_fact(self, user_id: str, fact_text: str):
        """删除事实。"""
        uid = str(user_id)
        fact_hash = self._compute_hash(uid, fact_text)
        
        with self._get_connection() as conn:
            conn.execute("""
                DELETE FROM facts
                WHERE user_id = ? AND fact_hash = ?
            """, (uid, fact_hash))
        
        # 清除缓存
        self._cache.pop(uid, None)
    
    def get_fact_count(self, user_id: str) -> int:
        """获取用户的事实数量。"""
        uid = str(user_id)
        
        with self._get_connection() as conn:
            cursor = conn.execute("""
                SELECT COUNT(*) as count
                FROM facts
                WHERE user_id = ?
            """, (uid,))
            row = cursor.fetchone()
            return row["count"] if row else 0
    
    def clear_cache(self, user_id: str = ""):
        """清除缓存。"""
        if user_id:
            self._cache.pop(str(user_id), None)
        else:
            self._cache.clear()


# 按 agent 分桶的单例：{ agent_id: FactStore }
_fact_store: dict = {}


def get_fact_store(agent_id: "str | None" = None) -> FactStore:
    """获取（按智能体隔离的）FactStore 实例。agent_id 为空时取当前 agent 上下文。"""
    aid = agent_id or agent_ctx.current_agent() or "__default__"
    if aid not in _fact_store:
        _fact_store[aid] = FactStore()
    return _fact_store[aid]


# ==================================================================
# 向后兼容：迁移旧数据
# ==================================================================

def migrate_from_json(json_file: str = ""):
    """从旧的 user_profiles.json 迁移数据到 SQLite。"""
    if not json_file:
        json_file = os.path.join(_base_dir(), "user_profiles.json")
    
    if not os.path.exists(json_file):
        return
    
    try:
        with open(json_file, "r", encoding="utf-8") as f:
            profiles = json.load(f)
        
        store = get_fact_store()
        
        for user_id, profile in profiles.items():
            facts = profile.get("facts", [])
            if facts:
                store.add_facts(user_id, facts)
        
        print(f"[FACT_STORE] 迁移完成: {len(profiles)} 个用户")
        
        # 重命名旧文件，避免重复迁移
        os.rename(json_file, json_file + ".migrated")
    except Exception as e:
        print(f"[FACT_STORE] 迁移失败: {e}")
