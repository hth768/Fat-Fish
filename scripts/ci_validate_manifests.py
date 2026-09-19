# -*- coding: utf-8 -*-
"""CI 用的插件包 manifest 规范校验（stdlib only，避免引入重型依赖）。

与 bridge/pkg_manager.validate_manifest 的规则对齐：扫描 plugins/ 下每个含
manifest.json 的目录，检查必填字段、kind 合法性、包名与目录一致、schema_version
兼容。命中 error 则退出码非 0（CI 变红）；仅 warn 不影响。

用法：python scripts/ci_validate_manifests.py [目录...]  默认扫 plugins/
"""
import json
import os
import sys

MANIFEST_SCHEMA_VERSION = 2
VALID_KINDS = ("platform", "feature", "brain", "sidecar", "local")


def _validate_one(pkg_dir: str):
    errors = []
    warns = []
    name = os.path.basename(os.path.normpath(pkg_dir))
    mp = os.path.join(pkg_dir, "manifest.json")
    if not os.path.isfile(mp):
        # 只校验含 manifest.json 的目录（跳过纯资源/占位目录）
        return errors, warns
    try:
        with open(mp, "r", encoding="utf-8") as f:
            meta = json.load(f)
    except Exception as e:
        return [f"{name}: manifest.json 解析失败: {e}"], warns

    mn = str(meta.get("name") or "").strip()
    if not mn:
        errors.append(f"{name}: 缺少必填字段 name")
    elif mn != name:
        errors.append(f"{name}: name（{mn}）与目录名（{name}）不一致")

    if not str(meta.get("title") or "").strip():
        warns.append(f"{name}: 建议补充 title（展示名）")
    if not str(meta.get("version") or "").strip():
        warns.append(f"{name}: 建议补充 version（semver，如 1.0.0）")

    kind = str(meta.get("kind") or "").strip().lower()
    if not kind:
        errors.append(f"{name}: 缺少必填字段 kind")
    elif kind not in VALID_KINDS:
        errors.append(f"{name}: kind 非法：{kind}（应为 {'/'.join(VALID_KINDS)} 之一）")

    sv = meta.get("schema_version")
    if sv is None:
        warns.append(f"{name}: 建议补 schema_version（当前协议版本 {MANIFEST_SCHEMA_VERSION}）")
    elif isinstance(sv, bool) or not isinstance(sv, int):
        errors.append(f"{name}: schema_version 必须是整数，实得：{sv!r}")
    elif sv > MANIFEST_SCHEMA_VERSION:
        errors.append(
            f"{name}: schema_version={sv} 高于本机支持的 {MANIFEST_SCHEMA_VERSION}，拒绝装载"
        )

    if kind == "sidecar":
        sc = meta.get("sidecar")
        if not isinstance(sc, dict) or not all(k in sc for k in ("script", "host", "port")):
            errors.append(f"{name}: kind=sidecar 必须提供 sidecar 对象（script/host/port）")

    return errors, warns


def main():
    roots = sys.argv[1:] or [os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "plugins")]
    all_errors = []
    for root in roots:
        if not os.path.isdir(root):
            continue
        for entry in sorted(os.listdir(root)):
            d = os.path.join(root, entry)
            if not os.path.isdir(d):
                continue
            errors, warns = _validate_one(d)
            for w in warns:
                print(f"[manifest][warn] {w}")
            all_errors.extend(errors)
            for e in errors:
                print(f"[manifest][error] {e}")

    if all_errors:
        print(f"\n插件包规范校验失败：{len(all_errors)} 个 error")
        sys.exit(1)
    print("插件包规范校验通过：所有含 manifest.json 的包均符合规范")
    sys.exit(0)


if __name__ == "__main__":
    main()
