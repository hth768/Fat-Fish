# -*- coding: utf-8 -*-
"""肥鱼娘 App 入口：以库方式复用 qq_bot 的智能体核心，零源码改动。

启动流程：
1) 探测 qq_bot 运行时位置 -> 加入 sys.path，切换 cwd（数据文件按相对路径落盘）
2) 读取覆盖层 app_settings.json -> setattr 到 config 模块（App 模式默认全关外部功能）
3) 启动事件循环线程 -> 构造 CoreBridge -> 启动 HTTP 服务
4) 以桌面窗口打开控制台（pywebview / Edge App 窗口 / 浏览器三级降级），关闭窗口即退出

运行时位置解析（支持与源项目物理分离）：
1) 环境变量 FEIYU_QQ_BOT 指向的目录（最高优先）
2) f:/feiyu_app/runtime/qq_bot   —— APP 私有运行时副本（与源项目互不影响）
3) f:/qq_bot                     —— 源项目原位（默认）
"""
import argparse
import os
import sys
import time

APP_DIR = os.path.dirname(os.path.abspath(__file__))

# exe/pythonw（无控制台）运行时 sys.stdout/stderr 为 None，任何 print 都会崩：
# 一律重定向到日志文件（必须在第一处 print 之前执行）。
if sys.stdout is None or sys.stderr is None:
    _log_dir = os.path.join(APP_DIR, "data")
    os.makedirs(_log_dir, exist_ok=True)
    _log = open(os.path.join(_log_dir, "app_stdout.log"), "a",
                buffering=1, encoding="utf-8", errors="replace")
    if sys.stdout is None:
        sys.stdout = _log
    if sys.stderr is None:
        sys.stderr = _log


def resolve_qq_bot_dir() -> str:
    """定位 qq_bot 运行时目录（按优先级探测，找不到给出明确指引）。"""

    def _valid(path: str) -> bool:
        # 用 config.py 的存在性确认这是完整的 qq_bot 运行时
        return bool(path) and os.path.isfile(os.path.join(path, "config.py"))

    env = os.environ.get("FEIYU_QQ_BOT", "").strip()
    if env:
        env = os.path.abspath(env)
        if _valid(env):
            return env
        print(f"[APP][ERROR] 环境变量 FEIYU_QQ_BOT={env} 不是有效的 qq_bot 目录（缺 config.py）")
        sys.exit(1)

    candidates = [
        os.path.join(APP_DIR, "libs", "qq_bot_runtime"),     # 独立项目私有依赖库（libs/）
        os.path.join(APP_DIR, "runtime", "qq_bot"),          # 兼容旧布局
        r"f:\qq_bot",                                        # 源项目（备用）
    ]
    for c in candidates:
        if _valid(c):
            return c
    print("[APP][ERROR] 找不到 qq_bot 运行时。解决方式任选其一：")
    print("  1) 设置环境变量 FEIYU_QQ_BOT 指向 qq_bot 目录后重新启动")
    print("  2) 把完整 qq_bot 运行时放到 f:/feiyu_standalone/libs/qq_bot_runtime")
    sys.exit(1)


def bootstrap():
    """引导：路径、覆盖层生效（必须在 import config 之前完成 setattr 准备）。"""
    qq_bot_dir = resolve_qq_bot_dir()
    if qq_bot_dir not in sys.path:
        sys.path.insert(0, qq_bot_dir)
    os.chdir(qq_bot_dir)  # 相对路径数据文件落在运行时目录内
    print(f"[APP] 运行时: {qq_bot_dir}")

    sys.path.insert(0, APP_DIR)
    import settings_store
    settings_store.ensure_default_overlay()
    n = settings_store.apply_overlay()
    print(f"[APP] 覆盖层配置已应用 {n} 项（E:/feiyu_standalone/data/app_settings.json）")

    # 供应商路由重建：AI_PROVIDERS dict 在 config import 时用当时的 key 构建，
    # 覆盖层改 key 后必须把新凭据同步进 dict 再 reload（否则重启后仍是旧/空 key）。
    try:
        import config as _qq_config
        for _name, _p in (getattr(_qq_config, "AI_PROVIDERS", {}) or {}).items():
            if not isinstance(_p, dict):
                continue
            for _field, _suffix in (("api_key", "API_KEY"), ("base_url", "BASE_URL"),
                                    ("default_model", "MODEL")):
                _val = getattr(_qq_config, f"{_name.upper()}_{_suffix}", None)
                if _val:
                    _p[_field] = _val
        from ai_provider import reload_provider_config
        reload_provider_config()
        print("[APP] AI 供应商路由已按覆盖层重建")
    except Exception as _e:
        print(f"[APP][WARN] 供应商重建失败: {_e!r}")

    # 安全网：若引擎升级重新带回 telemetry.py 缺 import time/hmac 的旧 bug
    # （reporter 线程 5 秒即崩、HMAC 签名静默失败的根因），在模块对象上补齐。
    try:
        import time as _time_mod
        import hmac as _hmac_mod
        import telemetry as _telemetry_mod
        if not hasattr(_telemetry_mod, "time"):
            _telemetry_mod.time = _time_mod
        if not hasattr(_telemetry_mod, "hmac"):
            _telemetry_mod.hmac = _hmac_mod
    except Exception as e:
        try:
            from quiet import degrade
        except Exception:
            degrade = None
        if degrade is not None:
            degrade("app.bootstrap", e, "补齐 telemetry 依赖失败")
    return settings_store


