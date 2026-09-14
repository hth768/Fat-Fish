# -*- coding: utf-8 -*-
"""监控 sidecar 进程（对齐 N.E.K.O 的 monitor / 本地遥测进程）。

为 bot 提供「可观测性」：独立进程暴露状态与指标，隔离于聊天主循环，即使 bot 卡死
也能单独查看。主进程按 MONITOR_PUSH_INTERVAL 周期把 `core.status()` 快照 + 各 sidecar
健康度推送过来；本服务聚合后在 HTTP 端点/简易看板展示。

端点：
    GET  /health   -> {"ok": true}
    POST /status   -> 接收 bot 推送的快照 {"core":..., "memory_sidecar": bool, "ts":...}
    GET  /status   -> 最近一次快照 + monitor 自身 uptime + 各 sidecar 健康探测
    GET  /         -> 简易 HTML 看板

设计：
    - 仅 127.0.0.1。
    - stdlib ThreadingHTTPServer 轻量实现。
    - 仅自身（监控）端口可配置；对其它 sidecar 的健康探测通过各自 /health 完成。
"""
import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import urlopen
from urllib.error import URLError

START_TS = time.time()
LATEST = {"core": None, "memory_sidecar": None, "providers": None, "vision": None, "ts": None}
# 被监控的 sidecar 列表（name -> health URL），启动时注入
SIDECAR_HEALTH = {}


def _check_sidecar(name, url):
    try:
        with urlopen(url + "/health", timeout=2):
            return True
    except (URLError, OSError):
        return False


def _snapshot():
    sidecars = {name: _check_sidecar(name, url) for name, url in SIDECAR_HEALTH.items()}
    return {
        "ok": True,
        "monitor_uptime_s": round(time.time() - START_TS, 1),
        "sidecars": sidecars,
        "bot": LATEST.get("core"),
        "bot_pushed_ts": LATEST.get("ts"),
        "memory_sidecar": LATEST.get("memory_sidecar"),
        "providers": LATEST.get("providers"),
        "vision": LATEST.get("vision"),
    }


HTML = """<!doctype html><meta charset=utf-8><title>肥鱼娘 监控</title>
<style>body{font-family:system-ui;background:#11131a;color:#e6e6e6;padding:24px}
h1{color:#7fd1ff}pre{background:#1b1e27;padding:16px;border-radius:8px;overflow:auto}
.k{color:#9fb3c8}.v{color:#a6e3a1}.bad{color:#f38ba8}</style>
<h1>🐟 肥鱼娘 运行监控</h1>
<pre id=o>加载中…</pre>
<script>async function r(){let j=await fetch('/status').then(x=>x.json());
let s='';function p(k,v,ind){s+=('  '.repeat(ind))+'<span class=k>'+k+':</span> '+
('<span class=v>'+JSON.stringify(v)+'</span>')+'\\n';}
p('监控运行时长(s)',j.monitor_uptime_s,0);
p('sidecars',j.sidecars,0);
p('memory_sidecar(最近推送)',j.memory_sidecar,0);
p('bot_push_ts',j.bot_pushed_ts,0);
if(j.bot){let b=j.bot;p('bot.platform',b.platform,0);
p('bot.brains',(b.brains||[]).map(x=>x.name+':'+x.status),0);}
if(j.providers){let pr=j.providers;p('llm_providers',Object.keys(pr).length+' 家',0);
for(let k in pr){let v=pr[k];p('  '+k,{calls:v.calls,ok:v.ok,fail:v.fail,
fail_rate:v.fail_rate,avg_ms:v.avg_latency_ms,tok:v.tokens_total},1);}}
if(j.vision){let vi=j.vision;p('vision_drivers',Object.keys(vi).length+' 个',0);
for(let k in vi){let v=vi[k];p('  '+k,{calls:v.calls,ok:v.ok,fail:v.fail,
fail_rate:v.fail_rate,avg_ms:v.avg_latency_ms,err:v.last_error},1);}}
document.getElementById('o').innerHTML=s;}setInterval(r,2000);r();</script>"""


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, payload, ctype="application/json"):
        if ctype == "application/json":
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        else:
            body = payload.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/health":
            self._send(200, {"ok": True})
        elif path == "/status":
            self._send(200, _snapshot())
        elif path == "/" or path == "/index.html":
            self._send(200, HTML, "text/html")
        else:
            self._send(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        if self.path.split("?")[0] != "/status":
            self._send(404, {"ok": False, "error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            body = json.loads(raw.decode("utf-8"))
        except Exception as e:
            self._send(400, {"ok": False, "error": f"bad request: {e}"})
            return
        LATEST["core"] = body.get("core")
        LATEST["memory_sidecar"] = body.get("memory_sidecar")
        LATEST["providers"] = body.get("providers")
        LATEST["vision"] = body.get("vision")
        LATEST["ts"] = body.get("ts")
        self._send(200, {"ok": True})


def main():
    p = argparse.ArgumentParser(description="肥鱼娘 监控 sidecar")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8770)
    p.add_argument("--memory-url", default="http://127.0.0.1:8766")
    args = p.parse_args()

    if args.memory_url:
        SIDECAR_HEALTH["memory"] = args.memory_url

    httpd = ThreadingHTTPServer((args.host, args.port), _Handler)
    print(f"[monitor] 已启动 http://{args.host}:{args.port} (看板: / )", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
