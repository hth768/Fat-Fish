# -*- coding: utf-8 -*-
"""B站直播字幕服务：把她说的话实时推给直播姬的「浏览器素材」上屏。

原理：
    bilibili_plugin 里所有她开口的出口（弹幕回应/开口感谢/主动闲聊/主人点播）
    → push(text) 写入环形缓冲
    → 本地 HTTP 服务（标准库，无新依赖）：
        GET /             字幕页面（直播姬浏览器素材填 http://127.0.0.1:{port}/）
        GET /api/lines    最近台词 JSON（页面轮询，1s 一次）
    → 直播姬加一个「浏览器素材」指向本服务，她说话的文字就实时显示在直播画面。

特性：
    - 最近 50 条环形缓冲，页面只渲染最近 3 条（一条大全屏主字幕 + 上两条小字历史）；
    - 新字幕淡入、旧字幕淡出，纯 CSS 动画，页面无外部依赖；
    - 服务随开播启动（_boot）/关播停止（_go_standby）。
"""
import json
import os
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import config

_MAX_LINES = 50
_PORT = 8768
_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<style>
  html, body { margin: 0; padding: 0; background: transparent; overflow: hidden;
               font-family: "Microsoft YaHei", "PingFang SC", sans-serif; }
  #wrap { position: fixed; left: 0; right: 0; bottom: 24px; display: flex;
          flex-direction: column-reverse; align-items: flex-start; padding: 0 32px; }
  .line { max-width: 92%; padding: 10px 22px; margin-top: 10px;
          background: rgba(0, 0, 0, 0.55); border-left: 6px solid #4aa8ff;
          border-radius: 10px; color: #fff; font-size: 34px; font-weight: 700;
          text-shadow: 0 2px 6px rgba(0,0,0,.8); line-height: 1.5; opacity: 0;
          transform: translateY(16px); transition: opacity .4s, transform .4s; }
  .line.show { opacity: 1; transform: translateY(0); }
  .line.old { opacity: .55; font-size: 26px; font-weight: 500; }
  .line.dim { opacity: .28; font-size: 22px; }
</style>
</head>
<body>
<div id="wrap"></div>
<script>
const MAX = 3;
let seen = 0;
async function tick() {
  try {
    const r = await fetch('/api/lines?_=' + Date.now());
    const d = await r.json();
    const lines = d.lines || [];
    const wrap = document.getElementById('wrap');
    wrap.innerHTML = '';
    const shown = lines.slice(-MAX).reverse();
    shown.forEach((it, i) => {
      const el = document.createElement('div');
      el.className = 'line' + (i === 0 ? ' show' : (i === 1 ? ' old' : ' dim'));
      el.textContent = it.text;
      wrap.appendChild(el);
      seen = Math.max(seen, it.id || 0);
    });
  } catch (e) { /* 服务重启间隙静默 */ }
  setTimeout(tick, 1000);
}
tick();
</script>
</body>
</html>"""


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/":
            body = _PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/lines":
            body = json.dumps({"lines": _store.snapshot()}, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, fmt, *args):
        pass  # 静默，避免刷日志


class _CaptionStore:
    """环形台词缓冲：push() 供插件各出口调用。"""

    def __init__(self):
        self._lines = deque(maxlen=_MAX_LINES)
        self._next_id = 1
        self._lock = threading.Lock()

    def push(self, text: str):
        text = (text or "").strip()
        if not text:
            return
        with self._lock:
            self._lines.append({"id": self._next_id, "text": text[:120],
                                "ts": time.time()})
            self._next_id += 1

    def snapshot(self):
        with self._lock:
            return list(self._lines)


_store = _CaptionStore()
_server = None
_thread = None


def push(text: str):
    """她开口时调用：把台词推到字幕服务。"""
    _store.push(text)


def is_running() -> bool:
    return _server is not None


def start():
    """启动字幕服务（幂等）。"""
    global _server, _thread
    if _server is not None:
        return
    port = int(getattr(config, "BILIBILI_CAPTIONS_PORT", _PORT) or _PORT)
    try:
        _server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    except OSError as e:
        print(f"[BILI-CAPTIONS] 字幕服务启动失败（端口 {port} 被占？）: {e}")
        _server = None
        return
    _thread = threading.Thread(target=_server.serve_forever, daemon=True,
                               name="bili-captions")
    _thread.start()
    print(f"[BILI-CAPTIONS] 字幕服务已启动: http://127.0.0.1:{port}/"
          "（直播姬加「浏览器素材」指向它）")


def stop():
    """停止字幕服务（幂等）。"""
    global _server, _thread
    if _server is not None:
        try:
            _server.shutdown()
            _server.server_close()
        except Exception:
            pass
        _server = None
    if _thread is not None:
        _thread = None
