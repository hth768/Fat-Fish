# -*- coding: utf-8 -*-
"""B 站学习调度插件包装器：惰性加载 qq_bot 的 bili_learn_scheduler.BilibiliLearnScheduler。"""
MODULE = "bili_learn_scheduler"
CLS = "BilibiliLearnScheduler"


def create_plugin(core):
    import importlib
    mod = importlib.import_module(MODULE)
    return getattr(mod, CLS)(core)
