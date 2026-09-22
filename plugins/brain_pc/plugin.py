# -*- coding: utf-8 -*-
"""电脑操控大脑包装器：惰性加载 qq_bot 的 agent_core.PcBrain。"""
MODULE = "agent_core"
CLS = "PcBrain"


def create_brain(core):
    import importlib
    mod = importlib.import_module(MODULE)
    return getattr(mod, CLS)(core)
