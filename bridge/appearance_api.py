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

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(APP_DIR, "data")
DATA_FILE = os.path.join(DATA_DIR, "appearance.json")
BG_PATH = os.path.join(DATA_DIR, "appearance_bg")
BG_MAX = 8 * 1024 * 1024  # 背景图上限 8MB

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
        base = dict(_DARK)
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
    except Exception:
        pass
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
    return cur


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
        "themes": THEMES,
        "vars": _build_vars(name),
    }


def get_window_title() -> str:
    return _read()["title"]
