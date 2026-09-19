# -*- coding: utf-8 -*-
"""首启自举引导器（纯标准库，运行于捆绑裸 Python，无需 pywebview / tkinter）。

职责：
  1) 弹一个**本地 HTML 进度窗**（捆绑 Python 自带 http.server + 系统浏览器），
     显示百分比 + 当前正在装的包名 + 滚动日志 —— 解决「首启没有可视化进度」问题；
  2) 后台创建 venv 并安装 requirements-app.txt（带重试），显示真实进度；
  3) 检测到 N 卡时，在进度页上交互询问是否安装 CUDA 版 torch（替代原来的 set /p）；
  4) 依赖就绪后，以独立进程拉起 app.py（原生 UI），进度页显示「主界面已启动」。

设计约束：首启时 venv 尚未建立、pywebview 不存在，唯一可用的就是捆绑 Python 自带
标准库，因此进度窗用 html.server 而非 Tkinter（实测捆绑 Python 未带 tkinter）。
"""
import argparse
import os
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

# ---------------- 全局状态（被后台工作线程与 HTTP 线程共享）----------------
STATE = {
    "phase": "init",        # init | creating_venv | installing | ask_cuda | cuda_install | launching | done | error
    "percent": 0,
    "current": "",
    "log": [],
    "done": False,
    "error": "",
    "app_url": "",
    "need_cuda": False,
    "cuda_asked": False,
    "cuda_choice": "",
}
_STATE_LOCK = threading.Lock()
_CUDA_EVENT = threading.Event()   # 等待用户在进度页选择是否装 CUDA


def _log(msg):
    with _STATE_LOCK:
        STATE["log"].append(msg)
        if len(STATE["log"]) > 800:
            STATE["log"] = STATE["log"][-800:]
    try:
        print(msg)
    except Exception:
        pass


def _set(**kw):
    with _STATE_LOCK:
        STATE.update(kw)


def _get(key, default=None):
    with _STATE_LOCK:
        return STATE.get(key, default)


# ---------------- 进度页 HTML ----------------
def _progress_html() -> str:
    return """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>肥鱼娘 · 首次启动</title>
<style>
  :root{--bg:#0d1220;--fg:#e6ecf5;--acc:#5ad1c4;--warn:#ffb454;--err:#ff6b6b;--muted:#8a93a6}
  *{box-sizing:border-box}
  body{margin:0;background:radial-gradient(1200px 600px at 50% -10%,#16203a,#0d1220);
       color:var(--fg);font:15px/1.6 -apple-system,"Segoe UI",Roboto,"Microsoft YaHei",sans-serif;
       display:flex;align-items:center;justify-content:center;min-height:100vh;padding:24px}
  .card{width:min(640px,92vw);background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.08);
        border-radius:16px;padding:28px 30px;box-shadow:0 20px 60px rgba(0,0,0,.45)}
  h1{margin:0 0 4px;font-size:20px;letter-spacing:.5px}
  .sub{color:var(--muted);font-size:13px;margin-bottom:18px}
  .bar{height:12px;border-radius:8px;background:rgba(255,255,255,.08);overflow:hidden;position:relative}
  .fill{height:100%;width:0;background:linear-gradient(90deg,#5ad1c4,#7aa2ff);
        transition:width .35s ease;border-radius:8px}
  .meta{display:flex;justify-content:space-between;margin:10px 2px 0;font-size:13px;color:var(--muted)}
  .cur{color:var(--acc)}
  .log{margin-top:16px;background:rgba(0,0,0,.35);border-radius:10px;padding:12px 14px;
       height:160px;overflow:auto;font:12.5px/1.5 ui-monospace,Consolas,Menlo,monospace;
       white-space:pre-wrap;word-break:break-all;color:#cdd6e6}
  .btns{margin-top:18px;display:none;gap:12px}
  .btns.show{display:flex}
  button{flex:1;padding:11px;border:0;border-radius:10px;font-size:15px;cursor:pointer;font-weight:600}
  .yes{background:linear-gradient(90deg,#5ad1c4,#7aa2ff);color:#0d1220}
  .no{background:rgba(255,255,255,.1);color:var(--fg)}
  .done{margin-top:16px;text-align:center;display:none}
  .done.show{display:block}
  .done a{color:var(--acc);font-size:16px;text-decoration:none;font-weight:600}
  .err{margin-top:16px;color:var(--err);display:none;white-space:pre-wrap}
  .err.show{display:block}
  .spin{display:inline-block;width:12px;height:12px;border:2px solid rgba(255,255,255,.25);
        border-top-color:var(--acc);border-radius:50%;animation:s .8s linear infinite;vertical-align:-1px;margin-right:6px}
  @keyframes s{to{transform:rotate(360deg)}}
</style></head><body><div class="card">
  <h1>肥鱼娘 · 首次启动</h1>
  <div class="sub">正在为你自动搭建运行环境（创建虚拟环境并安装依赖）</div>
  <div class="bar"><div class="fill" id="fill"></div></div>
  <div class="meta"><span id="phase">准备中…</span><span class="cur" id="pct">0%</span></div>
  <div class="meta"><span>当前：<span class="cur" id="cur">—</span></span></div>
  <div class="log" id="log"></div>
  <div class="btns" id="btns">
    <button class="yes" id="yes">安装 CUDA 版（加速本地模型）</button>
    <button class="no" id="no">保持 CPU 版（跳过）</button>
  </div>
  <div class="done" id="done">
    <p>主界面已启动 🎉</p>
    <a id="open" href="#" target="_blank">打开肥鱼娘 →</a>
  </div>
  <div class="err" id="err"></div>
</div>
<script>
const $=id=>document.getElementById(id);
const ph={
  init:'准备中…',creating_venv:'创建虚拟环境',installing:'安装依赖',
  ask_cuda:'检测到 N 卡',cuda_install:'安装 CUDA 版 torch',
  launching:'启动主界面',done:'完成',error:'出错'
};
let lastLog=0;
async function tick(){
  try{
    const r=await fetch('/api/progress');const s=await r.json();
    $('fill').style.width=s.percent+'%';
    $('pct').textContent=s.percent+'%';
    $('phase').textContent=ph[s.phase]||s.phase;
    $('cur').textContent=s.current||'—';
    if(s.log&&s.log.length>lastLog){
      const add=s.log.slice(lastLog);lastLog=s.log.length;
      for(const l of add){const d=document.createElement('div');d.textContent=l;$('log').appendChild(d);}
      $('log').scrollTop=$('log').scrollHeight;
    }
    if(s.need_cuda&&!s.cuda_asked){$('btns').classList.add('show');}
    if(s.done){$('done').classList.add('show');if(s.app_url)$('open').href=s.app_url;}
    if(s.error){const e=$('err');e.textContent='环境搭建失败：\\n'+s.error;$('err').classList.add('show');}
  }catch(e){}
  setTimeout(tick,500);
}
$('yes').onclick=async()=>{await fetch('/api/cuda',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({choice:'yes'})});$('btns').classList.remove('show');};
$('no').onclick=async()=>{await fetch('/api/cuda',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({choice:'no'})});$('btns').classList.remove('show');};
tick();
</script></body></html>"""


