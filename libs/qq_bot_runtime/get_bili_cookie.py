# -*- coding: utf-8 -*-
"""获取 B 站登录 Cookie（SESSDATA / bili_jct），写入 config.py。

三种用法：
  1) 浏览器登录后自动读取（推荐：你已在浏览器登录，无需扫码）：
       venv/Scripts/python.exe get_bili_cookie.py browser
     从 Edge/Chrome 本地 Cookie 库里读取 bilibili.com 的 SESSDATA / bili_jct。
     注意：若浏览器正在运行，Chromium 会锁库，脚本会先提示关闭浏览器。

  2) 手动粘贴整段 Cookie：
       venv/Scripts/python.exe get_bili_cookie.py paste "SESSDATA=xxx; bili_jct=yyy; ..."

  3) 扫码登录（命令行二维码，需终端能显示二维码）：
       venv/Scripts/python.exe get_bili_cookie.py qr

写入字段（config.py）：BILIBILI_SESSDATA / BILIBILI_CSRF（=bili_jct）。
Cookie 只存在本地 config.py。
"""
import base64
import hashlib
import json
import os
import re
import sqlite3
import sys
import tempfile
import time
import shutil
import webbrowser

CONFIG = "config.py"
GEN_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
POLL_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

BROWSER_COOKIE_PATHS = [
    ("Edge", r"%LOCALAPPDATA%\Microsoft\Edge\User Data"),
    ("Chrome", r"%LOCALAPPDATA%\Google\Chrome\User Data"),
]


def write_config(sessdata: str, csrf: str) -> None:
    with open(CONFIG, encoding="utf-8") as f:
        txt = f.read()
    new = re.sub(r'BILIBILI_SESSDATA\s*=\s*"[^"]*"',
                 'BILIBILI_SESSDATA = "%s"' % sessdata, txt, count=1)
    new = re.sub(r'BILIBILI_CSRF\s*=\s*"[^"]*"',
                 'BILIBILI_CSRF = "%s"' % csrf, new, count=1)
    if new == txt:
        print("[X] config.py 里没找到 BILIBILI_SESSDATA / BILIBILI_CSRF 字段，请检查")
        return
    with open(CONFIG, "w", encoding="utf-8") as f:
        f.write(new)
    print("[OK] 已写入 config.py：BILIBILI_SESSDATA / BILIBILI_CSRF")


def parse_paste(cookie_str: str) -> bool:
    pairs = {}
    for part in cookie_str.replace("\n", "").replace("\r", "").split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            pairs[k.strip()] = v.strip()
    sd = pairs.get("SESSDATA", "")
    jct = pairs.get("bili_jct", "")
    if not sd or not jct:
        print("[X] 没找到 SESSDATA 或 bili_jct，请确认复制的是完整 Cookie 请求头")
        return False
    write_config(sd, jct)
    return True


# ---------------- 浏览器 Cookie 读取 ----------------

def _dpapi_decrypt(blob: bytes) -> bytes:
    """用 Windows DPAPI 解密（当前用户上下文）。"""
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_char))]

    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    buf_in = DATA_BLOB(len(blob), ctypes.cast(ctypes.create_string_buffer(blob), ctypes.POINTER(ctypes.c_char)))
    buf_out = DATA_BLOB()
    if not crypt32.CryptUnprotectData(ctypes.byref(buf_in), None, None, None, None, 0, ctypes.byref(buf_out)):
        raise OSError("CryptUnprotectData failed")
    data = ctypes.string_at(buf_out.pbData, buf_out.cbData)
    kernel32.LocalFree(buf_out.pbData)
    return data


def _get_master_key(user_data_dir: str):
    """从 Local State 取 master key，返回 (key_bytes, encrypted_flag)。"""
    ls = os.path.join(user_data_dir, "Local State")
    if not os.path.exists(ls):
        return None, None
    with open(ls, encoding="utf-8", errors="ignore") as f:
        obj = json.load(f)
    enc = obj.get("os_crypt", {}).get("encrypted_key", "")
    if not enc:
        return None, None
    raw = base64.b64decode(enc)
    if raw[:5] == b"DPAPI":
        raw = raw[5:]
    return _dpapi_decrypt(raw), obj.get("os_crypt", {}).get("app_bound_encrypted_key", "")


def _decrypt_value(value: bytes, key: bytes) -> str:
    if not value:
        return ""
    # v10 / v11: AES-GCM（32 字节 nonce 前缀在某些版本变化，这里按标准 12+16）
    if value[:3] in (b"v10", b"v11"):
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        except Exception:
            return ""
        payload = value[3:]
        # 当前 Chromium 标准：12 字节 nonce + 密文 + 16 字节 tag
        if len(payload) > 12 + 16:
            nonce, ct = payload[:12], payload[12:]
            try:
                return AESGCM(key).decrypt(nonce, ct, None).decode("utf-8", "ignore")
            except Exception:
                pass
        # 某些版本 32 字节 nonce 前缀
        if len(payload) > 32 + 16:
            nonce, ct = payload[32:44], payload[44:]
            try:
                return AESGCM(key).decrypt(nonce, ct, None).decode("utf-8", "ignore")
            except Exception:
                pass
        return ""
    # 老版本：直接 DPAPI
    try:
        return _dpapi_decrypt(value).decode("utf-8", "ignore")
    except Exception:
        return ""


def _iter_cookie_dbs(user_data_dir: str):
    for sub in ("Default", "Profile 1", "Profile 2", "Profile 3", "Profile 4", "Profile 5"):
        p = os.path.join(user_data_dir, sub, "Network", "Cookies")
        if os.path.exists(p):
            yield p