def main():
    ap = argparse.ArgumentParser(description="肥鱼娘 App（复用 qq_bot 核心）")
    ap.add_argument("--port", type=int, default=0, help="HTTP 端口，默认取 app 设置 8900")
    ap.add_argument("--with-core", action="store_true", help="启动后立即拉起智能体核心")
    ap.add_argument("--no-window", action="store_true", help="不开桌面窗口（命令行模式，浏览器手动访问）")
    ap.add_argument("--browser", action="store_true", help="强制用系统默认浏览器（跳过原生窗口）")
    args = ap.parse_args()

    settings_store = bootstrap()

    # qq_bot 运行时路径已在 bootstrap() 里加入 sys.path，此刻才能 import 引擎模块。
    # （模块级 import 会在路径设置之前执行 → ModuleNotFoundError）
    from quiet import degrade

    from bridge.loop import LoopThread
    from bridge.core_bridge import CoreBridge
    from bridge import plugins_api, app_window
    import server as app_server

    app_cfg = settings_store.get_app_settings()
    port = args.port or int(app_cfg.get("port", 8900))

    lt = LoopThread()
    lt.start()
    bridge = CoreBridge(lt, port)

    # 插件包管理器：每个插件 = plugins/<包名>/ 独立目录，可插拔
    pkg = plugins_api.init(bridge)
    bridge.set_build_hook(pkg.attach_to)

    try:
        srv = app_server.start_server(bridge, port)
    except OSError as e:
        # Windows 独占绑定：端口被占（通常是已有 App 实例在跑）
        if getattr(e, "winerror", None) == 10048 or "10048" in str(e):
            msg = (f"端口 {port} 已被占用——已有一个肥鱼娘 App 在运行。\n\n"
                   "请使用已打开的那个窗口；或先关闭它再启动新实例。\n"
                   f"（也可以用 --port {port + 1} 启动第二个实例）")
            print(f"[APP][ERROR] {msg}")
            # 窗口模式下用户看不到控制台报错，弹系统消息框
            try:
                import ctypes
                ctypes.windll.user32.MessageBoxW(None, msg, "肥鱼娘 App", 0x40)
            except Exception as e:
                degrade("app.main", e, "弹错误对话框失败")
            sys.exit(1)
        raise
    url = f"http://127.0.0.1:{srv.server_address[1]}"
    print(f"[APP] 肥鱼娘 App 服务已就绪: {url}  (核心: {'已运行' if bridge.is_running() else '待启动'})")

    if args.with_core:
        print("[APP] 正在启动智能体核心...")
        r = bridge.start(wait=True)
        if r.get("ok"):
            started = pkg.ensure_sidecars()
            if started:
                print(f"[APP] sidecar 服务已启动: {', '.join(started)}")
            print("[APP] 智能体核心已启动")
        else:
            print(f"[APP] 核心启动失败: {r.get('error')}（可在界面重试）")

    exit_code = 0
    try:
        if args.no_window:
            # 命令行模式：HTTP 服务常驻，Ctrl+C 退出
            print(f"[APP] 控制台地址: {url}（Ctrl+C 退出）")
            while True:
                time.sleep(3600)
        elif args.browser:
            import webbrowser
            webbrowser.open(url)
            print(f"[APP] 浏览器模式: {url}（Ctrl+C 退出）")
            while True:
                time.sleep(3600)
        else:
            # 默认（无 --browser / --no-window）：三级降级
            #   1) APP 独立窗口（pywebview → Edge App）
            #   2) webui（系统默认浏览器）
            #   3) 命令行兜底（仅 HTTP 服务，Ctrl+C 退出）
            from bridge import appearance_api
            mode = app_window.try_app_window(url, title=appearance_api.get_window_title())
            if mode is None:
                # 独立窗口不可用 -> 降级 webui
                import webbrowser
                try:
                    webbrowser.open(url)
                    mode = "webui"
                    print(f"[APP] 已降级到 webui 模式: {url}")
                except Exception as e:
                    print(f"[APP][WARN] 浏览器打开失败: {e!r}")
            if mode is None:
                # webui 也不可用 -> 命令行兜底
                mode = "cli"
                print(f"[APP] 已降级到命令行模式: {url}（Ctrl+C 退出）")
            if mode in ("webui", "cli"):
                # 窗口/浏览器为独立进程，主进程需常驻保持 HTTP 服务
                print(f"[APP] {'webui' if mode == 'webui' else '控制台'}地址: {url}（Ctrl+C 退出）")
                while True:
                    time.sleep(3600)
            # mode 为 webview/edge-app 时窗口已在 try_app_window 内阻塞并随关闭退出
    except KeyboardInterrupt:
        print("[APP] 收到退出信号")
    finally:
        print("[APP] 正在停止...")
        try:
            pkg.stop_all_sidecars()
        except Exception as e:
            degrade("app.main.shutdown.stop_all_sidecars", e, "停止全部 sidecar 失败")
        try:
            bridge.stop(wait=True)
        except Exception as e:
            degrade("app.main.shutdown.bridge_stop", e, "停止核心桥失败")
        try:
            lt.stop()
        except Exception as e:
            degrade("app.main.shutdown.lt_stop", e, "停止事件循环失败")
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
