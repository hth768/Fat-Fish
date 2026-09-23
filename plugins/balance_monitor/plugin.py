# -*- coding: utf-8 -*-
"""余额监控插件包装器：惰性加载 qq_bot 的 agent_core.BalanceMonitorPlugin。"""
MODULE = "agent_core"
CLS = "BalanceMonitorPlugin"


def create_plugin(core):
    import importlib
    mod = importlib.import_module(MODULE)
    return getattr(mod, CLS)(core)
