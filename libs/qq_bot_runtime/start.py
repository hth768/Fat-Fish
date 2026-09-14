# -*- coding: utf-8 -*-
"""一键拉起肥鱼娘全部主要功能（Web 控制台 + 核心 + 记忆/监控 sidecar）。

用法：
    python start.py              # 以网页控制台为主交互，拉起记忆/监控服务
    python start.py --web        # 透传参数给 bot（需结合 config 开关）

行为：
- 以「网页控制台」为主交互界面（ENABLE_WEB_PLUGIN=True）
- 启用记忆服务与监控服务（轻量、无外部依赖）
- 其余 heavy 功能（MC / PVZ / B站 / 本地VL / 屏幕感知 / TTS 等）保持 config 默认，
  可在网页「插件 / 服务」面板查看状态，并在 config.py 开启后由本启动器一并拉起

等价于：设好一组开关后调用 launcher.run()。
"""
import config

# —— 基础设施（最通用、无副作用，默认开启）——
config.ENABLE_WEB_PLUGIN = True       # 网页控制台作为主交互面
config.ENABLE_MEMORY_SERVER = True    # 记忆服务 sidecar
config.ENABLE_MONITOR = True           # 监控服务 sidecar

# —— 可选重型能力（默认关，避免无 key/硬件时报错；在 config.py 开启后一并拉起）——
config.ENABLE_TTS_SERVER = True     # 本地 TTS（隔离 venv_vox，较重；由 launcher 去重+守护，避免老机制重复拉起抢显存）
# config.ENABLE_QQ_PLUGIN = True      # QQ（NapCat）接入
# config.ENABLE_BILIBILI_PLUGIN = True  # B 站直播

import launcher
from launcher_core.bootstrap import configure_runtime_env, reexec_into_venv


if __name__ == "__main__":
    configure_runtime_env()
    reexec_into_venv()   # 仅在 __main__ 下可能切换进项目 .venv（import 时无效）
    launcher.main()
