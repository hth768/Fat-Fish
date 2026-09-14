# -*- coding: utf-8 -*-
"""插件包管理器：每个插件 = plugins/<包名>/ 目录（manifest.json + plugin.py）。

与 qq_bot 内建注册表（plugin_registry）解耦：
- 元数据来自 manifest.json（不执行代码即可列出全部插件）
- 实现由包装器 plugin.py 提供：
    平台/功能包:  create_plugin(core)  -> Plugin 实例（注册进 core.plugins）
    大脑包:       create_brain(core)   -> AgentBrain 实例（注册进 core.brains）
    sidecar 包:   无包装器，manifest 声明 script/host/port，由 SidecarProcess 子进程运行
    本地包:       create_plugin(core)（自包含实现，不包装 qq_bot 模块）

启用状态存覆盖层 pkg_plugins: {包名: bool}；装载平台/功能包时同步把
manifest.switch 置 True（底层模块会检查 config 开关），卸载时置 False。
核心构建期由 core_bridge 调 attach_to() 预注册已启用包（避免内建注册表双注册）。
"""
import importlib.util
import json
import os
import sys

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACKAGE_DIR = os.path.join(APP_DIR, "plugins")

try:
    from .sidecar_runner import SidecarProcess
except ImportError:
    from sidecar_runner import SidecarProcess

import settings_store


# ----------------------------------------------------------------------
# 覆盖层：包启用表
# ----------------------------------------------------------------------
def _enabled_map() -> dict:
    overlay = settings_store.load_overlay()
    m = overlay.get("pkg_plugins")
    return m if isinstance(m, dict) else {}


def _set_enabled(name: str, on: bool):
    with settings_store._lock:
        overlay = settings_store.load_overlay()
        m = overlay.get("pkg_plugins") if isinstance(overlay.get("pkg_plugins"), dict) else {}
        m[name] = bool(on)
        overlay["pkg_plugins"] = m
        settings_store.save_overlay(overlay)


def _set_switch(switch: str, on: bool):
    """把底层 config 开关写进覆盖层并立即生效。"""
    if not switch:
        return
    settings_store.set_values({switch: bool(on)})
    settings_store.apply_value(switch, bool(on))


# ----------------------------------------------------------------------
# 包扫描与元数据
# ----------------------------------------------------------------------
def _manifest_path(pkg_dir: str) -> str:
    return os.path.join(pkg_dir, "manifest.json")


def read_manifest(pkg_dir: str) -> dict:
    try:
        with open(_manifest_path(pkg_dir), "r", encoding="utf-8") as f:
            m = json.load(f)
        if isinstance(m, dict) and m.get("name"):
            return m
    except Exception:
        pass
    return None


def scan_packages() -> list:
    """扫描插件包目录（只读 manifest，不执行代码）。"""
    out = []
    if not os.path.isdir(PACKAGE_DIR):
        return out
    enabled = _enabled_map()
    for fn in sorted(os.listdir(PACKAGE_DIR)):
        pkg_dir = os.path.join(PACKAGE_DIR, fn)
        if not os.path.isdir(pkg_dir) or fn.startswith(("_", ".")):
            continue
        meta = read_manifest(pkg_dir)
        if not meta:
            continue
        name = meta["name"]
        out.append({
            "name": name,
            "title": meta.get("title", name),
            "kind": meta.get("kind", "feature"),
            "version": meta.get("version", "1.0.0"),
            "description": meta.get("description", ""),
            "switch": meta.get("switch", ""),
            "plugin_key": meta.get("plugin", name),      # 在 core.plugins/brains 里的键
            "requires": list(meta.get("requires") or []),
            "optional_requires": list(meta.get("optional_requires") or []),
            "sidecar": meta.get("sidecar"),               # sidecar 包: {script,host,port}
            "builtin": bool(meta.get("builtin")),         # True=主体内置，不可卸载
            "dir": pkg_dir.replace("\\", "/"),
            "enabled": bool(enabled.get(name, meta.get("default_on", False))),
        })
    return out


def get_package(name: str):
    for p in scan_packages():
        if p["name"] == name:
            return p
    return None