# ---------------- HTTP 处理器 ----------------
def make_handler():
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _json(self, obj, code=200):
            data = (json_dumps(obj)).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            p = urlparse(self.path).path
            if p in ("/", "/index.html"):
                html = _progress_html().encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(html)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(html)
            elif p == "/api/progress":
                with _STATE_LOCK:
                    self._json(dict(STATE))
            else:
                self._json({"error": "not found"}, 404)

        def do_POST(self):
            p = urlparse(self.path).path
            if p == "/api/cuda":
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                    raw = self.rfile.read(n) if n else b"{}"
                    choice = (json_loads(raw.decode("utf-8") or "{}") or {}).get("choice", "")
                except Exception:
                    choice = ""
                with _STATE_LOCK:
                    STATE["cuda_choice"] = "yes" if choice == "yes" else "no"
                    STATE["cuda_asked"] = True
                _CUDA_EVENT.set()
                self._json({"ok": True})
            else:
                self._json({"error": "not found"}, 404)

    return Handler


def json_dumps(obj):
    import json
    return json.dumps(obj, ensure_ascii=False)


def json_loads(s):
    import json
    return json.loads(s)


# ---------------- 安装工作 ----------------
def _count_requirements(req_path):
    n = 0
    try:
        with open(req_path, "r", encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if not s or s.startswith("#") or s.startswith("-"):
                    continue
                n += 1
    except Exception:
        pass
    return max(n, 1)


def _run_pip(venv_py, req_path, index_url, on_line, retries=3):
    """运行 pip install，逐行回调 on_line(line)；失败重试。返回是否成功。"""
    for attempt in range(1, retries + 1):
        _log(f"[pip] 安装尝试 {attempt}/{retries} …")
        proc = subprocess.Popen(
            [venv_py, "-m", "pip", "install", "--retries", "5", "--timeout", "60",
             "-r", req_path],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, encoding="utf-8", errors="replace",
        )
        collected = set()
        total = _count_requirements(req_path)
        for line in proc.stdout:
            line = line.rstrip("\n")
            on_line(line)
            if line.startswith("Collecting "):
                name = line[len("Collecting "):].split()[0].split("(")[0].strip()
                if name and name not in collected:
                    collected.add(name)
                    pct = 15 + int(80 * len(collected) / total)
                    _set(percent=min(pct, 95), current=name)
        rc = proc.wait()
        if rc == 0:
            return True
        _log(f"[pip][WARN] 第 {attempt} 次失败（rc={rc}），稍后重试…")
        import time as _t
        _t.sleep(5)
    return False


def _has_webview(venv_py):
    proc = subprocess.run([venv_py, "-c", "import webview"],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return proc.returncode == 0


def _detect_nvidia():
    proc = subprocess.run(["nvidia-smi"], stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL, shell=True)
    return proc.returncode == 0


def _launch_app(venv_py, app_py, app_args, app_port):
    url = f"http://127.0.0.1:{app_port}"
    _set(app_url=url)
    _log(f"[launch] 启动主界面：{venv_py} {app_py} --with-core")
    try:
        subprocess.Popen(
            [venv_py, app_py, "--with-core"] + list(app_args),
            # 独立进程：firstboot 退出后 app 继续存活
            creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception as e:
        _log(f"[launch][ERR] 启动失败: {e!r}")
        _set(phase="error", error=f"启动主界面失败：{e!r}")
        return False
    return True


def worker(opts):
    """后台主流程：建 venv → 装依赖 → (可选 CUDA) → 启动 app。"""
    try:
        venv_py = os.path.join(opts.venv, "Scripts", "python.exe")

        # 1) 创建 venv
        if not os.path.isfile(venv_py):
            _set(phase="creating_venv", percent=5, current="python -m venv")
            _log(f"[venv] 创建虚拟环境：{opts.venv}")
            rc = subprocess.run([opts.rpy, "-m", "venv", opts.venv]).returncode
            if rc != 0:
                _set(phase="error", error="创建虚拟环境失败（捆绑 Python 可能不完整）")
                return
        else:
            _log("[venv] 虚拟环境已存在，跳过创建")

        # 2) 安装依赖
        _set(phase="installing", percent=10, current="解析依赖…")
        if _has_webview(venv_py):
            _log("[deps] 依赖已就绪（含 pywebview），跳过安装")
            _set(percent=95)
        else:
            ok = _run_pip(venv_py, opts.req, opts.index_url,
                          on_line=lambda l: (_log(l) if l.strip() else None))
            if not ok:
                _set(phase="error",
                     error="依赖安装失败（可能无网络/被墙）。可手动执行：\n"
                           f"{venv_py} -m pip install -r {opts.req}")
                return
            _log("[deps] 依赖安装完成")
            _set(percent=95)

        # 3) N 卡交互询问（仅自建成 CPU 版时）
        if _detect_nvidia():
            _log("[gpu] 检测到 NVIDIA 显卡，等待用户选择是否安装 CUDA 版 torch…")
            with _STATE_LOCK:
                STATE["need_cuda"] = True
                STATE["phase"] = "ask_cuda"
                STATE["current"] = "等待选择"
            _CUDA_EVENT.clear()
            _CUDA_EVENT.wait(timeout=180)   # 超时默认保持 CPU
            choice = _get("cuda_choice", "no")
            _log(f"[gpu] 用户选择：{choice}")
            if choice == "yes":
                _set(phase="cuda_install", percent=96, current="torch cu128")
                _log("[gpu] 安装 CUDA 版 torch（cu128）…")
                proc = subprocess.Popen(
                    [venv_py, "-m", "pip", "install", "torch", "torchvision",
                     "torchaudio", "--index-url",
                     "https://download.pytorch.org/whl/cu128"],
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, bufsize=1, encoding="utf-8", errors="replace")
                for line in proc.stdout:
                    _log(line.rstrip("\n"))
                if proc.wait() != 0:
                    _log("[gpu][WARN] CUDA 版安装失败，保持 CPU 版")
                else:
                    _log("[gpu] 已切换 CUDA 版 torch")

        # 4) 启动主界面
        _set(phase="launching", percent=99, current="启动中…")
        if not opts.no_launch:
            if not _launch_app(venv_py, opts.app, opts.app_args, opts.app_port):
                return
        _set(phase="done", percent=100, done=True, current="完成")
        _log("[done] 首启完成")
    except Exception as e:
        _set(phase="error", error=repr(e))
        _log(f"[FATAL] {e!r}")


# ---------------- 入口 ----------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", required=True)
    ap.add_argument("--venv", required=True)
    ap.add_argument("--rpy", required=True, help="捆绑解释器路径")
    ap.add_argument("--req", required=True, help="requirements 文件路径")
    ap.add_argument("--app", required=True, help="app.py 路径")
    ap.add_argument("--index-url", default="https://pypi.org/simple")
    ap.add_argument("--progress-port", type=int, default=8910)
    ap.add_argument("--app-port", type=int, default=8900)
    ap.add_argument("--no-launch", action="store_true")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("app_args", nargs="*")
    opts = ap.parse_args()

    # 起 HTTP 进度服务（仅本机）
    srv = ThreadingHTTPServer(("127.0.0.1", opts.progress_port), make_handler())
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{opts.progress_port}"
    _log(f"[boot] 进度窗：{url}")

    if not opts.no_browser:
        try:
            webbrowser.open(url)
        except Exception as e:
            _log(f"[boot][WARN] 浏览器打开失败: {e!r}（请手动访问 {url}）")

    # 后台执行建环境流程
    threading.Thread(target=worker, args=(opts,), daemon=False).start()

    try:
        while True:
            time_sleep(0.5)
            if _get("done") or _get("error"):
                # 完成后保留服务 60s 供用户查看，再退出
                time_sleep(60)
                break
    except KeyboardInterrupt:
        pass
    finally:
        srv.shutdown()


def time_sleep(s):
    import time
    time.sleep(s)


if __name__ == "__main__":
    main()
