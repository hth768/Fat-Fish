#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fact_store 性能测试脚本"""

import os
import sys
import time
import random
import string

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fact_store


def generate_random_text(length=50):
    """生成随机文本"""
    return ''.join(random.choices(string.ascii_letters + string.digits + '中文测试', k=length))


def test_write_performance(store, user_id, num_facts=100):
    """测试写入性能"""
    print(f"\n→ 测试写入性能 ({num_facts} 条事实)...")
    
    facts = [generate_random_text(random.randint(20, 100)) for _ in range(num_facts)]
    
    start = time.time()
    store.add_facts(user_id, facts)
    elapsed = time.time() - start
    
    print(f"  ✓ 写入 {num_facts} 条事实: {elapsed:.3f} 秒")
    print(f"  ✓ 平均每条: {elapsed/num_facts*1000:.2f} 毫秒")
    
    return elapsed


def test_read_performance(store, user_id, num_reads=100):
    """测试读取性能"""
    print(f"\n→ 测试读取性能 ({num_reads} 次读取)...")
    
    start = time.time()
    for _ in range(num_reads):
        facts = store.get_facts(user_id)
    elapsed = time.time() - start
    
    print(f"  ✓ 读取 {num_reads} 次: {elapsed:.3f} 秒")
    print(f"  ✓ 平均每次: {elapsed/num_reads*1000:.2f} 毫秒")
    
    return elapsed


def test_search_performance(store, user_id, num_searches=50):
    """测试 FTS5 搜索性能"""
    print(f"\n→ 测试 FTS5 搜索性能 ({num_searches} 次搜索)...")
    
    # 先获取一些事实作为搜索关键词
    all_facts = store.get_facts(user_id)
    if not all_facts:
        print("  ⚠ 没有事实可搜索")
        return 0
    
    # 随机选择一些事实的片段作为搜索词
    search_terms = []
    for _ in range(num_searches):
        fact = random.choice(all_facts)
        # 取事实的前10个字符作为搜索词
        term = fact['text'][:10]
        search_terms.append(term)
    
    start = time.time()
    result_count = 0
    for term in search_terms:
        results = store.search_facts(user_id, term, limit=10)
        result_count += len(results)
    elapsed = time.time() - start
    
    print(f"  ✓ 搜索 {num_searches} 次: {elapsed:.3f} 秒")
    print(f"  ✓ 平均每次: {elapsed/num_searches*1000:.2f} 毫秒")
    print(f"  ✓ 总结果数: {result_count}")
    
    return elapsed


def test_concurrent_performance(store, num_users=10, facts_per_user=50):
    """测试多用户并发性能"""
    print(f"\n→ 测试多用户并发性能 ({num_users} 个用户, 每用户 {facts_per_user} 条)...")
    
    start = time.time()
    
    for i in range(num_users):
        user_id = f"test_user_{i}"
        facts = [generate_random_text(random.randint(20, 100)) for _ in range(facts_per_user)]
        store.add_facts(user_id, facts)
    
    elapsed = time.time() - start
    
    total_facts = num_users * facts_per_user
    print(f"  ✓ 写入 {total_facts} 条事实 ({num_users} 用户): {elapsed:.3f} 秒")
    print(f"  ✓ 平均每条: {elapsed/total_facts*1000:.2f} 毫秒")
    
    return elapsed


def test_dedup_performance(store, user_id, num_duplicates=20):
    """测试去重性能"""
    print(f"\n→ 测试去重性能 (重复写入 {num_duplicates} 次相同事实)...")
    
    fact = generate_random_text(50)
    
    start = time.time()
    for _ in range(num_duplicates):
        store.add_facts(user_id, [fact])
    elapsed = time.time() - start
    
    # 验证只存储了一次
    facts = store.get_facts(user_id)
    count = sum(1 for f in facts if f['text'] == fact)
    
    print(f"  ✓ 重复写入 {num_duplicates} 次: {elapsed:.3f} 秒")
    print(f"  ✓ 实际存储次数: {count} (应为 1)")
    
    return elapsed


def cleanup_test_data(store):
    """清理测试数据"""
    print("\n→ 清理测试数据...")
    
    with store._get_connection() as conn:
        conn.execute("DELETE FROM facts WHERE user_id LIKE 'test_user_%' OR user_id = 'perf_test_user'")
    
    print("  ✓ 测试数据已清理")


def main():
    print("=" * 60)
    print("fact_store 性能测试")
    print("=" * 60)
    
    store = fact_store.get_fact_store()
    test_user = "perf_test_user"
    
    try:
        # 测试 1: 写入性能
        test_write_performance(store, test_user, num_facts=100)
        
        # 测试 2: 读取性能
        test_read_performance(store, test_user, num_reads=100)
        
        # 测试 3: FTS5 搜索性能
        test_search_performance(store, test_user, num_searches=50)
        
        # 测试 4: 多用户并发性能
        test_concurrent_performance(store, num_users=10, facts_per_user=50)
        
        # 测试 5: 去重性能
        test_dedup_performance(store, test_user, num_duplicates=20)
        
        print("\n" + "=" * 60)
        print("✓ 性能测试完成！")
        print("=" * 60)
        
    finally:
        # 清理测试数据
        cleanup_test_data(store)


if __name__ == "__main__":
    main()
