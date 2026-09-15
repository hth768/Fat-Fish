# -*- coding: utf-8 -*-
"""发布构建：大文件 zip 化 + GitHub Releases 拆卷（>1.9GB 分卷）+ 合并校验。

用法（在主包目录运行）：
    python _split_release.py build            # 生成 dist/ 下各 zip + 分卷
    python _split_release.py merge <卷前缀>   # 合并校验：parts -> 原文件
    python _split_release.py clean            # 清 dist/（除 .gitignore 外）

产物对应 GitHub Release 资产（单资产上限 2GiB，故 >1.9GB 拆卷）：
    feiyu_core_7z.part1/2/3   <- offline_deps_core.zip 5.46GB -> 3 卷
    voice_pack_7z.part1/2/3   <- voice_pack 9.7GB -> 3 卷（如已生成）
    mc_pack.zip / tools_pack.zip / plugins_pack.zip / vl_pack（<2GB 直接发）
合并：copy /b feiyu_core_7z.part* feiyu_core.zip（脚本 merge 自动校验 sha256）。
"""
import hashlib
import os
import shutil
import sys
import zipfile

BASE = os.path.dirname(os.path.abspath(__file__))
DIST = os.path.join(BASE, "dist")
RT = os.path.join(BASE, "libs", "qq_bot_runtime")
REPO = os.path.join(os.path.splitdrive(BASE)[0] + "\\", "plugins")
VOL_BYTES = 1900 * 1024 * 1024        # 单卷 1.9GB（< 2GiB 资产上限）
STAT_REPARSE = 0x400

# (产物名, 源路径, 打包根目录名)  源为目录则 zip 化，为文件直接分卷
# 顺序：小件优先（省空间策略），vl 最大放最后
TARGETS = [
    ("plugins_pack", os.path.join(REPO, "plugins"),     "plugins"),
    ("mc_pack",      os.path.join(REPO, "mc_pack"),     "mc_pack"),
    ("tools_pack",   os.path.join(REPO, "tools_pack"),  "tools_pack"),
    ("feiyu_core",   os.path.join(BASE, "libs", "offline_deps_core.zip"), None),
    ("voice_pack",   os.path.join(REPO, "voice_pack"),  "voice_pack"),
    ("vl_pack",      os.path.join(REPO, "vl_pack"),     "vl_pack"),
    # NapCat（QQ NT 框架）：qq_platform 插件的配套运行时，独立运行无需接线
    ("napcat_pack",  os.path.join(os.path.splitdrive(BASE)[0] + "\\", "NapCat"), "napcat"),
]


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def is_reparse(path: str) -> bool:
    try:
        return bool(os.stat(path, follow_symlinks=False).st_file_attributes & STAT_REPARSE)
    except (OSError, AttributeError):
        return False


def make_zip(src: str, root_name: str, out: str):
    n = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as zf:
        for dirpath, dirnames, filenames in os.walk(src):
            dirnames[:] = [d for d in dirnames if not is_reparse(os.path.join(dirpath, d))]
            for fn in filenames:
                full = os.path.join(dirpath, fn)
                if is_reparse(full):
                    continue
                rel = os.path.relpath(full, os.path.dirname(src)).replace("\\", "/")
                zf.write(full, rel)
                n += 1
    print(f"  zip {root_name}: {n} files -> {os.path.getsize(out) / 1e9:.2f} GB")


def split(path: str, stem: str) -> bool:
    """拆卷。返回 True 表示产生了分卷（此时 zip 中间件可删）。"""
    size = os.path.getsize(path)
    if size <= VOL_BYTES:
        print(f"  {stem}: {size / 1e9:.2f} GB 单文件（无需拆卷）")
        return False
    n = (size + VOL_BYTES - 1) // VOL_BYTES
    with open(path, "rb") as f:
        for i in range(n):
            part = os.path.join(DIST, f"{stem}.part{i + 1}")
            left = min(VOL_BYTES, size - i * VOL_BYTES)
            with open(part, "wb") as out:
                remaining = left
                while remaining:
                    chunk = f.read(min(1 << 24, remaining))
                    if not chunk:
                        break
                    out.write(chunk)
                    remaining -= len(chunk)
            print(f"  {os.path.basename(part)}: {left / 1e9:.2f} GB")
    return True


def build():
    os.makedirs(DIST, exist_ok=True)
    manifest = []
    for name, src, root in TARGETS:
        stem = f"{name}_7z" if (root and name != "plugins_pack") else name
        # 断点续跑：产物已齐则跳过
        vol_exists = [p for p in os.listdir(DIST) if p.startswith(stem + ".part")] \
            if os.path.isdir(DIST) else []
        single = os.path.join(DIST, f"{name}.zip")
        if (vol_exists and not os.path.exists(single)) or \
           (not vol_exists and os.path.exists(single) and os.path.getsize(single) > 0):
            print(f"[skip] {name}: 产物已存在")
            if os.path.exists(single):
                digest = sha256(single)
            elif root is None:
                digest = sha256(src)  # core 等文件型产物：分卷合并目标与源同内容
            else:
                digest = "N/A (merged from parts)"
            manifest.append((name, single, digest))
            continue
        if not os.path.exists(src):
            print(f"[skip] {name}: {src} 不存在")
            continue
        print(f"[build] {name}")
        if root:  # 目录 -> zip
            out = os.path.join(DIST, f"{name}.zip")
            if os.path.exists(out):
                os.remove(out)
            make_zip(src, root, out)
        else:     # 文件（core zip 本体）-> 复制
            out = single
            shutil.copy2(src, out)
        made_vols = split(out, stem)
        digest = sha256(out)        # 删中间件前先算合并目标的哈希
        if made_vols:
            os.remove(out)          # 拆卷后删 zip 中间件（分卷即发布物）
        manifest.append((name, out, digest))  # 清单仍指 zip 名（合并目标）
    # 校验清单
    lines = ["# dist 产物 sha256（合并/下载后校验用；分卷产物为合并目标 zip 的哈希）", ""]
    for name, _out, digest in manifest:
        lines.append(f"{name}: {digest}")
    with open(os.path.join(DIST, "SHA256SUMS.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print("SHA256SUMS.txt written")


def merge(prefix: str):
    """合并分卷并校验：python _split_release.py merge feiyu_core_7z"""
    parts = sorted(p for p in os.listdir(DIST)
                   if p.startswith(prefix + ".part"))
    assert parts, f"dist 下无 {prefix}.part* 卷"
    out = os.path.join(DIST, prefix.replace("_7z", "") + ".zip")
    with open(out, "wb") as w:
        for p in parts:
            with open(os.path.join(DIST, p), "rb") as r:
                shutil.copyfileobj(r, w)
    print(f"merged -> {out} ({os.path.getsize(out) / 1e9:.2f} GB)")
    sums = os.path.join(DIST, "SHA256SUMS.txt")
    if os.path.isfile(sums):
        want = next((l.split(":")[1].strip() for l in open(sums, encoding="utf-8")
                     if l.startswith(prefix.replace("_7z", "") + ":")), None)
        if want:
            got = sha256(out)
            print("sha256 " + ("OK" if got == want else f"MISMATCH want={want} got={got}"))


def clean():
    for p in os.listdir(DIST):
        os.remove(os.path.join(DIST, p))
    print("dist cleaned")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "build":
        build()
    elif cmd == "merge":
        merge(sys.argv[2])
    elif cmd == "clean":
        clean()
    else:
        print(__doc__)
