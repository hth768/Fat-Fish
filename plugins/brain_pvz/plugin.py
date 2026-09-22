# -*- coding: utf-8 -*-
"""PVZ 大脑包装器：惰性加载 qq_bot 的 agent_core.PvzBrain。"""
MODULE = "agent_core"
CLS = "PvzBrain"


def create_brain(core):
    import importlib
    mod = importlib.import_module(MODULE)
    return getattr(mod, CLS)(core)
