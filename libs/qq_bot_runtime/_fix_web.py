import os
print("SCRIPT_START")
p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web_plugin.py")
with open(p, encoding="utf-8") as f:
    s = f.read()
print("LEN", len(s))
lines = s.split("\n")
ln = lines[386]
print("REPR", repr(ln[:60]))
bs = chr(92)
needle = bs + '"' + bs + '"' + bs + '"'
print("COUNT", ln.count(needle))
if needle in ln:
    new_ln = ln.replace(needle, '"""')
    lines[386] = new_ln
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print("FIXED_TRUE")
else:
    print("NEEDLE_NOT_FOUND")
