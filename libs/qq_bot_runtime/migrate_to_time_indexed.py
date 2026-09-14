#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""数据迁移脚本：将 JSONL 历史迁移到 time_indexed_memory (SQLite)"""

import json
import os
import sys
import time
from pathlib import Path

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import time_indexed_memory


def migrate_jsonl_history():
    """迁移 chat_history/*.jsonl 到 time_indexed_memory"""
    history_dir = Path("chat_history")
    
    if not history_dir.exists():
        print("✗ chat_history 目录不存在，无需迁移")
        return False
    
    jsonl_files = list(history_dir.glob("*.jsonl"))
    if not jsonl_files:
        print("✗ 没有找到任何 .jsonl 文件")
        return False
    
    print(f"→ 发现 {len(jsonl_files)} 个用户的历史文件")
    
    # 初始化 time_indexed_memory
    tim = time_indexed_memory.get_time_indexed_memory()
    
    # 迁移每个用户的历史
    migrated_count = 0
    total_records = 0
    
    for jsonl_file in jsonl_files:
        user_id = jsonl_file.stem  # 文件名就是 user_id
        
        try:
            with open(jsonl_file, "r", encoding="utf-8") as f:
                lines = f.readlines()
            
            records_migrated = 0
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                
                try:
                    record = json.loads(line)
                    role = record.get("role", "")
                    content = record.get("content", "")
                    message_type = record.get("message_type", "")
                    time_str = record.get("time", "")
                    
                    # 解析时间字符串为 timestamp
                    if time_str:
                        try:
                            dt = time.strptime(time_str, "%Y-%m-%d %H:%M:%S")
                            timestamp = time.mktime(dt)
                        except Exception:
                            timestamp = time.time()
                    else:
                        timestamp = time.time()
                    
                    # 写入 SQLite
                    tim.append_history(user_id, role, content, message_type)
                    records_migrated += 1
                    
                except json.JSONDecodeError as e:
                    print(f"  ⚠ JSON 解析失败: {e}")
                    continue
            
            migrated_count += 1
            total_records += records_migrated
            print(f"  ✓ 用户 {user_id}: 迁移 {records_migrated} 条记录")
            
        except Exception as e:
            print(f"  ✗ 用户 {user_id} 迁移失败: {e}")
    
    print(f"\n→ 迁移完成: {migrated_count} 个用户, {total_records} 条记录")
    
    # 备份旧文件
    backup_dir = Path("chat_history_backup_" + str(int(time.time())))
    try:
        history_dir.rename(backup_dir)
        print(f"→ 旧文件已备份到: {backup_dir}")
    except Exception as e:
        print(f"⚠ 备份失败: {e}")
    
    return True


def verify_migration():
    """验证迁移结果"""
    print("\n→ 验证迁移结果...")
    
    tim = time_indexed_memory.get_time_indexed_memory()
    
    # 获取所有用户的历史数量
    memory_dir = Path("memory/time_indexed")
    if not memory_dir.exists():
        print("✗ time_indexed 目录不存在")
        return False
    
    db_files = list(memory_dir.glob("*.db"))
    if not db_files:
        print("✗ 没有找到任何数据库文件")
        return False
    
    print(f"✓ time_indexed_memory 中有 {len(db_files)} 个用户的数据库")
    
    total_records = 0
    for db_file in db_files[:10]:  # 只显示前10个
        user_id = db_file.stem
        count = tim.get_history_count(user_id)
        total_records += count
        print(f"  - 用户 {user_id}: {count} 条记录")
    
    if len(db_files) > 10:
        print(f"  ... 还有 {len(db_files) - 10} 个用户")
    
    print(f"\n总计: {total_records} 条记录")
    return True


def main():
    print("=" * 60)
    print("数据迁移：JSONL 历史 → time_indexed_memory (SQLite)")
    print("=" * 60)
    
    # 执行迁移
    success = migrate_jsonl_history()
    
    if success:
        # 验证迁移
        verify_migration()
        
        print("\n" + "=" * 60)
        print("✓ 迁移完成！")
        print("  现在可以使用 time_indexed_memory 进行历史存储和检索")
        print("=" * 60)
    else:
        print("\n✗ 迁移失败")
        sys.exit(1)


if __name__ == "__main__":
    main()
