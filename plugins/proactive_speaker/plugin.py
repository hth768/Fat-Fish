# -*- coding: utf-8 -*-
"""主动说话插件包装器：惰性加载 qq_bot 的 agent_core.ProactiveSpeakerPlugin。"""
MODULE = "agent_core"
CLS = "ProactiveSpeakerPlugin"


def create_plugin(core):
    import importlib
    mod = importlib.import_module(MODULE)
    return getattr(mod, CLS)(core)
