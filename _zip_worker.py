
import clr  # pythonnet 在 venv 里有
clr.AddReference("System.IO.Compression")
clr.AddReference("System.IO.Compression.FileSystem")
from System.IO.Compression import ZipFile, CompressionLevel
from System.IO import File, FileMode

out = r"F:\FeiyuApp_portable.zip"
if File.Exists(out):
    File.Delete(out)
zf = ZipFile.Open(out, FileMode.CreateNew)
zf.CompressionLevel = CompressionLevel.Optimal
base = r"f:\feiyu_standalone"
n = 0
bad = []
import os
for root, dirs, files in os.walk(base):
    dirs[:] = [d for d in dirs if d not in ("_zip_log.txt",)]
    for fn in files:
        full = os.path.join(root, fn)
        rel = os.path.relpath(full, os.path.dirname(base)).replace("\\", "/")
        try:
            with open(full, "rb") as f:
                f.read(1 << 20)  # 首块预读，CRC 坏文件在这里暴露
            zf.CreateEntryFromFile(full, rel)
            n += 1
            if n % 500 == 0:
                print(n, rel, flush=True)
        except Exception as e:
            bad.append(f"{rel}: {e!r}")
zf.Dispose()
print("DONE files:", n)
for b in bad:
    print("BAD:", b)
