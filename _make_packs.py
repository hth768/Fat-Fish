# -*- coding: utf-8 -*-
"""分发包打包工具：主体核心与插件资源分开打包（分开下载）。

用法（命令行参数任选多个）：
    python _make_packs.py core            # 主包（核心，自含可跑，不含插件代码）
    python _make_packs.py plugins         # 插件代码包（16 个插件包装器+分组）
    python _make_packs.py voice           # 本地语音 pack（VoxCPM2 模型+venv_vox）
    python _make_packs.py mc              # Minecraft pack（mc_bot+mc_mod+_mc_ref）
    python _make_packs.py tools           # 语音转码 pack（ffmpeg+silk）
    python _make_packs.py vl              # 视频理解 pack（Qwen2.5-VL，可选大件）
    python _make_packs.py all             # 以上全部
    python _make_packs.py core mc         # 任意组合

产物落在 dist/ 下：
    feiyu_core.zip        <- 必下。解压后双击 FeiyuApp.exe 即可聊天（纯文字+云端TTS）
    plugins_pack.zip      <- 插件代码包（core 不含插件；不解压则插件页为空，功能不受影响）
    voice_pack.zip        <- 可选。vox_tts 插件用（需 NVIDIA 卡；无卡自动降级云端）
    mc_pack.zip           <- 可选。brain_mc_bot / brain_mc_mod 插件用
    tools_pack.zip        <- 可选。语音消息转码（QQ/语音链路）
    vl_pack.zip           <- 可选。本地视频理解回退模型（云端优先不受影响）

新机器安装：解压 feiyu_core.zip -> 把想要的 pack.zip 解压到 libs/ 对应位置
（plugins_pack.zip -> libs/plugins，voice_pack.zip -> libs/voice_pack，
vl_pack.zip -> libs/qq_bot_runtime/）-> 双击 FeiyuApp.exe，
启动器自动把 pack junction 接回引擎原位，无需任何手工配置。
"""
import os
import sys
import time
import zipfile

BASE = os.path.dirname(os.path.abspath(__file__))
DIST = os.path.join(BASE, "dist")
RT = os.path.join(BASE, "libs", "qq_bot_runtime")
STAT_REPARSE = getattr(__import__("stat"), "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)

# 主包排除：可选 pack 实体 + 用户运行数据/临时物 + 打包机自身产物
# （根目录 plugins 此时是 junction -> libs/plugins，walk 剪枝自动跳过，不会进 core）
CORE_EXCLUDE_DIRS = {"_build", "__pycache__", "dist", "plugins",
                     "voice_pack", "mc_pack", "tools_pack"}
RT_EXCLUDE_TOP = {"venv", "venv_vox", "hf_cache", "chat_history", "video_tmp",
                  "voice_tmp", "memory", "webui_old", "launcher_core"}
RT_EXCLUDE_FILES_PREFIX = (".setup_core_complete", ".setup_vox_complete",
                           ".setup_vox_failed")
RT_EXCLUDE_FILES_SUFFIX = (".log",)

# 插件总仓（包外）：<盘>:\plugins\{plugins, mc_pack, tools_pack, voice_pack, vl_pack}
# 各 pack zip 的内部路径即 <pack>/...，统一解压到 <盘>:\plugins\ 即完成安装。
PLUGINS_REPO = os.path.join(os.path.splitdrive(BASE)[0], "plugins")
PACKS = {
    "plugins": os.path.join(PLUGINS_REPO, "plugins"),
    "voice": os.path.join(PLUGINS_REPO, "voice_pack"),
    "mc": os.path.join(PLUGINS_REPO, "mc_pack"),
    "tools": os.path.join(PLUGINS_REPO, "tools_pack"),
    "vl": os.path.join(PLUGINS_REPO, "vl_pack"),
}
PACK_NAMES = {"plugins": "plugins_pack.zip", "voice": "voice_pack.zip",
              "mc": "mc_pack.zip", "tools": "tools_pack.zip",
              "vl": "vl_pack.zip"}


def _is_reparse(path: str) -> bool:
    """junction / 符号链接探测：zip 时跳过，避免把 pack 实体重复打进主包。"""
    try:
        return bool(os.stat(path, follow_symlinks=False).st_file_attributes & STAT_REPARSE)
    except (OSError, AttributeError):
        return False


def _iter_files(root: str, skip_root_name: bool = False):
    """yield (绝对路径, zip 内相对路径)。跳过排除目录与 junction。"""
    root_name = os.path.basename(root.rstrip("\\/"))
    for dirpath, dirnames, filenames in os.walk(root):
        # 剪枝：junction 目录与隐藏系统目录不深入
        dirnames[:] = [d for d in dirnames if not _is_reparse(os.path.join(dirpath, d))]
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            if _is_reparse(full):
                continue
            rel = os.path.relpath(full, os.path.dirname(root)).replace("\\", "/")
            yield full, rel


def _core_files():
    """主包文件清单：BASE 全量（排除 packs/运行数据/临时物）。"""
    for dirpath, dirnames, filenames in os.walk(BASE):
        dirnames[:] = [d for d in dirnames
                       if d not in CORE_EXCLUDE_DIRS
                       and not _is_reparse(os.path.join(dirpath, d))]
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, BASE).replace("\\", "/")
            top = rel.split("/", 2)[1] if rel.startswith("libs/") else rel.split("/")[0]
            if rel.startswith("libs/qq_bot_runtime/"):
                seg = rel[len("libs/qq_bot_runtime/"):].split("/")[0]
                if seg in RT_EXCLUDE_TOP:
                    continue
                name = os.path.basename(rel)
                if name.startswith(RT_EXCLUDE_FILES_PREFIX) or name.endswith(RT_EXCLUDE_FILES_SUFFIX):
                    continue
            if rel.startswith("dist/"):
                continue
            yield full, "feiyu_standalone/" + rel


def make_zip(zip_path: str, files, label: str):
    os.makedirs(DIST, exist_ok=True)
    t0 = time.time()
    n = total = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as zf:
        for full, rel in files:
            try:
                zf.write(full, rel)
                n += 1
                total += os.path.getsize(full)
            except OSError as e:
                print(f"  [skip] {rel}: {e!r}", flush=True)
    size = os.path.getsize(zip_path)
    print(f"[{label}] done: {n} files, {total / 1e9:.2f} GB -> {size / 1e9:.2f} GB, "
          f"{time.time() - t0:.0f}s -> {zip_path}", flush=True)


def main() -> int:
    targets = sys.argv[1:]
    if "all" in targets:
        targets = ["core"] + list(PACKS)
    if not targets:
        print(__doc__)
        return 1
    if "core" in targets:
        make_zip(os.path.join(DIST, "feiyu_core.zip"), _core_files(), "core")
    for t in targets:
        if t in PACKS:
            src = PACKS[t]
            if not os.path.isdir(src):
                print(f"[{t}] skip: {src} 不存在（本机未分离/未下载）", flush=True)
                continue
            make_zip(os.path.join(DIST, PACK_NAMES[t]), _iter_files(src), t)
    return 0


if __name__ == "__main__":
    sys.exit(main())
