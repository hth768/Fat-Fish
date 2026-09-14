#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""数据迁移脚本：将 user_profiles.json 迁移到 fact_store (SQLite FTS5)"""

import json
import os
import sys
import time

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fact_store


def migrate_user_profiles():
    """迁移 user_profiles.json 到 fact_store"""
    profiles_file = os.path.join(os.path.dirname(__file__), "user_profiles.json")
    
    if not os.path.exists(profiles_file):
        print("✗ user_profiles.json 不存在，无需迁移")
        return False
    
    print("→ 读取 user_profiles.json...")
    try:
        with open(profiles_file, "r", encoding="utf-8") as f:
            profiles = json.load(f)
    except Exception as e:
        print(f"✗ 读取失败: {e}")
        return False
    
    if not isinstance(profiles, dict):
        print("✗ 文件格式错误，期望 dict")
        return False
    
    print(f"→ 发现 {len(profiles)} 个用户档案")
    
    # 初始化 fact_store
    store = fact_store.get_fact_store()
    
    # 迁移每个用户的事实
    migrated_count = 0
    failed_users = []
    
    for user_id, profile_data in profiles.items():
        if not isinstance(profile_data, dict):
            print(f"  ⚠ 用户 {user_id} 数据格式错误，跳过")
            failed_users.append(user_id)
            continue
        
        facts = profile_data.get("facts", [])
        if not facts:
            continue
        
        try:
            # 添加事实
            store.add_facts(user_id, facts)
            migrated_count += 1
            print(f"  ✓ 用户 {user_id}: 迁移 {len(facts)} 条事实")
        except Exception as e:
            print(f"  ✗ 用户 {user_id} 迁移失败: {e}")
            failed_users.append(user_id)
    
    print(f"\n→ 迁移完成: {migrated_count}/{len(profiles)} 个用户成功")
    
    if failed_users:
        print(f"  ⚠ 失败用户: {', '.join(failed_users)}")
    
    # 备份旧文件
    backup_file = profiles_file + f".backup_{int(time.time())}"
    try:
        os.rename(profiles_file, backup_file)
        print(f"→ 旧文件已备份到: {backup_file}")
    except Exception as e:
        print(f"⚠ 备份失败: {e}")
    
    return True


def verify_migration():
    """验证迁移结果"""
    print("\n→ 验证迁移结果...")
    
    store = fact_store.get_fact_store()
    
    # 获取所有用户
    with store._get_connection() as conn:
        cursor = conn.execute("""
            SELECT user_id, COUNT(*) as fact_count
            FROM facts
            GROUP BY user_id
            ORDER BY fact_count DESC
        """)
        results = cursor.fetchall()
    
    if not results:
        print("✗ fact_store 为空")
        return False
    
    print(f"✓ fact_store 中有 {len(results)} 个用户")
    for row in results[:10]:  # 只显示前10个
        print(f"  - 用户 {row['user_id']}: {row['fact_count']} 条事实")
    
    if len(results) > 10:
        print(f"  ... 还有 {len(results) - 10} 个用户")
    
    return True


def main():
    print("=" * 60)
    print("数据迁移：user_profiles.json → fact_store (SQLite FTS5)")
    print("=" * 60)
    
    # 执行迁移
    success = migrate_user_profiles()
    
    if success:
        # 验证迁移
        verify_migration()
        
        print("\n" + "=" * 60)
        print("✓ 迁移完成！")
        print("  现在可以使用 fact_store 进行事实存储和检索")
        print("=" * 60)
    else:
        print("\n✗ 迁移失败")
        sys.exit(1)


if __name__ == "__main__":
    main()
