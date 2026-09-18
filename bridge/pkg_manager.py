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

from quiet import degrade

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACKAGE_DIR = os.path.join(APP_DIR, "plugins")

# manifest 协议版本：新增/改变字段语义时 +1。
# 缺失按 1 处理（兼容老包），仅告警不阻塞装载；高于当前版本则拒绝（避免按旧规则理解新包）。
MANIFEST_SCHEMA_VERSION = 2
VALID_KINDS = ("platform", "feature", "brain", "sidecar", "local")
_KIND_NEEDS_CREATE = ("platform", "feature", "brain", "local")   # local 也走 create_plugin

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


def validate_manifest(meta: dict, pkg_dir: str = "") -> tuple:
    """静态校验 manifest（不执行插件代码），返回 (errors, warnings) 两个「人话」列表。

    覆盖：必填字段 / kind 合法 / 包名与目录一致 / 依赖字段类型 / sidecar 必需字段 /
    config_schema 结构 / schema_version 兼容性。装载前校验，避免「字段写错被静默忽略」。
    """
    errors, warns = [], []
    if not isinstance(meta, dict):
        return ["manifest.json 顶层必须是 JSON 对象"], []
    name = str(meta.get("name") or "").strip()
    if not name:
        errors.append("缺少必填字段 name")
    elif pkg_dir:
        dirname = os.path.basename(os.path.normpath(pkg_dir))
        if dirname != name:
            errors.append(f"name（{name}）与目录名（{dirname}）不一致")
    if not str(meta.get("title") or "").strip():
        warns.append("建议补充 title（展示名）")
    if not str(meta.get("version") or "").strip():
        warns.append("建议补充 version（semver，如 1.0.0）")

    kind = str(meta.get("kind") or "").strip().lower()
    if not kind:
        errors.append("缺少必填字段 kind")
    elif kind not in VALID_KINDS:
        errors.append(f"kind 非法：{kind}（应为 {'/'.join(VALID_KINDS)} 之一）")

    sv = meta.get("schema_version")
    if sv is None:
        warns.append(f"建议补 schema_version（当前协议版本 {MANIFEST_SCHEMA_VERSION}）；"
                     "缺失按 1 处理")
    else:
        try:
            sv_i = int(sv)
            if sv_i > MANIFEST_SCHEMA_VERSION:
                errors.append(f"schema_version={sv_i} 高于本机支持的 {MANIFEST_SCHEMA_VERSION}，"
                              "请升级 App 或改用兼容写法")
        except Exception:
            errors.append(f"schema_version 必须是整数，实得：{sv!r}")

    for key in ("requires", "optional_requires", "pkg_requires"):
        v = meta.get(key)
        if v is None:
            continue
        if not isinstance(v, list) or any(not isinstance(x, str) for x in v):
            errors.append(f"{key} 必须是字符串数组，实得：{type(v).__name__}")

    if kind == "sidecar":
        sc = meta.get("sidecar")
        if not isinstance(sc, dict):
            errors.append("kind=sidecar 必须提供 sidecar 对象（script/host/port）")
        elif not str(sc.get("script") or "").strip():
            errors.append("sidecar.script 必填（相对 qq_bot 根目录的启动脚本）")
    elif kind in _KIND_NEEDS_CREATE:
        entry = str(meta.get("entry") or "plugin.py")
        if pkg_dir and not os.path.isfile(os.path.join(pkg_dir, entry)):
            errors.append(f"kind={kind} 需要包装器 {entry}（缺失）")
        # 注：create_plugin / create_brain 是否存在留到装载时校验（需执行代码才能确定），
        # 这里不再告警，否则每个正常包都会收到一条无用的提示。

    schema = meta.get("config_schema")
    if schema is not None:
        if not isinstance(schema, list):
            errors.append("config_schema 必须是数组")
        else:
            for i, f in enumerate(schema):
                if not isinstance(f, dict) or not str(f.get("key") or "").strip():
                    errors.append(f"config_schema[{i}] 缺少 key")
    return errors, warns


