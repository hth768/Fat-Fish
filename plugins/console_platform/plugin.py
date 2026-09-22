# -*- coding: utf-8 -*-
"""控制台平台插件包装器：惰性加载 qq_bot 的 console_plugin.ConsolePlugin。"""
MODULE = "console_plugin"
CLS = "ConsolePlugin"


def create_plugin(core):
    import importlib
    mod = importlib.import_module(MODULE)
    return getattr(mod, CLS)(core)
