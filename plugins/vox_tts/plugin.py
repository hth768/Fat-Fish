# -*- coding: utf-8 -*-
"""本地 TTS 插件包装器：惰性加载 qq_bot 的 tts_vox.VoxTTSPlugin。"""
MODULE = "tts_vox"
CLS = "VoxTTSPlugin"


def create_plugin(core):
    import importlib
    mod = importlib.import_module(MODULE)
    return getattr(mod, CLS)(core)
