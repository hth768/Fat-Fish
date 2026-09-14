#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""时间索引记忆性能测试脚本

对比 JSONL 和 SQLite (time_indexed_memory) 的性能差异
"""

import os
import sys
import time
import json
import random
import string

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import time_indexed_memory


def generate_random_text(length=100):
    """生成随机文本"""
    return ''.join(random.choices(string.ascii_letters + string.digits + '中文测试', k=length))


def test_write_performance(tim, user_id, num_records=1000):
    """测试写入性能"""
    print(f"\n→ 测试写入性能 ({num_records} 条记录)...")
    
    start = time.time()
    for i in range(num_records):
        role = "user" if i % 2 == 0 else "assistant"
        content = generate_random_text(random.randint(50, 200))
        message_type = "private" if i % 3 == 0 else "group"
        tim.append_history(user_id, role, content, message_type)
    elapsed = time.time() - start
    
    print(f"  ✓ 写入 {num_records} 条记录: {elapsed:.3f} 秒")
    print(f"  ✓ 平均每条: {elapsed/num_records*1000:.2f} 毫秒")
    
    return elapsed


def test_read_performance(tim, user_id, num_reads=100, limit=200):
    """测试读取性能"""
    print(f"\n→ 测试读取性能 ({num_reads} 次读取, 每次 {limit} 条)...")
    
    start = time.time()
    for _ in range(num_reads):
        records = tim.get_recent_history(user_id, limit)
    elapsed = time.time() - start
    
    print(f"  ✓ 读取 {num_reads} 次: {elapsed:.3f} 秒")
    print(f"  ✓ 平均每次: {elapsed/num_reads*1000:.2f} 毫秒")
    print(f"  ✓ 返回记录数: {len(records)}")
    
    return elapsed


def test_time_range_performance(tim, user_id, num_queries=50):
    """测试时间范围查询性能"""
    print(f"\n→ 测试时间范围查询性能 ({num_queries} 次查询)...")
    
    # 获取当前时间
    now = time.time()
    
    start = time.time()
    for i in range(num_queries):
        # 查询不同时间范围
        start_time = now - (i + 1) * 3600  # 1小时前
        end_time = now - i * 3600  # 当前小时
        records = tim.get_history_by_time_range(user_id, start_time, end_time, limit=100)
    elapsed = time.time() - start
    
    print(f"  ✓ 查询 {num_queries} 次: {elapsed:.3f} 秒")
    print(f"  ✓ 平均每次: {elapsed/num_queries*1000:.2f} 毫秒")
    
    return elapsed


def test_search_performance(tim, user_id, num_searches=50):
    """测试 FTS5 搜索性能"""
    print(f"\n→ 测试 FTS5 搜索性能 ({num_searches} 次搜索)...")
    
    # 先获取一些记录作为搜索关键词
    all_records = tim.get_recent_history(user_id, limit=100)
    if not all_records:
        print("  ⚠ 没有记录可搜索")
        return 0
    
    # 随机选择一些记录的内容片段作为搜索词
    search_terms = []
    for _ in range(num_searches):
        record = random.choice(all_records)
        content = record['content']
        # 取内容的前10个字符作为搜索词
        term = content[:10]
        search_terms.append(term)
    
    start = time.time()
    result_count = 0
    for term in search_terms:
        results = tim.search_history(user_id, term, limit=10)
        result_count += len(results)
    elapsed = time.time() - start
    
    print(f"  ✓ 搜索 {num_searches} 次: {elapsed:.3f} 秒")
    print(f"  ✓ 平均每次: {elapsed/num_searches*1000:.2f} 毫秒")
    print(f"  ✓ 总结果数: {result_count}")
    
    return elapsed


def test_concurrent_performance(num_users=10, records_per_user=100):
    """测试多用户并发性能"""
    print(f"\n→ 测试多用户并发性能 ({num_users} 个用户, 每用户 {records_per_user} 条)...")
    
    tim = time_indexed_memory.get_time_indexed_memory()
    
    start = time.time()
    
    for i in range(num_users):
        user_id = f"perf_test_user_{i}"
        for j in range(records_per_user):
            role = "user" if j % 2 == 0 else "assistant"
            content = generate_random_text(random.randint(50, 200))
            tim.append_history(user_id, role, content, "private")
    
    elapsed = time.time() - start
    
    total_records = num_users * records_per_user
    print(f"  ✓ 写入 {total_records} 条记录 ({num_users} 用户): {elapsed:.3f} 秒")
    print(f"  ✓ 平均每条: {elapsed/total_records*1000:.2f} 毫秒")
    
    # 清理测试数据
    for i in range(num_users):
        user_id = f"perf_test_user_{i}"
        db_file = time_indexed_memory._user_db_file(user_id)
        if os.path.exists(db_file):
            try:
                os.remove(db_file)
            except Exception:
                pass
    
    return elapsed


def cleanup_test_data():
    """清理测试数据"""
    print("\n→ 清理测试数据...")
    
    # 清理性能测试用户的数据库
    test_users = ["perf_test_user"]
    for i in range(10):
        test_users.append(f"perf_test_user_{i}")
    
    for user_id in test_users:
        db_file = time_indexed_memory._user_db_file(user_id)
        if os.path.exists(db_file):
            try:
                os.remove(db_file)
            except Exception as e:
                print(f"  ⚠ 清理 {user_id} 失败: {e}")
    
    print("  ✓ 测试数据已清理")


def main():
    print("=" * 60)
    print("时间索引记忆性能测试")
    print("=" * 60)
    
    tim = time_indexed_memory.get_time_indexed_memory()
    test_user = "perf_test_user"
    
    try:
        # 测试 1: 写入性能
        test_write_performance(tim, test_user, num_records=1000)
        
        # 测试 2: 读取性能
        test_read_performance(tim, test_user, num_reads=100, limit=200)
        
        # 测试 3: 时间范围查询性能
        test_time_range_performance(tim, test_user, num_queries=50)
        
        # 测试 4: FTS5 搜索性能
        test_search_performance(tim, test_user, num_searches=50)
        
        # 测试 5: 多用户并发性能
        test_concurrent_performance(num_users=10, records_per_user=100)
        
        print("\n" + "=" * 60)
        print("✓ 性能测试完成！")
        print("=" * 60)
        
    finally:
        # 清理测试数据
        cleanup_test_data()
        
        # 关闭所有连接
        tim.close_all()


if __name__ == "__main__":
    main()
