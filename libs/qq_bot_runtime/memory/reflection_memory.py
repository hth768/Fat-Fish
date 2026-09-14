# -*- coding: utf-8 -*-
"""反思记忆存储"""
import json
import os
from datetime import datetime
from typing import List, Dict, Optional
from pathlib import Path


class ReflectionMemory:
    """反思记忆存储管理"""

    def __init__(self, data_dir: str = "data"):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(exist_ok=True)
        self.reflections_file = self.data_dir / "reflections.json"
        self.stats_file = self.data_dir / "reflection_stats.json"

        self._reflections: List[Dict] = []
        self._stats: Dict = {}
        self._load()

    def _load(self):
        """加载反思数据"""
        if self.reflections_file.exists():
            try:
                with open(self.reflections_file, 'r', encoding='utf-8') as f:
                    self._reflections = json.load(f)
            except Exception as e:
                print(f"[REFLECT-MEM] 加载反思失败: {e}")
                self._reflections = []

        if self.stats_file.exists():
            try:
                with open(self.stats_file, 'r', encoding='utf-8') as f:
                    self._stats = json.load(f)
            except Exception as e:
                print(f"[REFLECT-MEM] 加载统计失败: {e}")
                self._stats = {}

    def _save(self):
        """保存反思数据"""
        try:
            with open(self.reflections_file, 'w', encoding='utf-8') as f:
                json.dump(self._reflections, f, ensure_ascii=False, indent=2)
            with open(self.stats_file, 'w', encoding='utf-8') as f:
                json.dump(self._stats, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[REFLECT-MEM] 保存失败: {e}")

    def add_reflection(self, reflection: Dict):
        """添加一条反思"""
        self._reflections.append(reflection)
        # 限制反思数量
        if len(self._reflections) > 500:
            self._reflections = self._reflections[-500:]
        self._save()

    def get_user_reflections(self, user_id: str, limit: int = 10) -> List[Dict]:
        """获取用户的反思"""
        user_refs = [r for r in self._reflections if r.get('user_id') == user_id]
        # 按时间倒序
        user_refs.sort(key=lambda x: x.get('timestamp', ''), reverse=True)
        return user_refs[:limit]

    def get_interaction_rules(self, user_id: str) -> List[Dict]:
        """获取用户的交互规则"""
        user_refs = self.get_user_reflections(user_id, limit=50)
        rules = [r for r in user_refs if r.get('actionable') and r.get('confidence', 0) >= 0.6]
        return rules

    def get_conversation_count(self, user_id: str) -> int:
        """获取用户对话次数"""
        key = f"{user_id}_conv_count"
        return self._stats.get(key, 0)

    def increment_conversation(self, user_id: str):
        """增加对话计数"""
        key = f"{user_id}_conv_count"
        self._stats[key] = self._stats.get(key, 0) + 1
        self._save()

    def get_last_reflection_time(self, user_id: str) -> int:
        """获取上次反思时的对话次数"""
        key = f"{user_id}_last_reflect"
        return self._stats.get(key, 0)

    def set_last_reflection_time(self, user_id: str, conv_count: int):
        """设置上次反思时的对话次数"""
        key = f"{user_id}_last_reflect"
        self._stats[key] = conv_count
        self._save()

    def get_all_user_ids(self) -> List[str]:
        """获取所有有反思记录的用户ID"""
        user_ids = set()
        for r in self._reflections:
            uid = r.get('user_id')
            if uid:
                user_ids.add(uid)
        return list(user_ids)

    def clear_user_reflections(self, user_id: str):
        """清空用户的反思"""
        self._reflections = [r for r in self._reflections if r.get('user_id') != user_id]
        self._save()


# 全局单例
_reflection_memory = None

def get_reflection_memory() -> ReflectionMemory:
    global _reflection_memory
    if _reflection_memory is None:
        _reflection_memory = ReflectionMemory()
    return _reflection_memory
