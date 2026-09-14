# -*- coding: utf-8 -*-
import hashlib, json, urllib.request

token = "napcat123456"
h = hashlib.sha256((token + ".napcat").encode()).hexdigest()
print("sha256:", h)

url = "http://127.0.0.1:6099/api/auth/login"
body = json.dumps({"hash": h}).encode()
req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
try:
    resp = urllib.request.urlopen(req, timeout=5)
    print("HTTP", resp.status)
    print("响应:", resp.read().decode("utf-8", "replace"))
except urllib.error.HTTPError as e:
    print("HTTPError", e.code)
    print("响应:", e.read().decode("utf-8", "replace"))
except Exception as e:
    print("异常:", e)
