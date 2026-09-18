# -*- coding: utf-8 -*-
"""外观设置：主题配色、窗口标题、背景图，持久化到 data/appearance.json。

- 主题：暗色基调下切换强调色，外加一套浅色主题。
- 标题：作用于窗口标题栏、侧栏品牌名、浏览器标签，最长 40 字符。
- 背景：可为空（默认）、http(s) 远程图片链接，或本地上传图片（存于 data/appearance_bg）。
"""
import json
import os
import re
import base64
import io
import struct
from quiet import degrade

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(APP_DIR, "data")
DATA_FILE = os.path.join(DATA_DIR, "appearance.json")
BG_PATH = os.path.join(DATA_DIR, "appearance_bg")
BG_MAX = 8 * 1024 * 1024  # 背景图上限 8MB
ICON_PNG = os.path.join(APP_DIR, "webui", "icon.png")
ICON_ICO = os.path.join(APP_DIR, "webui", "favicon.ico")
ICON_DEFAULT_PNG = os.path.join(APP_DIR, "webui", "icon_default.png")
ICON_MAX = 8 * 1024 * 1024  # 图标上传上限 8MB

DEFAULT_TITLE = "肥鱼娘 · App 控制台"
DEFAULT_THEME = "midnight"

# 暗色中性调色板（各主题共用，仅强调色不同）
_DARK = {
    "--bg": "#0d1220", "--bg2": "#111829", "--panel": "#151d31",
    "--panel2": "#1a2338", "--line": "#26304a", "--text": "#dce3f2",
    "--muted": "#8b96ad", "--ok": "#3ecf8e", "--warn": "#f5b759",
    "--err": "#f56b6b",
}
# 浅色主题整盘替换
_LIGHT = {
    "--bg": "#f3f5fb", "--bg2": "#ffffff", "--panel": "#ffffff",
    "--panel2": "#eef2fb", "--line": "#dbe2f0", "--text": "#1c2435",
    "--muted": "#6b7589", "--ok": "#1fae6f", "--warn": "#d9942b",
    "--err": "#e05454",
}
# 各暗色主题专属暗色调色板（在 _DARK 基础上按色相偏移，使切换后整体观感明显区分，
# 而非仅换强调色）。midnight 用基准 _DARK，不在本表内。
_DARK_VARIANTS = {
    "aurora":  {"--bg": "#100e1c", "--bg2": "#15132a", "--panel": "#1b1832",
                "--panel2": "#211d3a", "--line": "#2e2a48", "--text": "#e0ddf2", "--muted": "#968fad"},
    "forest":  {"--bg": "#0c1611", "--bg2": "#101d16", "--panel": "#142219",
                "--panel2": "#18271d", "--line": "#243a2c", "--text": "#d6e6df", "--muted": "#87a395"},
    "rose":    {"--bg": "#160e14", "--bg2": "#1d131a", "--panel": "#211821",
                "--panel2": "#281d27", "--line": "#3a2836", "--text": "#f1dde6", "--muted": "#ad8fa2"},
    "sunset":  {"--bg": "#160f0a", "--bg2": "#1d150f", "--panel": "#211a14",
                "--panel2": "#281e16", "--line": "#3a2c20", "--text": "#f0e2d6", "--muted": "#ad9787"},
    "ocean":   {"--bg": "#0a1416", "--bg2": "#0f1d20", "--panel": "#122225",
                "--panel2": "#162a2e", "--line": "#203a3f", "--text": "#d6e6e8", "--muted": "#87a3a8"},
    "crimson": {"--bg": "#160c0e", "--bg2": "#1d1215", "--panel": "#211519",
                "--panel2": "#281c20", "--line": "#3a2429", "--text": "#f1dde0", "--muted": "#ad8f95"},
}
# 强调色（主 / 次）
_ACCENTS = {
    "midnight": ("#4f8cff", "#6ea8ff"),
    "aurora":   ("#a06bff", "#bd92ff"),
    "forest":   ("#3ecf8e", "#5fe0a6"),
    "rose":     ("#ff6b9d", "#ff8fb5"),
    "sunset":   ("#ff8c42", "#ffa463"),
    "ocean":    ("#2bc4d8", "#5fd8e6"),
    "crimson":  ("#f56b6b", "#ff8f8f"),
}

