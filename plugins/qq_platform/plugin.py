# -*- coding: utf-8 -*-
"""QQ 平台插件包装器：惰性加载 qq_bot 的 qq_plugin.QQPlugin。"""
MODULE = "qq_plugin"
CLS = "QQPlugin"


def create_plugin(core):
    import importlib
    mod = importlib.import_module(MODULE)
    return getattr(mod, CLS)(core)
