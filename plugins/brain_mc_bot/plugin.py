# -*- coding: utf-8 -*-
"""原版 MC Bot 大脑包装器：惰性加载 qq_bot 的 agent_core.McBotBrain（外部进程形态）。"""
MODULE = "agent_core"
CLS = "McBotBrain"


def create_brain(core):
    import importlib
    mod = importlib.import_module(MODULE)
    return getattr(mod, CLS)(core)
