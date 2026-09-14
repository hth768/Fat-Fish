# -*- coding: utf-8 -*-
"""肥鱼娘统一启动器（根门面，对齐 N.E.K.O 根 launcher.py）。

用法：
    python launcher.py                 # 编排 sidecar 就绪后启动 bot
    python launcher.py --web --console # 参数透传给 bot（main.py）

流程（与 N.E.K.O 一致）：环境自愈 -> 拉起 sidecar -> 等待就绪 ->
启动主程序 -> 监控状态 / 崩溃自动重启 + 优雅退出。

未开启任何 sidecar（config 里 ENABLE_MEMORY_SERVER / ENABLE_MONITOR 均为 False）
时退化成「直接启动 bot」，与旧 python main.py 行为一致。
"""
import os
import sys
from typing import List

from launcher_core.bootstrap import configure_runtime_env, reexec_into_venv
from launcher_core.runtime import Launcher, SidecarSpec
from urllib.parse import urlparse

import config

# 同一进程内，launcher 与 bot 共用此实例，供 Web 控制台统一管理与查询 sidecar。
ACTIVE_LAUNCHER = None


def _sidecar_spec_from_entry(e) -> SidecarSpec:
    """把注册表里的 sidecar 条目转成编排用的 SidecarSpec（清单是唯一事实来源）。"""
    ex = e.extra or {}

    def _host():
        if ex.get("host"):
            return ex["host"]
        return getattr(config, ex.get("host_switch", ""), "127.0.0.1")

    def _port():
        if ex.get("port"):
            return int(ex["port"])
        return int(getattr(config, ex.get("port_switch", ""), ex.get("port_default", 0)))

    extra_args: List[str] = []
    if e.name == "memory":
        extra_args = ["--modules", getattr(config, "MEMORY_SERVER_MODULES", "vector_memory")]
    elif e.name == "monitor":
        # 监控依赖记忆服务：先按清单解析记忆端口（杜绝两处各写一份默认值）
        mem = next((s for s in _sidecars_raw() if s.name == "memory"), None)
        mem_port = int(getattr(config, "MEMORY_SERVER_PORT",
                               (mem.extra.get("port_default", 8766) if mem else 8766)))
        extra_args = ["--memory-url", f"http://127.0.0.1:{mem_port}"]

    env = {}
    if e.name == "tts":
        # 端口数据源复用 VOXCPM_TTS_URL（与 tts_vox 客户端一致，零回归）
        pu = urlparse(getattr(config, "VOXCPM_TTS_URL", "http://127.0.0.1:8765"))
        host, port = pu.hostname or "127.0.0.1", pu.port or 8765
        env = {"VOXCPM_FFMPEG": str(getattr(config, "FFMPEG_PATH", "") or "")}
        return SidecarSpec(
            name=e.name, script=ex.get("script", f"{e.module}.py"),
            host=host, port=port,
            python=getattr(config, ex.get("python_switch", ""), ex.get("python_default", "")),
            env=env, required=bool(ex.get("required", False)), order=e.order,
        )

    return SidecarSpec(
        name=e.name, script=ex.get("script", f"{e.module}.py"),
        host=_host(), port=_port(), extra_args=extra_args,
        required=bool(ex.get("required", False)), order=e.order,
    )


def _sidecars_raw():
    """清单里的全部 sidecar 条目（未按开关过滤，供端口/依赖解析）。"""
    import plugin_registry as reg
    return reg.by_kind("sidecar")


def _build_specs():
    """按注册表清单构建 sidecar 编排规格（开关 / 端口 / 依赖顺序均来自清单）。"""
    import plugin_registry as reg
    specs = []
    for e in reg.enabled_specs("sidecar"):
        specs.append(_sidecar_spec_from_entry(e))
    if specs:
        print("[launcher] 注册表 sidecar: " + ", ".join(f"{s.name}(order={s.order})" for s in specs))
    return specs


def main():
    specs = _build_specs()
    # 标记由 launcher 托管：bot 主体不再自行拉起 sidecar（避免重复）
    os.environ.setdefault("QQBOT_MANAGED", "1")
    import main as bot_main

    def _bot_entry():
        sys.argv = [bot_main.__file__] + list(sys.argv[1:])
        import asyncio
        asyncio.run(bot_main.run_bot())

    root = os.path.dirname(os.path.abspath(__file__))
    global ACTIVE_LAUNCHER
    ACTIVE_LAUNCHER = Launcher(specs=specs, root=root)
    ACTIVE_LAUNCHER.run(_bot_entry)


if __name__ == "__main__":
    configure_runtime_env()
    reexec_into_venv()   # 仅在 __main__ 下可能切换进项目 .venv（import 时无效）
    main()