def load_manifest(pkg_dir: str) -> tuple:
    """读取 + 校验 manifest，返回 (meta, errors, warnings)。

    读取失败时 meta 为 None 并给出原因（不再静默返回 None 让插件「凭空消失」）。
    """
    path = _manifest_path(pkg_dir)
    if not os.path.isfile(path):
        return None, [f"缺少 manifest.json（{path}）"], []
    try:
        with open(path, "r", encoding="utf-8") as f:
            meta = json.load(f)
    except json.JSONDecodeError as e:
        return None, [f"manifest.json 不是合法 JSON：{e}"], []
    except Exception as e:
        return None, [f"manifest.json 读取失败：{e!r}"], []
    errors, warns = validate_manifest(meta, pkg_dir)
    return meta, errors, warns


# ---- manifest 校验提示的去重 -------------------------------------------------
# scan_packages() 会被高频调用（状态轮询、插件页刷新、各处 get_package），逐次打印
# 会把控制台刷满（曾出现同一批告警刷十几屏）。
#   · 错误 / 其它提醒：按「包 + manifest mtime + 内容签名」只打一次，改了才再打；
#   · schema_version 缺失：跨包聚合成一行，包集合变化时才重打。
_warn_cache: dict = {}
_schema_missing_logged: set = set()


def _manifest_stamp(pkg_dir: str) -> float:
    try:
        return os.path.getmtime(_manifest_path(pkg_dir))
    except OSError:
        return 0.0


def _log_manifest_issues(name: str, pkg_dir: str, errors: list, warns: list) -> None:
    """打印 manifest 校验问题（带去重）。"""
    if errors:
        sig = ("E", _manifest_stamp(pkg_dir), tuple(errors))
        if _warn_cache.get("err:" + name) != sig:
            _warn_cache["err:" + name] = sig
            print(f"[PKG][WARN] {name} manifest 无效: " + "；".join(errors))
    soft = [w for w in warns if not w.startswith("建议补 schema_version")]
    if soft:
        sig = ("W", _manifest_stamp(pkg_dir), tuple(soft))
        if _warn_cache.get("soft:" + name) != sig:
            _warn_cache["soft:" + name] = sig
            print(f"[PKG][WARN] {name} manifest 提醒: " + "；".join(soft))


def _flush_schema_notice(names: list) -> None:
    """schema_version 缺失跨包聚合提示（包集合变化时才重打）。"""
    uniq = sorted(set(names))
    if not uniq or set(uniq) == _schema_missing_logged:
        return
    _schema_missing_logged.clear()
    _schema_missing_logged.update(uniq)
    print(f"[PKG] {len(uniq)} 个插件包未声明 schema_version（按 1 处理，建议补 "
          f"{MANIFEST_SCHEMA_VERSION}）：" + "、".join(uniq))


def read_manifest(pkg_dir: str) -> dict:
    """兼容旧调用：只取 meta（校验结果请用 load_manifest）。"""
    meta, errors, warns = load_manifest(pkg_dir)
    if errors or warns:
        _log_manifest_issues(os.path.basename(os.path.normpath(pkg_dir)), pkg_dir, errors, warns)
    return meta


def scan_packages() -> list:
    """扫描插件包目录（只读 manifest，不执行代码）。"""
    out = []
    if not os.path.isdir(PACKAGE_DIR):
        return out
    enabled = _enabled_map()
    schema_missing = []
    for fn in sorted(os.listdir(PACKAGE_DIR)):
        pkg_dir = os.path.join(PACKAGE_DIR, fn)
        if not os.path.isdir(pkg_dir) or fn.startswith(("_", ".")):
            continue
        meta, errors, warns = load_manifest(pkg_dir)
        if meta is None or errors:
            # 不再静默跳过：坏 JSON / 缺字段 / 校验不过的包，以「问题包」形式列出便于排障。
            # （name 可能缺失或与目录名不一致，统一用目录名做键，避免 KeyError）
            out.append({
                "name": fn, "title": fn, "kind": "invalid", "version": "",
                "description": "manifest 有问题，无法装载", "switch": "",
                "plugin_key": fn, "requires": [], "optional_requires": [],
                "pkg_requires": [], "sidecar": None, "builtin": False,
                "dir": pkg_dir.replace("\\", "/"), "enabled": False,
                "manifest_errors": errors or ["manifest 无效"], "manifest_warnings": warns,
            })
            _log_manifest_issues(fn, pkg_dir, errors or ["manifest 无效"], warns)
            continue
        name = meta["name"]
        if warns:
            _log_manifest_issues(name, pkg_dir, [], warns)
            if any(w.startswith("建议补 schema_version") for w in warns):
                schema_missing.append(name)
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
            "pkg_requires": list(meta.get("pkg_requires") or []),   # 依赖的其它插件包
            "sidecar": meta.get("sidecar"),               # sidecar 包: {script,host,port}
            "builtin": bool(meta.get("builtin")),         # True=主体内置，不可卸载
            "dir": pkg_dir.replace("\\", "/"),
            "enabled": bool(enabled.get(name, meta.get("default_on", False))),
            "manifest_errors": errors, "manifest_warnings": warns,
        })
    _flush_schema_notice(schema_missing)
    return out


