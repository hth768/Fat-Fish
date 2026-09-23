# -*- coding: utf-8 -*-
"""B 站私信插件包装器：惰性加载 qq_bot 的 bili_dm.BilibiliDmPlugin。"""
MODULE = "bili_dm"
CLS = "BilibiliDmPlugin"


def create_plugin(core):
    import importlib
    mod = importlib.import_module(MODULE)
    return getattr(mod, CLS)(core)