# 主题元信息（前端渲染色卡用）；accent 为色卡小圆点的强调色
THEMES = {
    "midnight": {"label": "暗夜蓝", "dark": True,  "accent": "#4f8cff"},
    "aurora":   {"label": "极光紫", "dark": True,  "accent": "#a06bff"},
    "forest":   {"label": "森林绿", "dark": True,  "accent": "#3ecf8e"},
    "rose":     {"label": "玫瑰粉", "dark": True,  "accent": "#ff6b9d"},
    "sunset":   {"label": "日落橙", "dark": True,  "accent": "#ff8c42"},
    "ocean":    {"label": "深海青", "dark": True,  "accent": "#2bc4d8"},
    "crimson":  {"label": "绯红",   "dark": True,  "accent": "#f56b6b"},
    "light":    {"label": "晨光白", "dark": False, "accent": "#4f8cff"},
}


def _build_vars(theme: str) -> dict:
    if theme == "light":
        base = dict(_LIGHT)
    else:
        base = dict(_DARK_VARIANTS.get(theme, _DARK))
    ac = _ACCENTS.get(theme, _ACCENTS["midnight"])
    base["--accent"] = ac[0]
    base["--accent2"] = ac[1]
    base["--radius"] = "12px"
    return base


def _read() -> dict:
    out = {"theme": DEFAULT_THEME, "title": DEFAULT_TITLE, "bg": ""}
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            d = json.load(f) or {}
        if isinstance(d.get("theme"), str) and d["theme"] in THEMES:
            out["theme"] = d["theme"]
        if isinstance(d.get("title"), str) and d["title"].strip():
            out["title"] = d["title"].strip()[:40]
        if isinstance(d.get("bg"), str):
            out["bg"] = d["bg"]
    except Exception as e:
        degrade("bridge/appearance_api.py:105 _read", e, "降级：with open(DATA_FILE, 'r', encoding='utf-8') as f")
    return out


def _write(d: dict) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)


def _rm_bg() -> None:
    try:
        if os.path.exists(BG_PATH):
            os.remove(BG_PATH)
    except OSError:
        pass


