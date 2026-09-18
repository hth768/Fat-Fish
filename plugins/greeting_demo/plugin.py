# -*- coding: utf-8 -*-
"""本地插件示例（自包含）：定时问候。

编写一个自己的插件包：
1. 在 plugins/ 下新建文件夹，放一个 manifest.json（name/title/kind/schema_version…）
2. 放一个 plugin.py，提供 create_plugin(core)（平台/功能/本地包）或 create_brain(core)（大脑包）
3. App「插件」页点「重新扫描」，打开开关即可装载（核心运行中即时生效）

本示例不 import 任何 qq_bot 模块，通过**官方通道** `core.app_bridge` 向界面推事件：
    bridge = getattr(self.core, "app_bridge", None)
    if bridge:
        bridge.push(session, {"type": "message", "role": "assistant", "text": "..."})
- `push` 是线程安全的，可在任意线程/协程里调用；
- session 传 None 表示全局广播（所有订阅者都能收到）；
- 事件 type 可选：message / image / audio / tts / status / error（详见 PLUGINS.md）。
"""
import asyncio

try:
    from plugin_base import FeaturePlugin
except Exception:  # 脱离 qq_bot 环境时的降级基类
    class FeaturePlugin:
        def __init__(self, core=None):
            self.core = core
            self._started = False


DEFAULTS = {
    "interval_seconds": 600,
    "text": "（示例插件）第 {n} 次问候：主人还记得喝水吗～",
}


class GreetingDemoPlugin(FeaturePlugin):
    name = "greeting_demo"
    version = "1.0.0"

    def __init__(self, core):
        super().__init__(core)
        self._task = None
        self._cfg = dict(DEFAULTS)
        self._count = 0
        self._wake = None

    # ---- 参数热生效（App 插件页「设置」保存后即时回调）----
    def apply_config(self, cfg: dict):
        cfg = cfg or {}
        if "interval_seconds" in cfg:
            try:
                self._cfg["interval_seconds"] = max(10, int(cfg["interval_seconds"]))
            except Exception:
                pass
        if cfg.get("text"):
            self._cfg["text"] = str(cfg["text"])
        # 唤醒等待中的循环：改间隔后立刻按新值重新计时（否则要等旧 sleep 走完）
        ev = self._wake
        if ev is not None:
            try:
                ev.set()
            except Exception:
                pass

    # ---- 生命周期 ----
    async def start(self):
        self._wake = asyncio.Event()
        self._task = asyncio.get_event_loop().create_task(self._loop())
        await super().start()

    async def stop(self):
        if self._task:
            self._task.cancel()
            self._task = None
        self._wake = None
        await super().stop()

    # ---- 主循环（可被配置变更唤醒，重算等待时间）----
    async def _loop(self):
        while True:
            try:
                await asyncio.wait_for(self._wake.wait(),
                                       timeout=max(10, int(self._cfg["interval_seconds"])))
                self._wake.clear()
                continue
            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:
                raise
            self._count += 1
            self._greet()

    def _greet(self):
        """向 App 事件流推一条问候（拿不到桥就静默跳过，不报错）。"""
        bridge = getattr(self.core, "app_bridge", None)
        if bridge is None:
            return
        try:
            text = str(self._cfg["text"]).format(n=self._count)
        except Exception:
            text = f"（示例插件）第 {self._count} 次问候"
        try:
            bridge.push(None, {"type": "message", "role": "assistant", "text": text})
        except Exception:
            pass


_INSTANCE = None


def on_config(cfg: dict):
    """管理器在装载时与用户保存配置后调用（模块级，作用于当前实例）。"""
    inst = _INSTANCE
    if inst is not None and hasattr(inst, "apply_config"):
        inst.apply_config(cfg)


def create_plugin(core):
    global _INSTANCE
    _INSTANCE = GreetingDemoPlugin(core)
    return _INSTANCE
