# -*- coding: utf-8 -*-
"""肥鱼娘统一启动器核心（对齐 N.E.K.O launcher_core）。

职责：环境自愈(bootstrap) + 多 sidecar 编排(runtime)。
根启动器门面见仓库根的 launcher.py；架构与 N.E.K.O 一致：
    spawn servers -> wait until ready -> start main -> monitor state。
"""
