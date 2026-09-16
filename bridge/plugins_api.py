# -*- coding: utf-8 -*-
"""活动插件 API（插件包版）：全部委托 PackageManager，每个插件是 plugins/ 下的独立包。

另含插件市场（market_*）：主包 plugins/ 为空插件库，<盘>:/plugins 为仓库，
UI 内按需安装（代码包拷贝 / 资源大件 junction）。
"""
import os
import re
import subprocess
import time
import json
import settings_store

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

try:
    from .pkg_manager import PackageManager, read_manifest, scan_packages
except ImportError:
    from pkg_manager import PackageManager, read_manifest, scan_packages

_pkg: PackageManager = None


def init(bridge):
    """app.py 启动时构造单例。"""
    global _pkg
    _pkg = PackageManager(bridge)
    return _pkg


def manager() -> PackageManager:
    return _pkg


# ----------------------------------------------------------------------
# 分组（依赖归组）：plugins/groups.json —— 同组共享同一批外部依赖/资源
# ----------------------------------------------------------------------
_GROUPS_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "plugins", "groups.json")


def load_groups() -> dict:
    try:
        import json
        with open(_GROUPS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("groups", {}) if isinstance(data, dict) else {}
    except Exception:
        return {}


def group_action(bridge, group: str, action: str) -> dict:
    """组级批量启停：action = on | off。逐包 toggle，结果逐个汇报。"""
    groups = load_groups()
    meta = groups.get(group)
    if not meta:
        return {"ok": False, "error": f"未知分组: {group}"}
    results = {}
    for name in meta.get("packages", []):
        r = toggle(bridge, name, action == "on")
        results[name] = {"ok": r.get("ok"), "hint": r.get("hint", r.get("error", ""))}
    return {"ok": True, "group": group, "action": action, "results": results}


# ----------------------------------------------------------------------
# HTTP API
# ----------------------------------------------------------------------
def list_plugins(bridge) -> dict:
    d = _pkg.list_all()
    d["groups"] = load_groups()
    return d


def rescan(bridge) -> dict:
    """重新扫描插件目录（放入/移除包文件夹后调用）。"""
    return {"ok": True, "packages": scan_packages()}


def toggle(bridge, name: str, on: bool) -> dict:
    """装载/卸载一个插件包（运行时尽量生效）。"""
    if on:
        return _pkg.load(name)
    return _pkg.unload(name)


def brain_action(bridge, name: str, action: str) -> dict:
    """大脑包的 start / stop / status（name=包名或大脑键）。"""
    if bridge.core is None:
        return {"ok": False, "error": "核心未运行"}
    meta = get_pkg_meta(name)
    key = meta["plugin_key"] if meta else name
    try:
        b = bridge.core.brains.get(key)
    except Exception as e:
        return {"ok": False, "error": repr(e)}
    if b is None:
        return {"ok": False, "error": f"大脑未注册（先启用对应插件包）: {key}"}
    try:
        if action == "start":
            bridge.lt.run_coro(b.start(), timeout=120)
        elif action == "stop":
            bridge.lt.run_coro(b.stop(), timeout=60)
        elif action == "status":
            pass
        else:
            return {"ok": False, "error": f"未知动作: {action}"}
        return {"ok": True, "status": b.status()}
    except Exception as e:
        return {"ok": False, "error": repr(e)}


def sidecar_action(bridge, name: str, action: str) -> dict:
    """sidecar 包的 start / stop / restart / status（经装载/卸载语义，覆盖层保持一致）。"""
    meta = get_pkg_meta(name)
    if meta is None or meta["kind"] != "sidecar":
        return {"ok": False, "error": f"不是 sidecar 包: {name}"}
    if action == "status":
        st = _pkg.sidecar_status(name)
        return {"ok": True, "status": st or {"running": False, "hint": "从未启动（用 start 拉起）"}}
    if action == "stop":
        return _pkg.unload(name)
    if action == "restart":
        _pkg.unload(name)
        return _pkg.load(name)
    if action == "start":
        return _pkg.load(name)
    return {"ok": False, "error": f"未知动作: {action}"}


def get_pkg_meta(name: str):
    for p in scan_packages():
        if p["name"] == name:
            return p
    return None


# ----------------------------------------------------------------------
# 插件参数配置（扩展设置）：每个插件可在 manifest.json 声明 config_schema，
# UI 据此动态渲染表单；保存即落到覆盖层 plugin_config 子键，并可热推给插件。
# ----------------------------------------------------------------------
def _to_bool(v):
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    return str(v).strip().lower() in ("1", "true", "yes", "on", "y")


def plugin_config_view(name: str) -> dict:
    """返回某插件参数配置视图：元信息 + config_schema + 已保存值。"""
    meta = get_pkg_meta(name)
    if meta is None:
        return {"error": "plugin_not_found", "name": name}
    manifest = read_manifest(meta["dir"]) or {}
    schema = manifest.get("config_schema") or []
    saved = settings_store.get_plugin_config(name)
    fields = []
    for f in schema:
        if not isinstance(f, dict) or "key" not in f:
            continue
        key = f["key"]
        value = saved.get(key, f.get("default"))
        item = dict(f)
        item["value"] = value
        if item.get("type") == "secret" and value not in (None, ""):
            item["value"] = settings_store.mask_secret(value)  # 展示掩码，留掩码即不修改
            item["masked"] = True
        fields.append(item)
    return {
        "name": name,
        "display_name": manifest.get("title") or manifest.get("name") or name,
        "description": manifest.get("description", ""),
        "version": manifest.get("version", ""),
        "kind": meta.get("kind", ""),
        "has_config": bool(fields),
        "fields": fields,
        "enabled": bool(meta.get("enabled")),
    }


def plugin_config_save(name: str, values: dict) -> dict:
    """保存某插件参数：仅接受 schema 声明键并按类型转换；secret 留掩码则保留原值。"""
    meta = get_pkg_meta(name)
    if meta is None:
        return {"error": "plugin_not_found", "name": name}
    manifest = read_manifest(meta["dir"]) or {}
    schema = manifest.get("config_schema") or []
    saved = settings_store.get_plugin_config(name)
    cleaned = {}
    for f in schema:
        if not isinstance(f, dict) or "key" not in f:
            continue
        key = f["key"]
        if key not in values:
            continue
        raw = values[key]
        ftype = f.get("type", "str")
        try:
            if ftype == "bool":
                cleaned[key] = _to_bool(raw)
            elif ftype == "int":
                cleaned[key] = int(raw)
            elif ftype == "float":
                cleaned[key] = float(raw)
            elif ftype == "json":
                cleaned[key] = raw if isinstance(raw, (dict, list)) else json.loads(raw or "{}")
            elif ftype == "secret":
                if raw in (None, "") or settings_store.is_masked(raw):
                    cleaned[key] = saved.get(key, f.get("default"))  # 保留原值
                else:
                    cleaned[key] = str(raw)
            else:  # str / text / choice
                cleaned[key] = "" if raw is None else (raw if isinstance(raw, str) else str(raw))
        except Exception:
            cleaned[key] = saved.get(key, f.get("default"))
    final = settings_store.set_plugin_config(name, cleaned)
    applied = False
    try:
        if _pkg is not None:
            applied = _pkg.set_config(name, final)
    except Exception:
        pass
    return {"ok": True, "name": name, "values": final, "applied": applied}



# ----------------------------------------------------------------------
# 插件市场：主包 plugins/ 为空插件库（已安装集），<盘>:\plugins 为仓库（安装源）
# 代码包安装=拷贝目录；资源大件安装=建 junction（不复制 GB 级文件）
# ----------------------------------------------------------------------
import shutil

# 资源包 -> 引擎内接线点（rt/<name>）
PACK_TARGETS = {
    "voice_pack": ["models", "venv_vox"],
    "mc_pack": ["mc_bot", "mc_mod", "_mc_ref"],
    "tools_pack": ["tools"],
    "vl_pack": ["hf_cache"],
}
PACK_DESC = {
    "voice_pack": "本地 VoxCPM2 语音（模型+推理环境，需 NVIDIA 卡；无卡自动走 GLM 云端语音）",
    "mc_pack": "Minecraft 两大脑资源（mineflayer 原版世界 + 模组世界）",
    "tools_pack": "语音消息转码（ffmpeg + silk，QQ/语音链路需要）",
    "vl_pack": "本地视频理解模型（Qwen2.5-VL，云端优先时仅作回退）",
}


def _market_repo() -> str:
    """插件仓库位置：<主包所在盘>:\\plugins（splitdrive 返回 'E:'，join 前必须补 '\\'）。"""
    drive = os.path.splitdrive(APP_DIR)[0] + "\\"
    return os.path.join(drive, "plugins")


def _rt_dir() -> str:
    return os.environ.get("FEIYU_QQ_BOT", "").strip() or os.getcwd()


def _dir_size(path: str) -> int:
    total = 0
    try:
        for root, _dirs, files in os.walk(path):
            for fn in files:
                try:
                    total += os.path.getsize(os.path.join(root, fn))
                except OSError:
                    pass
    except OSError:
        pass
    return total


def market_list(bridge) -> dict:
    """仓库清单 + 主包安装状态（代码包按 manifest 读元数据，资源包按 targets 判定）。"""
    repo = _market_repo()
    lib_dir = os.path.join(APP_DIR, "plugins")
    rt = _rt_dir()
    items = []

    # 1) 插件代码包源：repo/plugins/<pkg>
    src_plugins = os.path.join(repo, "plugins")
    installed_pkgs = {d for d in os.listdir(lib_dir)
                      if os.path.isdir(os.path.join(lib_dir, d))} if os.path.isdir(lib_dir) else set()
    if os.path.isdir(src_plugins):
        for d in sorted(os.listdir(src_plugins)):
            pkg_dir = os.path.join(src_plugins, d)
            if not os.path.isdir(pkg_dir) or d.startswith(("_", ".")):
                continue
            if d == "groups.json" or not os.path.isfile(os.path.join(pkg_dir, "manifest.json")):
                continue
            meta = read_manifest(pkg_dir) or {}
            items.append({
                "kind": "plugin", "name": d,
                "title": meta.get("title", d),
                "description": meta.get("description", ""),
                "version": meta.get("version", ""),
                "size_mb": round(_dir_size(pkg_dir) / 1e6, 1),
                "installed": d in installed_pkgs,
            })

    # 2) 资源包源：repo/<pack>
    for pack, targets in PACK_TARGETS.items():
        pack_dir = os.path.join(repo, pack)
        if not os.path.isdir(pack_dir):
            continue
        installed = all(os.path.isdir(os.path.join(rt, t)) for t in targets)
        items.append({
            "kind": "pack", "name": pack,
            "title": pack,
            "description": PACK_DESC.get(pack, ""),
            "size_gb": round(_dir_size(pack_dir) / 1e9, 2),
            "installed": installed,
        })

    return {"ok": True, "repo": repo, "repo_exists": os.path.isdir(repo),
            "lib_dir": lib_dir, "items": items}


def market_action(bridge, action: str, name: str) -> dict:
    """安装/卸载。代码包拷目录（含 groups.json 一次性带入），资源包建/删 junction。"""
    repo = _market_repo()
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    lib_dir = os.path.join(base, "plugins")
    rt = _rt_dir()

    if action == "install":
        pkg_src = os.path.join(repo, "plugins", name)
        if os.path.isdir(pkg_src) and os.path.isfile(os.path.join(pkg_src, "manifest.json")):
            dst = os.path.join(lib_dir, name)
            if os.path.isdir(dst):
                return {"ok": False, "error": f"已安装: {name}"}
            try:
                shutil.copytree(pkg_src, dst)
                groups_src = os.path.join(repo, "plugins", "groups.json")
                groups_dst = os.path.join(lib_dir, "groups.json")
                if os.path.isfile(groups_src) and not os.path.isfile(groups_dst):
                    shutil.copy2(groups_src, groups_dst)
            except Exception as e:
                return {"ok": False, "error": f"安装失败: {e!r}"}
            try:
                rescan(bridge)
            except Exception:
                pass
            return {"ok": True, "hint": f"插件已安装到插件库: {name}（到列表中启用）"}

        targets = PACK_TARGETS.get(name)
        if targets and os.path.isdir(os.path.join(repo, name)):
            linked, failed = [], []
            for t in targets:
                src = os.path.join(repo, name, t)
                dst = os.path.join(rt, t)
                if os.path.isdir(dst):
                    linked.append(t)
                    continue
                if not os.path.isdir(src):
                    failed.append(f"{t}(仓库缺失)")
                    continue
                ok = _mklink_j(dst, src)
                (linked if ok else failed).append(t if ok else f"{t}(建链失败)")
            if failed:
                return {"ok": False, "error": f"部分接线失败: {', '.join(failed)}"}
            return {"ok": True, "hint": f"资源包已接线: {', '.join(linked)}"}
        return {"ok": False, "error": f"仓库中不存在: {name}"}

    if action == "uninstall":
        pkg_dst = os.path.join(lib_dir, name)
        if os.path.isdir(pkg_dst) and os.path.isfile(os.path.join(pkg_dst, "manifest.json")):
            try:
                if bridge.core is not None:
                    _pkg = manager()
                    if _pkg is not None:
                        _pkg.unload(name)
            except Exception:
                pass
            try:
                shutil.rmtree(pkg_dst)
            except Exception as e:
                return {"ok": False, "error": f"卸载失败: {e!r}"}
            try:
                rescan(bridge)
            except Exception:
                pass
            return {"ok": True, "hint": f"插件已从插件库移除: {name}"}

        targets = PACK_TARGETS.get(name)
        if targets:
            removed = []
            for t in targets:
                dst = os.path.join(rt, t)
                try:
                    if os.path.isdir(dst) and (os.stat(dst, follow_symlinks=False)
                                               .st_file_attributes & 0x400):
                        os.rmdir(dst)  # 仅删 junction，绝不动实体
                        removed.append(t)
                    elif os.path.isdir(dst):
                        return {"ok": False, "error": f"{t} 不是接线（真实目录），拒绝删除"}
                except OSError as e:
                    return {"ok": False, "error": f"{t} 移除失败: {e!r}"}
            return {"ok": True, "hint": f"资源包已断开: {', '.join(removed) or '（原本未安装）'}"}
        return {"ok": False, "error": f"未知项: {name}"}

    return {"ok": False, "error": f"未知动作: {action}"}


def _mklink_j(dst: str, src: str) -> bool:
    try:
        subprocess.run(["cmd", "/c", "mklink", "/J", dst, src],
                       capture_output=True, text=True, timeout=30,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return os.path.isdir(dst)
    except Exception:
        return False


# ----------------------------------------------------------------------
# 寻找插件（全盘扫描 + 自动安装）：
# 限深遍历所有固定磁盘，特征识别插件代码包（manifest.json）与资源大件
# （voice/mc/tools/vl 签名），命中即自动安装（拷贝 / junction）。
# ----------------------------------------------------------------------
SEEK_MAX_DEPTH = 3          # 相对盘根的最大深入层数
SEEK_TIME_LIMIT = 120.0     # 扫描硬超时（秒），超时返回部分结果
SEEK_SKIP_DIRS = {
    "windows", "program files", "program files (x86)", "programdata",
    "$recycle.bin", "system volume information", "appdata",
    "node_modules", "site-packages", "__pycache__", ".git",
    "venv", "venv_vox", "_build", "dist", "hf_cache", "emojis", "voice_tmp",
    "qq_bot_runtime",   # 任何 feiyu 引擎运行时副本（内部巨大且不含待装插件）
}
# feiyu 插件 manifest 特征（防把浏览器组件等带 manifest.json 的目录误装）
SEEK_KINDS = {"platform", "feature", "brain", "sidecar", "local"}
SEEK_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_\-]*$")
SEEK_PACK_PARTS = {
    "voice_pack": ["models", "venv_vox"],
    "mc_pack": ["mc_bot", "mc_mod", "_mc_ref"],
    "tools_pack": ["tools"],
    "vl_pack": ["hf_cache"],
}


def _is_reparse(path: str) -> bool:
    """junction / 符号链接探测（不深入、不误删实体）。"""
    try:
        return bool(os.stat(path, follow_symlinks=False).st_file_attributes & 0x400)
    except (OSError, AttributeError):
        return False


def _fixed_drives() -> list:
    """所有本地固定磁盘盘根（跳过 U 盘/光驱/网络盘）。"""
    try:
        import ctypes
        mask = ctypes.windll.kernel32.GetLogicalDrives()
        drives = []
        for i in range(26):
            if mask >> i & 1:
                root = chr(65 + i) + ":\\"
                if ctypes.windll.kernel32.GetDriveTypeW(root) == 3:  # DRIVE_FIXED
                    drives.append(root)
        return drives
    except Exception:
        return []


def _pack_parts_found(dirpath: str, pack: str) -> list:
    """目录 dirpath 下存在哪些可接线的资源部件（带特征校验，防误接同名泛目录）。"""
    found = []
    if pack == "voice_pack":
        if (os.path.isdir(os.path.join(dirpath, "models", "VoxCPM2"))
                and os.path.isdir(os.path.join(dirpath, "venv_vox", "Scripts"))):
            found = ["models", "venv_vox"]
    elif pack == "tools_pack":
        if os.path.isdir(os.path.join(dirpath, "tools", "ffmpeg")):
            found = ["tools"]
    elif pack == "vl_pack":
        if os.path.isdir(os.path.join(dirpath, "hf_cache", "hub")):
            found = ["hf_cache"]
    elif pack == "mc_pack":
        if os.path.isfile(os.path.join(dirpath, "mc_bot", "package.json")):
            found.append("mc_bot")
        mc_mod = os.path.join(dirpath, "mc_mod")
        if os.path.isdir(mc_mod) and any(
                fn.lower().endswith(".jar") for fn in os.listdir(mc_mod)):
            found += ["mc_mod", "_mc_ref"] if os.path.isdir(
                os.path.join(dirpath, "_mc_ref")) else ["mc_mod"]
    return found


def market_seek(bridge) -> dict:
    """全盘寻找插件：扫固定盘（限深+黑名单剪枝+硬超时），发现即自动安装。"""
    lib_dir = os.path.join(APP_DIR, "plugins")
    rt = _rt_dir()
    skip_roots = [os.path.normcase(APP_DIR), os.path.normcase(rt)]
    deadline = time.monotonic() + SEEK_TIME_LIMIT

    found_plugins, found_packs = [], []
    installed, skipped, failed = [], [], []
    installed_names = {d for d in os.listdir(lib_dir)
                       if os.path.isdir(os.path.join(lib_dir, d))} if os.path.isdir(lib_dir) else set()
    timeout_hit = False

    def install_plugin(dirpath: str) -> None:
        meta = read_manifest(dirpath) or {}
        name = str(meta.get("name") or "")
        # 收紧判定：必须是 feiyu 插件特征（合法 kind + 蛇形 name），
        # 浏览器组件等恰好带 manifest.json 的目录在这里被拒。
        if not name or not SEEK_NAME_RE.match(name) or meta.get("kind") not in SEEK_KINDS:
            return
        if name in found_plugins:
            return
        found_plugins.append(name)
        dst = os.path.join(lib_dir, name)
        if name in installed_names or os.path.isdir(dst):
            skipped.append(f"{name}(已安装)")
            return
        try:
            shutil.copytree(dirpath, dst)
            installed.append(f"插件:{name}")
            installed_names.add(name)
            groups_src = os.path.join(os.path.dirname(dirpath), "groups.json")
            groups_dst = os.path.join(lib_dir, "groups.json")
            if os.path.isfile(groups_src) and not os.path.isfile(groups_dst):
                shutil.copy2(groups_src, groups_dst)
        except Exception as e:
            failed.append(f"{name}: {e!r}")

    def install_pack(dirpath: str) -> None:
        for pack in SEEK_PACK_PARTS:
            hit = _pack_parts_found(dirpath, pack)
            if not hit:
                continue
            if all(f"{pack}/{t}" in {s.split("(")[0] for s in skipped} or
                   os.path.isdir(os.path.join(rt, t)) for t in hit):
                return  # 该包全部部件已接线，不再重复记录
            found_packs.append(f"{pack}@{dirpath}")
            done, fail = [], []
            for t in hit:
                dst = os.path.join(rt, t)
                if os.path.isdir(dst):
                    skipped.append(f"{pack}/{t}(已接线)")
                    continue
                if _mklink_j(dst, os.path.join(dirpath, t)):
                    done.append(t)
                else:
                    fail.append(t)
            if done:
                installed.append(f"资源:{pack}({','.join(done)})")
            if fail:
                failed.append(f"{pack}: 接线失败 {','.join(fail)}")
            return

    try:
        for root in _fixed_drives():
            for dirpath, dirnames, filenames in os.walk(root, topdown=True):
                if time.monotonic() > deadline:
                    timeout_hit = True
                    break
                depth = dirpath[len(root):].count(os.sep)
                if depth >= SEEK_MAX_DEPTH:
                    dirnames[:] = []
                # 剪枝：黑名单 / 隐藏系统目录 / junction（不深入大件与联接）
                dirnames[:] = [d for d in dirnames
                               if d.lower() not in SEEK_SKIP_DIRS
                               and not d.startswith(("$", "."))
                               and not _is_reparse(os.path.join(dirpath, d))]
                # 跳过主包自身与引擎运行时（巨大且不可能含待装插件）
                if any(os.path.normcase(dirpath).startswith(sr) for sr in skip_roots):
                    dirnames[:] = []
                    continue
                # 命中检测：代码包（manifest.json）优先，其次资源包签名
                if "manifest.json" in filenames:
                    if read_manifest(dirpath):
                        install_plugin(dirpath)
                    dirnames[:] = []
                else:
                    install_pack(dirpath)  # 未命中签名时为无害空操作
                if timeout_hit:
                    break
    except Exception as e:
        failed.append(f"扫描异常: {e!r}")

    # 装了新东西就重扫插件库（让列表立即可见）
    if installed:
        try:
            rescan(bridge)
        except Exception:
            pass
    return {"ok": True, "timeout": timeout_hit,
            "drives": _fixed_drives(),
            "found": {"plugins": found_plugins, "packs": found_packs},
            "installed": installed, "skipped": skipped, "failed": failed}
