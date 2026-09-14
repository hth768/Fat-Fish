#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""测试 FTS5 中文搜索功能"""

import fact_store

store = fact_store.get_fact_store()

# 测试中文搜索
print("=== 测试中文搜索 ===")
results = store.search_facts('2190720017', '主人', limit=10)
print(f"搜索'主人': {len(results)} 条结果")
for r in results[:3]:
    print(f"  - {r['text'][:50]}")

print()

# 测试英文搜索
print("=== 测试英文搜索 ===")
results = store.search_facts('2190720017', 'MC', limit=10)
print(f"搜索'MC': {len(results)} 条结果")
for r in results[:3]:
    print(f"  - {r['text'][:50]}")

print()

# 测试混合搜索
print("=== 测试混合搜索 ===")
results = store.search_facts('2190720017', '喜欢', limit=10)
print(f"搜索'喜欢': {len(results)} 条结果")
for r in results[:3]:
    print(f"  - {r['text'][:50]}")