def get_package(name: str):
    for p in scan_packages():
        if p["name"] == name:
            return p
    return None


def _mod_name(name: str) -> str:
    return f"feiyu_pkg_{name}"


def purge_wrapper_modules(name: str) -> list:
    """清掉包装器在 sys.modules 里的残留（含其子模块），保证可重装 / 热重载干净。

    卸载与重装前都要调用：否则同名重装会沿用旧模块对象（旧状态、旧闭包），
    表现为「改了 plugin.py 但行为没变」。
    """
    prefix = _mod_name(name)
    removed = [k for k in list(sys.modules) if k == prefix or k.startswith(prefix + ".")]
    for k in removed:
        sys.modules.pop(k, None)
    return removed


def _load_wrapper(pkg_dir: str, name: str):
    """执行包装器 plugin.py（惰性：只有装载时才 import qq_bot 重模块）。"""
    path = os.path.join(pkg_dir, "plugin.py")
    if not os.path.isfile(path):
        raise FileNotFoundError(f"包装器缺失: {path}")
    # 重装/热重载：先清掉上一次的模块残留，避免复用旧模块对象
    purge_wrapper_modules(name)
    spec = importlib.util.spec_from_file_location(_mod_name(name), path)
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
        self._loaded = {}     # name -> 已加载的插件 wrapper 模块（用于配置热生效）

    # ---- 依赖 ----
    def missing_deps(self, meta: dict) -> list:
        """硬依赖缺失项：requires（引擎内建能力）+ pkg_requires（其它插件包）。"""
        enabled = _enabled_map()
        missing = []
        for dep in (meta.get("requires") or []):
            dep_pkg = get_package(dep)
            if dep_pkg is None:
                missing.append(f"{dep}(不存在)")
            elif not enabled.get(dep, dep_pkg.get("default_on", False)):
                missing.append(dep)
        # pkg_requires：PLUGINS.md 协议里声明过的字段，之前未实现（文档与实现漂移）
        for dep in (meta.get("pkg_requires") or []):
            dep_pkg = get_package(dep)
            if dep_pkg is None:
                missing.append(f"{dep}(插件包不存在)")
            elif not enabled.get(dep, dep_pkg.get("default_on", False)):
                missing.append(f"{dep}(插件包未启用)")
        return missing

    def soft_deps(self, meta: dict) -> list:
        """软依赖（optional_requires）：缺失只告警，不阻塞装载。"""
        enabled = _enabled_map()
        out = []
        for dep in (meta.get("optional_requires") or []):
            dep_pkg = get_package(dep)
            if dep_pkg is None:
                out.append(f"{dep}(不存在)")
            elif not enabled.get(dep, dep_pkg.get("default_on", False)):
                out.append(f"{dep}(未启用)")
        return out

    # ---- 装载 ----
    def load(self, name: str) -> dict:
        meta = get_package(name)
        if meta is None:
            return {"ok": False, "error": f"插件包不存在: {name}"}
        if meta["builtin"]:
            return {"ok": False, "error": f"{name} 是主体内置能力，不可作为插件装载"}
        merr = meta.get("manifest_errors") or []
        if merr:
            return {"ok": False,
                    "error": "manifest 校验未通过（请先修正）：" + "；".join(merr)}
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
            self._loaded[name] = wrapper
            if meta["kind"] == "brain":
                inst = wrapper.create_brain(core)
                core.brains.register(inst)
            else:
                _set_switch(meta["switch"], True)
                inst = wrapper.create_plugin(core)
                core.plugins.register(inst)
                if self.bridge.is_running():
                    self.bridge.lt.schedule(inst.start())
            # 装载后立即推送已保存的参数（扩展设置）
            try:
                wrapper.on_config(settings_store.get_plugin_config(name))
            except Exception as e:
                degrade("bridge/pkg_manager.py:380 PackageManager.load", e, "降级：wrapper.on_config(settings_store.get_plugin_config")
            _set_enabled(name, True)
            out = {"ok": True, "applied": True, "hint": "已装载"}
            soft = self.soft_deps(meta)
            if soft:
                out["warnings"] = [f"可选依赖缺失（不影响装载）: {', '.join(soft)}"]
                print(f"[PKG][WARN] {name} 可选依赖缺失: {', '.join(soft)}")
            return out
        except Exception as e:
            # 装载失败要清掉半装载的模块残留，避免下次重装复用坏状态
            purge_wrapper_modules(name)
            self._loaded.pop(name, None)
            return {"ok": False, "error": f"装载失败: {e!r}"}

    def _load_sidecar(self, meta: dict) -> dict:
        sc_cfg = meta.get("sidecar") or {}
        sc = SidecarProcess(name=meta["name"], title=meta["title"],
                            script=sc_cfg.get("script", ""),
                            host=sc_cfg.get("host", "127.0.0.1"),
                            port=sc_cfg.get("port", 0))
        r = sc.start()
        if r.get("ok"):
            # 端口就绪等待：避免「进程起来了但服务还没在听」，调用方立刻请求会失败
            r["ready"] = sc.wait_ready()
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
            self._loaded.pop(name, None)
            purge_wrapper_modules(name)
            return {"ok": True, "applied": False, "error": f"卸载时异常(已记为停用): {e!r}"}
        _set_enabled(name, False)
        self._loaded.pop(name, None)
        # 清掉 sys.modules 里的包装器模块（含子模块）：否则同名重装会沿用旧模块对象，
        # 表现为「改了 plugin.py 但行为没变」。
        removed = purge_wrapper_modules(name)
        out = {"ok": True, "applied": True}
        if removed:
            out["purged_modules"] = removed
        return out

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
                    except Exception as e:
                        degrade("bridge/pkg_manager.py:469 PackageManager.attach_to", e, "降级：core.brains._plugins.pop(meta['plugin_key'], None)")
                continue
            try:
                wrapper = _load_wrapper(meta["dir"], name)
                self._loaded[name] = wrapper
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

    # ---- 参数配置（扩展设置） ----
    def get_config(self, name: str) -> dict:
        """返回某插件生效中的参数：manifest config_schema 默认值 + 已保存覆盖。"""
        meta = get_package(name)
        if meta is None:
            return {}
        manifest = read_manifest(meta["dir"]) or {}
        schema = manifest.get("config_schema") or []
        cfg = {}
        for f in schema:
            if isinstance(f, dict) and "key" in f:
                cfg[f["key"]] = f.get("default")
        cfg.update(settings_store.get_plugin_config(name))
        return cfg

    def set_config(self, name: str, cfg: dict) -> bool:
        """推送参数到插件 wrapper 模块（若其实现了 on_config）。返回是否成功应用。"""
        mod = self._loaded.get(name)
        if mod is None:
            return False
        fn = getattr(mod, "on_config", None)
        if callable(fn):
            try:
                fn(cfg)
                return True
            except Exception:
                return False
        return False

    # ---- 状态 ----
    def sidecar_status(self, name: str):
        sc = self._sidecars.get(name)
        return sc.status() if sc else None

    def stop_all_sidecars(self):
        for sc in list(self._sidecars.values()):
            try:
                sc.stop()
            except Exception as e:
                degrade("pkg_manager.stop_all_sidecars", e,
                        "停止 sidecar 失败（%s）" % getattr(sc, "name", "?"))
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