def _bg_content_type(raw: bytes) -> str:
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if raw[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if raw[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    return "image/png"


def load() -> dict:
    return _read()


def save(theme: str = None, title: str = None,
         bg: str = None, bg_data: str = None, clear_bg: bool = False) -> dict:
    """保存外观设置。背景参数三者取一：
    - clear_bg=True     清除背景
    - bg_data=数据URL   解码后以本地文件存储（bg 记为 "local"）
    - bg=链接/空串       直接记录远程链接或清空
    抛出异常表示参数非法（如链接非 http、图片超限）。
    """
    cur = _read()
    if theme is not None and theme in THEMES:
        cur["theme"] = theme
    if title is not None:
        t = str(title).strip()[:40]
        if t:
            cur["title"] = t
    if clear_bg:
        _rm_bg()
        cur["bg"] = ""
    elif bg_data and bg_data.startswith("data:"):
        m = re.match(r"^data:(image/[A-Za-z0-9.+-]+);base64,(.+)$", bg_data, re.S)
        if not m:
            raise ValueError("仅支持 PNG/JPEG/GIF/WEBP 图片")
        try:
            raw = base64.b64decode(m.group(2))
        except Exception:
            raise ValueError("图片数据无法解析")
        if len(raw) > BG_MAX:
            raise ValueError("图片过大（上限 8MB）")
        os.makedirs(DATA_DIR, exist_ok=True)
        _rm_bg()
        with open(BG_PATH, "wb") as f:
            f.write(raw)
        cur["bg"] = "local"
    elif bg is not None:
        u = str(bg).strip()
        if u and not u.lower().startswith(("http://", "https://")):
            raise ValueError("仅支持 http(s) 链接或本地上传图片")
        if u != "local":
            _rm_bg()
        cur["bg"] = u
    _write(cur)
    bg_url = _bg_url() if cur["bg"] == "local" else cur["bg"]
    return {**cur, "bg_url": bg_url}


def _bg_url() -> str:
    """本地背景图的带版本 URL（基于文件修改时间）。

    用于强制前端/WebView2 在重新上传后刷新缓存：每次上传都会更新文件
    mtime，URL 随之变化，浏览器不会命中旧图缓存。无本地背景图时返回空串。
    """
    if not os.path.exists(BG_PATH):
        return ""
    try:
        ts = int(os.path.getmtime(BG_PATH))
    except OSError:
        ts = 0
    return "/api/appearance/bg?v=%d" % ts


def read_bg() -> bytes:
    """读取本地背景图字节；无则返回 None。"""
    if not os.path.exists(BG_PATH):
        return None
    with open(BG_PATH, "rb") as f:
        return f.read()


def get_appearance(theme: str = None) -> dict:
    cur = _read()
    name = theme if (theme and theme in THEMES) else cur["theme"]
    return {
        "theme": cur["theme"],
        "title": cur["title"],
        "bg": cur["bg"],
        "bg_url": _bg_url() if cur["bg"] == "local" else cur["bg"],
        "themes": THEMES,
        "vars": _build_vars(name),
    }


def get_window_title() -> str:
    return _read()["title"]


# ---------------- 应用图标 ----------------

def _to_png(raw: bytes) -> bytes:
    """把任意图片字节归一为 PNG（RGBA）。优先本地 PIL，否则用独立版 venv 的 PIL 兜底。

    返回 PNG 字节；无法解码时抛 ValueError。
    """
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return raw  # 已是 PNG
    # 1) 本地 PIL
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(raw)).convert("RGBA")
        buf = io.BytesIO()
        im.save(buf, "PNG")
        return buf.getvalue()
    except Exception as e:
        degrade("bridge/appearance_api.py:241 _to_png", e, "降级：from PIL import Image")
    # 2) 独立版 venv 兜底（其 Python 自带 Pillow）
    for cand in (
        os.path.join(os.environ.get("FEIYU_QQ_BOT", ""), "..", "venv", "Scripts", "python.exe"),
        r"e:\qq_bot\venv\Scripts\python.exe",
    ):
        cand = os.path.abspath(cand)
        if os.path.isfile(cand):
            try:
                import subprocess
                script = (
                    "import sys,io,PIL.Image as I;"
                    "d=sys.stdin.buffer.read();"
                    'b=io.BytesIO();I.open(io.BytesIO(d)).convert("RGBA").save(b,"PNG");'
                    "sys.stdout.buffer.write(b.getvalue())"
                )
                p = subprocess.run([cand, "-c", script], input=raw,
                                   capture_output=True, timeout=30)
                if p.returncode == 0 and p.stdout[:8] == b"\x89PNG\r\n\x1a\n":
                    return p.stdout
            except Exception as e:
                degrade("bridge/appearance_api.py:262 _to_png", e, "降级：import subprocess")
    raise ValueError("无法解析该图片，请上传 PNG/JPG 等常见格式")


def _png_to_ico(png: bytes) -> bytes:
    """PNG-in-ICO 包裹（Vista+ 支持），无需外部库。单图，自动按 PNG 实际尺寸。"""
    # 解析 PNG 宽高（IHDR：偏移 16 起 4+4 字节）
    w = h = 0
    try:
        w = struct.unpack(">I", png[16:20])[0]
        h = struct.unpack(">I", png[20:24])[0]
        if w > 255:
            w = 0  # 0 在 ICO 中表示 256
        if h > 255:
            h = 0
    except Exception:
        w = h = 0
    out = struct.pack("<HHH", 0, 1, 1)  # ICONDIR：保留/类型/数量
    out += struct.pack("<BBBBHHII", w & 0xFF, h & 0xFF, 0, 0, 1, 32,
                       len(png), 6 + 16)  # ICONDIRENTRY
    out += png
    return out


def save_icon(icon_data: str) -> dict:
    """保存应用图标。icon_data 为 data URL（data:image/...;base64,...）。

    解码后归一为 PNG 写入 webui/icon.png，再生成 webui/favicon.ico。
    返回 {"ok": True, "ts": <秒级时间戳>}；非法输入抛 ValueError。
    """
    if not isinstance(icon_data, str) or not icon_data.startswith("data:"):
        raise ValueError("图标需为图片文件")
    m = re.match(r"^data:(image/[A-Za-z0-9.+-]+);base64,(.+)$", icon_data, re.S)
    if not m:
        raise ValueError("仅支持 PNG/JPEG/GIF/WEBP 等图片")
    try:
        raw = base64.b64decode(m.group(2))
    except Exception:
        raise ValueError("图片数据无法解析")
    if len(raw) > ICON_MAX:
        raise ValueError("图片过大（上限 8MB）")
    png = _to_png(raw)
    os.makedirs(os.path.dirname(ICON_PNG), exist_ok=True)
    with open(ICON_PNG, "wb") as f:
        f.write(png)
    with open(ICON_ICO, "wb") as f:
        f.write(_png_to_ico(png))
    import time
    return {"ok": True, "ts": int(time.time())}


def reset_icon() -> dict:
    """恢复默认图标：从 webui/icon_default.png 还原（首装时由当前图标生成）。"""
    if not os.path.isfile(ICON_DEFAULT_PNG):
        raise ValueError("未找到默认图标备份")
    with open(ICON_DEFAULT_PNG, "rb") as f:
        png = f.read()
    with open(ICON_PNG, "wb") as f:
        f.write(png)
    with open(ICON_ICO, "wb") as f:
        f.write(_png_to_ico(png))
    import time
    return {"ok": True, "ts": int(time.time())}