def _load_wrapper(pkg_dir: str, name: str):
    """执行包装器 plugin.py（惰性：只有装载时才 import qq_bot 重模块）。"""
    path = os.path.join(pkg_dir, "plugin.py")
    if not os.path.isfile(path):
        raise FileNotFoundError(f"包装器缺失: {path}")
    spec = importlib.util.spec_from_file_location(f"feiyu_pkg_{name}", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# ----------------------------------------------------------------------
# 包管理器
# ----------------------------------------------------------------------
class PackageManager:
    """插件包的装载/卸载/状态（绑定一个 CoreBridge）。"""

    def __init__(self, bridge):
        self.bridge = bridge
        self._sidecars = {}   # name -> SidecarProcess

    # ---- 依赖 ----
    def missing_deps(self, meta: dict) -> list:
        enabled = _enabled_map()
        missing = []
        for dep in (meta.get("requires") or []):
            dep_pkg = get_package(dep)
            if dep_pkg is None:
                missing.append(f"{dep}(不存在)")
            elif not enabled.get(dep, dep_pkg.get("default_on", False)):
                missing.append(dep)
        return missing

    # ---- 装载 ----
    def load(self, name: str) -> dict:
        meta = get_package(name)
        if meta is None:
            return {"ok": False, "error": f"插件包不存在: {name}"}
        if meta["builtin"]:
            return {"ok": False, "error": f"{name} 是主体内置能力，不可作为插件装载"}
        if meta["kind"] == "sidecar":
            return self._load_sidecar(meta)

        core = self.bridge.core
        if core is None:
            return {"ok": False, "error": "核心未构建（请先启动核心）"}
        miss = self.missing_deps(meta)
        if miss:
            return {"ok": False, "error": f"依赖未启用: {', '.join(miss)}（请先启用依赖包）"}
        try:
            wrapper = _load_wrapper(meta["dir"], name)
            if meta["kind"] == "brain":
                inst = wrapper.create_brain(core)
                core.brains.register(inst)
            else:
                _set_switch(meta["switch"], True)
                inst = wrapper.create_plugin(core)
                core.plugins.register(inst)
                if self.bridge.is_running():
                    self.bridge.lt.schedule(inst.start())
            _set_enabled(name, True)
            return {"ok": True, "applied": True, "hint": "已装载" }
        except Exception as e:
            return {"ok": False, "error": f"装载失败: {e!r}"}

    def _load_sidecar(self, meta: dict) -> dict:
        sc_cfg = meta.get("sidecar") or {}
        sc = SidecarProcess(name=meta["name"], title=meta["title"],
                            script=sc_cfg.get("script", ""),
                            host=sc_cfg.get("host", "127.0.0.1"),
                            port=sc_cfg.get("port", 0))
        r = sc.start()
        if r.get("ok"):
            _set_switch(meta["switch"], True) if meta.get("switch") else None
            _set_enabled(meta["name"], True)
            self._sidecars[meta["name"]] = sc
        return r

    # ---- 卸载 ----
    def unload(self, name: str) -> dict:
        meta = get_package(name)
        if meta is None:
            return {"ok": False, "error": f"插件包不存在: {name}"}
        if meta["builtin"]:
            return {"ok": False, "error": f"{name} 是主体内置能力，不可卸载"}
        if meta["kind"] == "sidecar":
            sc = self._sidecars.pop(name, None)
            if sc:
                sc.stop()
            if meta.get("switch"):
                _set_switch(meta["switch"], False)
            _set_enabled(name, False)
            return {"ok": True, "applied": True}

        core = self.bridge.core
        try:
            if meta["kind"] == "brain" and core is not None:
                b = core.brains.get(meta["plugin_key"])
                if b is not None:
                    if self.bridge.lt:
                        self.bridge.lt.schedule(b.stop())
                    core.brains._plugins.pop(meta["plugin_key"], None)
            elif core is not None:
                p = core.plugins.get(meta["plugin_key"])
                if p is not None:
                    if self.bridge.lt:
                        self.bridge.lt.schedule(p.stop())
                    core.plugins._plugins.pop(meta["plugin_key"], None)
                _set_switch(meta["switch"], False)
        except Exception as e:
            _set_enabled(name, False)
            return {"ok": True, "applied": False, "error": f"卸载时异常(已记为停用): {e!r}"}
        _set_enabled(name, False)
        return {"ok": True, "applied": True}

    # ---- 核心构建期：预注册已启用的插件/大脑包（core.start 之前调用）----
    def attach_to(self, core) -> list:
        loaded, failed = [], []
        enabled = _enabled_map()
        for meta in scan_packages():
            name = meta["name"]
            if meta["builtin"] or meta["kind"] == "sidecar":
                continue
            if not enabled.get(name):
                # 包被停用：若内建注册表已把同键大脑注册进来，移除之（保持「可插拔」语义）
                if meta["kind"] == "brain":
                    try:
                        core.brains._plugins.pop(meta["plugin_key"], None)
                    except Exception:
                        pass
                continue
            try:
                wrapper = _load_wrapper(meta["dir"], name)
                if meta["kind"] == "brain":
                    core.brains.register(wrapper.create_brain(core))
                else:
                    _set_switch(meta["switch"], True)
                    core.plugins.register(wrapper.create_plugin(core))
                loaded.append(name)
            except Exception as e:
                failed.append(f"{name}: {e!r}")
                print(f"[PKG] 预装载失败 {name}: {e!r}")
        if loaded:
            print(f"[PKG] 插件包已预装载: {', '.join(loaded)}")
        return loaded

    # ---- 核心启动后：拉起已启用的 sidecar ----
    def ensure_sidecars(self) -> list:
        started = []
        enabled = _enabled_map()
        for meta in scan_packages():
            if meta["kind"] != "sidecar" or not enabled.get(meta["name"]):
                continue
            if meta["name"] in self._sidecars and self._sidecars[meta["name"]].alive():
                continue
            r = self._load_sidecar(meta)
            if r.get("ok"):
                started.append(meta["name"])
        return started

    # ---- 状态 ----
    def sidecar_status(self, name: str):
        sc = self._sidecars.get(name)
        return sc.status() if sc else None

    def stop_all_sidecars(self):
        for sc in list(self._sidecars.values()):
            try:
                sc.stop()
            except Exception:
                pass
        self._sidecars.clear()

    def list_all(self) -> dict:
        """聚合视图：包清单 + 运行态 + 依赖校验。"""
        enabled = _enabled_map()
        core = self.bridge.core
        items = []
        for meta in scan_packages():
            item = dict(meta)
            item["missing_deps"] = self.missing_deps(meta)
            if meta["kind"] == "sidecar":
                sc = self.sidecar_status(meta["name"])
                item["running"] = bool(sc and sc["running"])
                item["detail"] = sc or {}
            elif core is not None:
                if meta["kind"] == "brain":
                    b = core.brains.get(meta["plugin_key"])
                    item["running"] = bool(b and getattr(b, "started", False))
                    item["detail"] = b.status() if b else {}
                else:
                    p = core.plugins.get(meta["plugin_key"])
                    item["running"] = bool(p and p.started)
                    item["detail"] = p.status() if p else {}
            else:
                item["running"] = False
                item["detail"] = {}
            items.append(item)
        return {"items": items, "enabled_map": enabled,
                "core_running": self.bridge.is_running()}
