# -*- coding: utf-8 -*-
"""B 站直播平台插件包装器：惰性加载 qq_bot 的 bilibili_plugin.BilibiliPlugin。"""
MODULE = "bilibili_plugin"
CLS = "BilibiliPlugin"


def create_plugin(core):
    import importlib
    mod = importlib.import_module(MODULE)
    return getattr(mod, CLS)(core)
