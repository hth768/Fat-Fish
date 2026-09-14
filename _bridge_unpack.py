# -*- coding: utf-8 -*-
"""离线依赖 zip 解包工具：python _bridge_unpack.py <zip> <目标site-packages>"""
import sys
import zipfile

if len(sys.argv) != 3:
    print("usage: _bridge_unpack.py <zip> <dest>")
    sys.exit(1)

zip_path, dest = sys.argv[1], sys.argv[2]
with zipfile.ZipFile(zip_path) as zf:
    total = len(zf.namelist())
    for i, name in enumerate(zf.namelist()):
        zf.extract(name, dest)
        if i % 3000 == 0:
            print(f"  {i}/{total}", flush=True)
print(f"unpacked {total} files -> {dest}")