def read_from_browser() -> None:
    found = False
    for name, raw_dir in BROWSER_COOKIE_PATHS:
        user_data_dir = os.path.expandvars(raw_dir)
        if not os.path.isdir(user_data_dir):
            continue
        key, _ = _get_master_key(user_data_dir)
        if not key:
            print("[!] %s：读不到 master key，跳过" % name)
            continue
        for db in _iter_cookie_dbs(user_data_dir):
            # 浏览器运行时会锁库，先复制到临时文件再读
            tmp = os.path.join(tempfile.gettempdir(), "ck_%s.db" % name)
            try:
                shutil.copyfile(db, tmp)
            except Exception as e:
                print("[!] %s：Cookie 库被占用（%s），请先完全退出浏览器再试" % (name, e))
                continue
            try:
                con = sqlite3.connect(tmp)
                rows = con.execute(
                    "SELECT name, encrypted_value, value FROM cookies "
                    "WHERE host_key LIKE '%bilibili.com'").fetchall()
                con.close()
            except Exception as e:
                print("[!] %s：读取失败 %s" % (name, e))
                continue
            finally:
                try:
                    os.remove(tmp)
                except Exception:
                    pass
            pairs = {}
            for cname, enc, plain in rows:
                val = plain or _decrypt_value(enc, key)
                if val:
                    pairs[cname] = val
            sd = pairs.get("SESSDATA", "")
            jct = pairs.get("bili_jct", "")
            if sd:
                print("[+] 从 %s 读到 SESSDATA（长度 %d）" % (name, len(sd)))
                if not jct:
                    print("[!] 没读到 bili_jct，稍后可能无法发送操作（仍先写入 SESSDATA）")
                write_config(sd, jct)
                found = True
                return
            else:
                print("[!] %s：库里没有 bilibili 的 SESSDATA（可能未登录或已退出）" % name)
    if not found:
        print("[X] 未能从浏览器读到 Cookie。可选：关闭所有浏览器窗口后重试，"
              "或手动 paste，或扫码登录。")


# ---------------- 扫码登录（终端二维码） ----------------

def _make_qr_ascii(url: str) -> str:
    try:
        import qrcode
    except Exception:
        return ""
    qr = qrcode.QRCode(border=1)
    qr.add_data(url)
    qr.make(fit=True)
    m = qr.get_matrix()
    # 用半块字符压缩竖向空间：两个像素→一行
    lines = []
    for y in range(0, len(m), 2):
        line = []
        for x in range(len(m[0])):
            top = m[y][x]
            bot = m[y + 1][x] if y + 1 < len(m) else False
            if top and bot:
                line.append("\u2588")
            elif top and not bot:
                line.append("\u2580")
            elif not top and bot:
                line.append("\u2584")
            else:
                line.append(" ")
        lines.append("".join(line))
    return "\n".join(lines)


def _save_qr_png(url: str) -> str:
    """生成二维码 PNG 并保存，返回路径（用于非 UTF-8 终端）。"""
    try:
        import qrcode
    except Exception:
        return ""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bili_qr.png")
    img = qrcode.make(url)
    img.save(path)
    return path


def do_qr() -> None:
    import httpx
    with httpx.Client(timeout=15, headers={"User-Agent": UA}) as c:
        r = c.get(GEN_URL)
        d = (r.json() or {}).get("data") or {}
        qr_url = d.get("url") or ""
        key = d.get("qrcode_key") or ""
        if not qr_url or not key:
            print("[X] 获取二维码失败:", r.text[:200])
            return
        # 始终生成 PNG 并弹出，便于直接拿手机扫（命令行 ASCII 不可靠）
        png = _save_qr_png(qr_url)
        if png:
            print("\n二维码图片已保存并弹出：%s" % png)
            print("（用手机 B 站 App「扫一扫」扫描该图片）")
            try:
                os.startfile(png)
            except Exception:
                pass
        else:
            print("\n未能生成二维码图片，改用浏览器打开链接：\n    " + qr_url)
            try:
                webbrowser.open(qr_url)
            except Exception:
                pass
        print("\n二维码链接（可复制到手机/浏览器打开）：\n    " + qr_url)
        print("\n等待扫码登录（最多 180 秒，扫完并在手机上点「确认」）...")
        deadline = time.time() + 180
        tip_shown = False
        while time.time() < deadline:
            time.sleep(2)
            rr = c.get(POLL_URL, params={"qrcode_key": key})
            j = rr.json() or {}
            data = j.get("data") or {}
            code = data.get("code")
            if code == 86090 and not tip_shown:
                print("[.] 已扫码，请在手机上点「确认」...")
                tip_shown = True
            if code == 0:
                cookies = rr.headers.get_list("set-cookie")
                sd = jct = ""
                for ck in cookies:
                    if "SESSDATA=" in ck:
                        sd = ck.split("SESSDATA=", 1)[1].split(";", 1)[0]
                    if "bili_jct=" in ck:
                        jct = ck.split("bili_jct=", 1)[1].split(";", 1)[0]
                if sd and jct:
                    write_config(sd, jct)
                    return
                # 兜底：从 client 的 cookie jar 取
                sd = c.cookies.get("SESSDATA") or sd
                jct = c.cookies.get("bili_jct") or jct
                if sd:
                    write_config(sd, jct)
                    return
                print("[X] 登录成功但提取 Cookie 失败，请改用 paste 模式")
                return
            if code == 86038:
                print("[X] 二维码已过期，请重新运行本脚本")
                return
        print("[X] 超时未扫码，请重新运行本脚本（或改用 browser / paste 模式）")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "browser"
    if mode == "paste" and len(sys.argv) > 2:
        parse_paste(sys.argv[2])
    elif mode == "qr":
        do_qr()
    else:
        read_from_browser()
