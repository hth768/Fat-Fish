# -*- coding: utf-8 -*-
"""统一启动入口：装配智能体核心 + 按配置/参数加载平台插件。

用法：
    python main.py                # 按 config.py 的 ENABLE_QQ_PLUGIN / ENABLE_CONSOLE_PLUGIN 启动
    python main.py --qq           # 只启用 QQ 平台
    python main.py --console      # 只启用控制台平台（无需 NapCat，终端直接聊天）
    python main.py --bili         # 只启用 B 站直播弹幕平台（需先填 BILIBILI_ROOM_ID）
    python main.py --qq --console # 同时启用多个平台（多平台并存）
    python main.py --web         # 启用浏览器前端交互界面（http://127.0.0.1:8800）
    python main.py --web --console # Web 前端 + 控制台并存

旧的 python bot.py 仍然可用（bot.py 已是本入口的兼容垫片）。
"""
import asyncio
import os
import subprocess
import sys
import time

import config

# 所有被本进程拉起的 sidecar 子进程句柄（退出时统一清理）
_SIDECARS = []


def _spawn_sidecar(script, enable, autospawn, host, port, name, extra_args=None):
    """通用 sidecar 拉起：按配置自动 Popen 一个本地服务脚本。

    拉起失败或不开启时静默返回（对应 client 会自动降级），绝不阻断主流程。
    """
    if not getattr(config, enable, False):
        return
    if not getattr(config, autospawn, True):
        return
    root = os.path.dirname(os.path.abspath(__file__))
    log_path = os.path.join(root, f"{name}_server.log")
    cmd = [sys.executable, script, "--host", str(host), "--port", str(port)]
    if extra_args:
        cmd += extra_args
    try:
        proc = subprocess.Popen(
            cmd, cwd=root,
            stdout=open(log_path, "a", encoding="utf-8"),
            stderr=subprocess.STDOUT,
        )
        _SIDECARS.append(proc)
        print(f"[MAIN] 已拉起 {name} sidecar (pid={proc.pid}) -> "
              f"http://{host}:{port}")
    except Exception as e:
        print(f"[WARN] {name} sidecar 拉起失败，将降级为进程内调用: {e}")


def _spawn_memory_server():
    """拉起记忆 sidecar（独立进程承载 vector_memory 等重负载）。"""
    _spawn_sidecar(
        "memory_server.py",
        enable="ENABLE_MEMORY_SERVER",
        autospawn="MEMORY_SERVER_AUTOSPAWN",
        host=getattr(config, "MEMORY_SERVER_HOST", "127.0.0.1"),
        port=getattr(config, "MEMORY_SERVER_PORT", 8766),
        name="memory",
        extra_args=["--modules", getattr(config, "MEMORY_SERVER_MODULES", "vector_memory")],
    )


def _spawn_monitor():
    """拉起监控 sidecar（对齐 N.E.K.O 的 monitor / 本地遥测进程）。"""
    mem_port = getattr(config, "MEMORY_SERVER_PORT", 8766)
    _spawn_sidecar(
        "monitor_server.py",
        enable="ENABLE_MONITOR",
        autospawn="MONITOR_AUTOSPAWN",
        host=getattr(config, "MONITOR_HOST", "127.0.0.1"),
        port=getattr(config, "MONITOR_PORT", 8770),
        name="monitor",
        extra_args=["--memory-url", f"http://127.0.0.1:{mem_port}"],
    )


async def _monitor_push_loop(core):
    """周期性把 bot 状态快照推送给 monitor（best-effort，失败静默）。"""
    from memory_client import server_healthy as mem_healthy
    from monitor_client import push_status
    interval = getattr(config, "MONITOR_PUSH_INTERVAL", 30)
    while True:
        await asyncio.sleep(interval)
        try:
            from ai_provider import provider_stats, vision_stats
            from telemetry import save as telemetry_save
            try:
                providers = provider_stats()
            except Exception:
                providers = {}
            try:
                vision = vision_stats()
            except Exception:
                vision = {}
            try:
                telemetry_save()
            except Exception:
                pass
            payload = {
                "core": core.status(),
                "memory_sidecar": await mem_healthy(),
                "providers": providers,
                "vision": vision,
                "ts": time.time(),
            }
            await push_status(payload)
        except Exception as e:
            print(f"[WARN] monitor 推送失败: {e}")


def _parse_platforms(argv) -> list:
    """从命令行解析平台列表；未指定时返回 None（走 config）。"""
    platforms = []
    if "--qq" in argv:
        platforms.append("qq")
    if "--console" in argv:
        platforms.append("console")
    if "--bili" in argv or "--bilibili" in argv:
        platforms.append("bilibili")
    if "--web" in argv:
        platforms.append("web")
    return platforms or None


async def run_bot():
    """bot 主体（不含 sidecar 拉起；由 launcher.py 或 main() 调用）。"""
    from agent_core import get_core

    core = get_core()
    core.register_builtin_plugins(platforms=_parse_platforms(sys.argv))
    await core.start()

    # 提示各平台接入状态
    for p in core.plugins.platforms():
        print(f"[MAIN] 平台插件已启用: {p.platform}（能力: "
              + ", ".join(k for k, v in p.capabilities.items() if v) + "）")

    # 后台预加载本地视频理解模型（可选功能，与平台无关）
    if getattr(config, "ENABLE_LOCAL_VL", False) and getattr(config, "PRELOAD_LOCAL_VL", False):
        async def _preload_model():
            try:
                from local_video_understand import local_vl
                local_vl.load()
                print("[MAIN] 本地视频理解模型已预加载")
            except Exception as e:
                print(f"[WARN] 本地模型预加载失败（将按需加载）: {e}")
        asyncio.create_task(_preload_model())

    # 后台周期性把状态推送给监控服务（best-effort，失败不影响主流程）
    if getattr(config, "ENABLE_MONITOR", False):
        asyncio.create_task(_monitor_push_loop(core))

    try:
        await asyncio.Future()  # 永久运行
    except asyncio.CancelledError:
        pass
    finally:
        await core.shutdown()
        # 退出时清理本进程拉起的 sidecar 子进程（launcher 托管时不在此列）
        for proc in _SIDECARS:
            try:
                proc.terminate()
            except Exception:
                pass


def main():
    """直接 `python main.py` 入口：自行拉起 sidecar 后启动 bot。

    若由 launcher.py 托管（已设 QQBOT_MANAGED），则跳过拉起避免重复。
    """
    if not os.environ.get("QQBOT_MANAGED"):
        _spawn_memory_server()
        _spawn_monitor()
    try:
        asyncio.run(run_bot())
    except KeyboardInterrupt:
        print("\n[INFO] 已退出")


if __name__ == "__main__":
    main()
