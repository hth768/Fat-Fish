# -*- coding: utf-8 -*-
"""构建助手：用 LLM 生成「智能体」或「插件」产物，严格校验后落盘（保证可适配）。

- 智能体：生成 agents/<id>/agent.json，走 agent_manager.save_agent 校验必填/默认值。
- 插件：生成 plugins/<name>/（manifest.json + plugin.py），做 manifest 字段 + py_compile + 入口函数(AST) 校验。
LLM 调用通过 bridge.lt.run_coro 在同步 handler 里跑异步 chat。
"""
import ast
import json
import os
import py_compile
import re
import shutil
import sys
import tempfile
import time
from datetime import datetime

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGINS_DIR = os.path.join(APP_DIR, "plugins")

VALID_KINDS = {"platform", "feature", "brain", "sidecar", "local"}
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_\-]*$")
AGENT_MODEL_HINT = ["gpt-4o-mini", "gpt-4o", "deepseek-chat", "glm-4"]

AGENT_SYSTEM = """你是一个「智能体构建助手」。根据用户的一句话需求，生成一套肥鱼娘智能体的定义，只输出一个 JSON 对象（不要任何解释、不要 markdown 代码围栏）。

字段说明（严格按此结构）：
{
  "name": "显示名（简短，中文或英文）",
  "emoji": "一个 emoji 图标，如 🐟",
  "system_prompt": "系统提示词：写清它的身份、性格、说话风格、职责（用中文）",
  "profile": {"age":"","gender":"","species":"","personality":"","background":""},
  "model": "从 ["gpt-4o-mini","gpt-4o","deepseek-chat","glm-4"] 中选一个",
  "enabled_brains": ["大脑能力键，从已提供列表选取，可空"],
  "enabled_plugins": ["功能插件名，从已提供列表选取，可空"],
  "bindings": [{"platform":"qq|discord|telegram|...","type":"private|group|channel","id":"账号/群号，无则空串"}],
  "auto_start": false
}
约束：若无明确平台绑定则 bindings 给空数组；profile 需是对象（字段可酌情省略）；enabled_brains/enabled_plugins 只能是已提供列表里的名字。"""

PLUGIN_SYSTEM = """你是一个「插件构建助手」。根据用户的一句话需求，生成一套可适配肥鱼娘的插件包，只输出一个 JSON 对象（不要解释、不要 markdown 代码围栏）。

结构：
{
  "manifest": {
    "name": "蛇形小写名，如 my_tool（必须匹配目录名，且只能含小写字母数字/_/-，以字母数字开头）",
    "title": "中文显示名",
    "description": "一句话说明",
    "kind": "feature|local|brain|platform|sidecar 之一",
    "version": "1.0.0",
    "config_schema": [{"key":"","label":"","type":"str|int|float|bool|choice|secret|json","desc":"","default":"","choices":[]}],
    "author": "可选"
  },
  "code": "plugin.py 的完整源码字符串（换行用 \\n 表示，确保整体是合法 JSON 字符串）"
}

插件契约（务必遵守，否则系统校验/干加载会失败）：
- feature / local / platform / sidecar：plugin.py 必须定义 create_plugin(core) -> 返回 Plugin 子类实例（注册进 core.plugins）。
- brain：plugin.py 必须定义 create_brain(core) -> 返回 AgentBrain 子类实例（注册进 core.brains）。
- 不要 import 不存在的第三方库；如需要请用标准库直接 import。
- 构造实例时只需妥善保存 core，不要在 __init__/模块顶层执行阻塞或危险操作。
- 可选：实现 on_config(cfg) 方法接收用户在「扩展设置」中填写的参数（cfg 为 dict），在 start() 前会被调用。
- 无配置则 config_schema 给 []。

最小可用 feature 插件示例：
import time
from plugin_base import FeaturePlugin

class ReminderPlugin(FeaturePlugin):
    name = "my_reminder"
    def __init__(self, core):
        super().__init__(core)
        self.interval = 60
    def on_config(self, cfg):
        self.interval = int(cfg.get("interval", 60))
    async def start(self):
        await super().start()
    async def stop(self):
        await super().stop()

def create_plugin(core):
    return ReminderPlugin(core)

最小可用 brain 插件示例：
from brain_base import AgentBrain

class MyBrain(AgentBrain):
    name = "my_brain"
    title = "示例大脑"
    kind = "chat"
    def __init__(self, core):
        super().__init__(core)
    async def start(self):
        await super().start()
    async def stop(self):
        await super().stop()

def create_brain(core):
    return MyBrain(core)

最小可用 platform 插件要点：继承 PlatformPlugin，务必设置类属性 platform="xxx"，并在 start() 里把平台消息转成 InboundMessage 后调用 core.chat.handle_message(msg, reply)，同时实现 ReplyTarget/MessageSender（参考控制台平台插件 console_plugin.py）。"""


# ----------------------------------------------------------------------
# 上下文：可用的插件 / 大脑，喂给 LLM 让生成结果可适配
# ----------------------------------------------------------------------
def _available_context() -> dict:
    out = {"plugins": [], "brains": []}
    try:
        from .pkg_manager import scan_packages
    except Exception:
        try:
            from pkg_manager import scan_packages
        except Exception:
            scan_packages = None
    if scan_packages:
        try:
            for p in scan_packages():
                nm = p.get("name")
                if nm:
                    out["plugins"].append({"name": nm, "kind": p.get("kind"), "title": p.get("title")})
        except Exception:
            pass
    try:
        import plugin_registry as reg
        for spec in reg.by_kind("brain"):
            k = getattr(spec, "key", None) or getattr(spec, "name", None)
            if k:
                out["brains"].append(k)
    except Exception:
        pass
    return out


# ----------------------------------------------------------------------
# 文件上下文（让构建助手能读取项目源码，产出更贴合现有代码的产物）
# ----------------------------------------------------------------------
CONTEXT_ALLOW_DIRS = ["libs/qq_bot_runtime", "bridge", "webui", "plugins"]
CONTEXT_SKIP_DIRS = {"__pycache__", "node_modules", ".git", "data", "runtime",
                     "Lib", "Scripts", "venv", "venv_vox", "dist"}
MAX_CONTEXT_FILE_BYTES = 48000
MAX_CONTEXT_FILES = 150


def _is_allowed_context_path(abspath: str) -> bool:
    ap = os.path.normcase(os.path.abspath(abspath))
    root = os.path.normcase(os.path.abspath(APP_DIR))
    if ap != root and not ap.startswith(root + os.sep):
        return False
    rel = os.path.relpath(ap, root).replace(os.sep, "/")
    return any(rel == d or rel.startswith(d + "/") for d in CONTEXT_ALLOW_DIRS)


def list_context_files() -> dict:
    """列出可作为上下文读取的项目源码文件（安全白名单 + 大小限制）。"""
    out = []
    try:
        for d in CONTEXT_ALLOW_DIRS:
            base = os.path.join(APP_DIR, d)
            if not os.path.isdir(base):
                continue
            for dirpath, dirnames, filenames in os.walk(base):
                dirnames[:] = [n for n in dirnames if n not in CONTEXT_SKIP_DIRS]
                for fn in filenames:
                    if fn.endswith((".py", ".json")) and not fn.endswith(".tmp"):
                        fp = os.path.join(dirpath, fn)
                        try:
                            sz = os.path.getsize(fp)
                        except OSError:
                            continue
                        if sz > MAX_CONTEXT_FILE_BYTES:
                            continue
                        rel = os.path.relpath(fp, APP_DIR).replace(os.sep, "/")
                        out.append({"path": rel, "size": sz})
    except Exception:
        pass
    out.sort(key=lambda x: x["path"])
    return {"ok": True, "files": out[:MAX_CONTEXT_FILES]}


def read_context_file(rel: str) -> dict:
    """读取单个上下文文件内容（带越权 / 大小保护）。"""
    if not rel or not isinstance(rel, str):
        return {"ok": False, "error": "路径为空"}
    fp = os.path.normpath(os.path.join(APP_DIR, rel))
    if not _is_allowed_context_path(fp):
        return {"ok": False, "error": "路径不在允许范围内: %s" % rel}
    if not os.path.isfile(fp):
        return {"ok": False, "error": "文件不存在: %s" % rel}
    try:
        with open(fp, "r", encoding="utf-8", errors="replace") as f:
            content = f.read(MAX_CONTEXT_FILE_BYTES)
    except Exception as e:
        return {"ok": False, "error": "读取失败: %r" % e}
    return {"ok": True, "path": rel, "content": content, "size": os.path.getsize(fp)}


def _context_block(rels) -> str:
    """把选中的文件拼成提示词上下文块。"""
    if not rels:
        return ""
    parts = []
    for rel in rels:
        if not isinstance(rel, str) or not rel.strip():
            continue
        r = read_context_file(rel.strip())
        if r.get("ok"):
            parts.append("### 参考文件：%s\n```\n%s\n```" % (r["path"], r.get("content", "")))
    if not parts:
        return ""
    return ("\n\n#### 项目上下文（供参考，产出请贴合现有代码风格 / 契约）\n"
            + "\n\n".join(parts) + "\n")


# ----------------------------------------------------------------------
# 构建历史（持久化为文件，支持「改进」复用历史成功案例）
# ----------------------------------------------------------------------
DATA_DIR = os.path.join(APP_DIR, "data")
HISTORY_PATH = os.path.join(DATA_DIR, "builder_history.jsonl")
MAX_HISTORY_LINES = 500


def _record_history(event, mode, requirement, ok, artifact, rounds=1,
                    model=None, provider=None, key=None, error=None):
    """写入一条构建历史（追加到 jsonl 文件）。"""
    rec = {
        "event": event, "mode": mode, "requirement": requirement, "ok": ok,
        "rounds": rounds, "model": model, "provider": provider, "key": key,
    }
    if artifact is not None:
        try:
            s = json.dumps(artifact, ensure_ascii=False)
            if len(s) > 6000:
                s = s[:6000] + " …(已截断)"
            rec["artifact"] = json.loads(s)
        except Exception:
            rec["artifact"] = None
    if error:
        rec["error"] = str(error)[:500]
    append_history(rec)


def append_history(rec: dict):
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        rec = dict(rec)
        rec.setdefault("time", int(time.time()))
        with open(HISTORY_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        _trim_history()
    except Exception:
        pass


def _trim_history():
    try:
        if not os.path.isfile(HISTORY_PATH):
            return
        with open(HISTORY_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()
        if len(lines) <= MAX_HISTORY_LINES:
            return
        with open(HISTORY_PATH, "w", encoding="utf-8") as f:
            f.writelines(lines[-MAX_HISTORY_LINES:])
    except Exception:
        pass


def get_history(limit: int = 60) -> dict:
    out = []
    try:
        if os.path.isfile(HISTORY_PATH):
            with open(HISTORY_PATH, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        out.append(json.loads(line))
                    except Exception:
                        continue
    except Exception:
        pass
    out.reverse()
    return {"ok": True, "history": out[:limit]}


def _history_block(mode: str, exclude_key: str = None) -> str:
    """取最近 2 条同类型成功案例作为 few-shot 改进范例。"""
    try:
        hist = get_history(limit=200)["history"]
    except Exception:
        return ""
    ex = []
    for rec in hist:
        if rec.get("mode") != mode or not rec.get("ok"):
            continue
        if exclude_key and rec.get("key") == exclude_key:
            continue
        art = rec.get("artifact")
        if not art:
            continue
        ex.append((rec.get("requirement", ""), art))
        if len(ex) >= 2:
            break
    if not ex:
        return ""
    lines = ["\n#### 历史成功案例（可作为风格 / 契约参考，产出请优于它们）"]
    for req, art in ex:
        try:
            art_s = json.dumps(art, ensure_ascii=False) if not isinstance(art, str) else art
        except Exception:
            art_s = str(art)
        if len(art_s) > 3500:
            art_s = art_s[:3500] + " …(已截断)"
        lines.append("- 需求：%s\n  产物：%s" % (req, art_s))
    return "\n".join(lines) + "\n"


# ----------------------------------------------------------------------
# LLM 调用
# ----------------------------------------------------------------------
async def _llm_json(system: str, user: str, model: str = None, think: bool = False,
                  provider: str = None) -> dict:
    from ai_provider import get_llm
    out = await get_llm().chat(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        capability="chat",
        model=model or None,
        think=bool(think),
        provider=provider or None,
    )
    return _extract_json(str(out or ""))


def _extract_json(text: str) -> dict:
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if m:
        text = m.group(1)
    else:
        s = text.find("{")
        e = text.rfind("}")
        if s != -1 and e != -1 and e > s:
            text = text[s:e + 1]
    return json.loads(text.strip())


# ----------------------------------------------------------------------
# 规范化 / 校验
# ----------------------------------------------------------------------
def _as_str_list(v):
    if isinstance(v, list):
        return [str(x) for x in v if str(x).strip()]
    if isinstance(v, str) and v.strip():
        return [v.strip()]
    return []


def _normalize_agent(d: dict):
    if not isinstance(d, dict):
        return None, "返回不是对象"
    name = (d.get("name") or "").strip()
    if not name:
        return None, "缺少 name"
    profile = d.get("profile")
    if not isinstance(profile, dict):
        profile = {}
    binds = []
    for b in (d.get("bindings") or []):
        if isinstance(b, dict) and (b.get("platform") or "").strip() and (b.get("type") or "").strip():
            binds.append({
                "platform": str(b["platform"]).strip(),
                "type": str(b["type"]).strip(),
                "id": str(b.get("id") or "").strip(),
            })
    out = {
        "name": name,
        "emoji": (d.get("emoji") or "🐟")[:4],
        "system_prompt": str(d.get("system_prompt") or "").strip(),
        "profile": profile,
        "model": (d.get("model") or "gpt-4o-mini").strip() or "gpt-4o-mini",
        "enabled_brains": _as_str_list(d.get("enabled_brains")),
        "enabled_plugins": _as_str_list(d.get("enabled_plugins")),
        "bindings": binds,
        "auto_start": bool(d.get("auto_start")),
    }
    return out, None


def _normalize_plugin(d: dict):
    if not isinstance(d, dict):
        return None, "返回不是对象"
    m = d.get("manifest")
    code = d.get("code")
    if not isinstance(m, dict):
        return None, "缺少 manifest"
    if not isinstance(code, str) or not code.strip():
        return None, "缺少/空的 code（plugin.py 源码）"
    name = (m.get("name") or "").strip()
    if not name:
        return None, "manifest 缺少 name"
    if not NAME_RE.match(name):
        return None, "name 必须为小写蛇形（字母数字/_/-，且以字母数字开头）"
    kind = (m.get("kind") or "").strip().lower()
    if kind not in VALID_KINDS:
        return None, "kind 非法: %s（应为 %s 之一）" % (kind, "/".join(sorted(VALID_KINDS)))
    schema = m.get("config_schema") or []
    if not isinstance(schema, list):
        schema = []
    for f in schema:
        if not isinstance(f, dict) or "key" not in f:
            return None, "config_schema 项需为含 key 的对象"
    out = {
        "name": name,
        "title": (m.get("title") or name).strip() or name,
        "description": (m.get("description") or "").strip(),
        "kind": kind,
        "version": (m.get("version") or "1.0.0").strip() or "1.0.0",
        "config_schema": schema,
        "author": (m.get("author") or "").strip(),
        "code": code,
    }
    return out, None


def _check_plugin_code(code: str, kind: str):
    """py_compile + 入口函数存在性（AST，不实际导入，避免重依赖误判）。"""
    with tempfile.TemporaryDirectory() as td:
        f = os.path.join(td, "plugin.py")
        try:
            with open(f, "w", encoding="utf-8") as fh:
                fh.write(code)
            py_compile.compile(f, doraise=True)
        except py_compile.PyCompileError as e:
            return "plugin.py 语法错误: %s" % e
        except Exception as e:
            return "plugin.py 写入/编译失败: %s" % e
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return "plugin.py 解析失败: %s" % e
    names = {n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    needed = "create_brain" if kind == "brain" else "create_plugin"
    if needed not in names:
        return "plugin.py 缺少入口函数 %s(core)" % needed
    return None


class _FakeManager:
    """干加载用的占位注册表。"""

    def __init__(self):
        self._reg = []

    def register(self, x):
        self._reg.append(x)
        return x


class _FakeCore:
    """干加载用的占位核心：覆盖插件构造/入口会碰到的常见属性。

    未知属性返回可任意调用/取值的占位对象，避免构造期访问 core.xxx 时炸。
    """

    def __init__(self):
        self.plugins = _FakeManager()
        self.brains = _FakeManager()
        self.bus = None
        self.chat = None
        self.config = {}

    def __getattr__(self, name):
        return _FakeCore._placeholder()

    @staticmethod
    def _placeholder():
        class _P:
            def __getattr__(self, _n):
                return _FakeCore._placeholder()

            def __call__(self, *a, **k):
                return _FakeCore._placeholder()

        return _P()


def _dry_load_plugin(code: str, kind: str, name: str):
    """真导入 + 实例化校验（干加载）：确保落盘即可被 pkg_manager 装载。

    返回 None 表示通过；返回字符串表示错误（会回喂 LLM 修正）。
    等价于：把 plugin.py 用 spec_from_file_location 加载，调用 create_plugin/create_brain(core)，
    再 isinstance 校验返回类型与基类。
    """
    runtime_dir = os.path.join(APP_DIR, "libs", "qq_bot_runtime")
    saved_path = sys.path[:]
    if runtime_dir and runtime_dir not in sys.path:
        sys.path.insert(0, runtime_dir)
    mod_name = "feiyu_dryload_%s" % re.sub(r"[^a-z0-9]", "_", (name or "x"))
    tmp = None
    try:
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "plugin.py")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(code)
        try:
            import importlib.util as _ilu
            spec = _ilu.spec_from_file_location(mod_name, path)
            mod = _ilu.module_from_spec(spec)
            sys.modules[mod_name] = mod
            spec.loader.exec_module(mod)
        except Exception as e:
            return "导入失败（语法/模块/依赖错误）: %s: %s" % (type(e).__name__, e)
        entry = "create_brain" if kind == "brain" else "create_plugin"
        fn = getattr(mod, entry, None)
        if fn is None:
            return "缺少入口函数 %s(core)" % entry
        inst = None
        try:
            inst = fn(_FakeCore())
        except Exception as e:
            return "调用 %s(core) 失败: %s: %s" % (entry, type(e).__name__, e)
        # 类型校验（基类模块缺失时降级跳过，仅报导入级错误）
        try:
            if kind == "brain":
                import brain_base
                if not isinstance(inst, brain_base.AgentBrain):
                    return "%s 返回值须是 AgentBrain 子类实例（实际: %s）" % (entry, type(inst).__name__)
            else:
                import plugin_base
                if not isinstance(inst, plugin_base.Plugin):
                    return "%s 返回值须是 Plugin 子类实例（实际: %s）" % (entry, type(inst).__name__)
        except Exception as e:
            return "基类校验无法进行（可能运行时环境缺失）: %s" % e
        # kind 专属契约
        if kind == "platform":
            plat = getattr(inst, "platform", None)
            if not plat or plat == "base":
                return "PlatformPlugin 实例须将类属性 platform 覆盖为你的平台标识（不要保留基类默认的 'base'）"
        return None
    finally:
        sys.path[:] = saved_path
        sys.modules.pop(mod_name, None)
        if tmp and os.path.isdir(tmp):
            try:
                import shutil
                shutil.rmtree(tmp, ignore_errors=True)
            except Exception:
                pass


# ----------------------------------------------------------------------
# 生成（异步）
# ----------------------------------------------------------------------
async def generate_agent(requirement: str, max_rounds: int = 2, model: str = None,
                      think: bool = False, provider: str = None,
                      context_paths: list = None, use_history: bool = False) -> dict:
    ctx = _available_context()
    extra = _context_block(context_paths)
    if use_history:
        extra += _history_block("agent")
    base_user = (
        "可用大脑: %s\n可用插件: %s\n\n需求: %s%s"
        % (ctx["brains"], [p["name"] for p in ctx["plugins"]], requirement, extra)
    )
    last_err = ""
    last_raw = None
    for i in range(max_rounds):
        user = base_user if i == 0 else (
            "上一版 JSON 未能通过校验，错误：%s\n请修正后重新输出完整 JSON。需求不变：%s%s"
            % (last_err, requirement, extra)
        )
        try:
            data = await _llm_json(AGENT_SYSTEM, user, model=model, think=think, provider=provider)
        except Exception as e:
            return {"ok": False, "error": "生成失败: %r" % e}
        last_raw = data
        norm, err = _normalize_agent(data)
        if err:
            last_err = err
            continue
        _record_history("generate", "agent", requirement, True, norm,
                        rounds=i + 1, model=model, provider=provider)
        return {"ok": True, "mode": "agent", "data": norm, "rounds": i + 1}
    _record_history("generate", "agent", requirement, False, None,
                    rounds=max_rounds, model=model, provider=provider, error=last_err)
    return {"ok": False, "error": "多次修正仍未通过校验（最后错误：%s）" % last_err, "raw": last_raw}


async def generate_plugin(requirement: str, kind: str = "", max_rounds: int = 3,
                       model: str = None, think: bool = False, provider: str = None,
                       context_paths: list = None, use_history: bool = False) -> dict:
    kind = (kind or "").strip().lower()
    system = PLUGIN_SYSTEM
    if kind:
        system += "\n本次要求 kind 必须为: %s" % kind
    extra = _context_block(context_paths)
    if use_history:
        extra += _history_block("plugin")
    last_err = ""
    last_raw = None
    for i in range(max_rounds):
        if i == 0:
            user = "需求: %s%s" % (requirement, extra)
        else:
            user = (
                "你上一版代码没能通过系统校验，错误信息如下：\n%s\n\n"
                "请修正后重新输出**完整**的 JSON（结构不变），务必解决上面的错误。\n"
                "需求不变：%s%s" % (last_err, requirement, extra)
            )
        try:
            data = await _llm_json(system, user, model=model, think=think, provider=provider)
        except Exception as e:
            return {"ok": False, "error": "生成失败: %r" % e}
        last_raw = data
        norm, err = _normalize_plugin(data)
        if err:
            last_err = "规范化错误: %s" % err
            continue
        if kind and norm["kind"] != kind:
            last_err = "kind 不符：要求 %s，实得 %s" % (kind, norm["kind"])
            continue
        # 真导入 + 实例化校验（干加载），失败则把错误回喂 LLM 改写
        dl = _dry_load_plugin(norm["code"], norm["kind"], norm["name"])
        if dl:
            last_err = dl
            continue
        _record_history("generate", "plugin", requirement, True, norm,
                        rounds=i + 1, model=model, provider=provider)
        return {"ok": True, "mode": "plugin", "data": norm, "rounds": i + 1}
    _record_history("generate", "plugin", requirement, False, None,
                    rounds=max_rounds, model=model, provider=provider, error=last_err)
    return {"ok": False, "error": "经过 %d 次修正仍未通过校验（最后错误：%s）" % (max_rounds, last_err), "raw": last_raw}


async def improve_agent(agent_id: str, instruction: str, model: str = None,
                      think: bool = False, provider: str = None,
                      context_paths: list = None, use_history: bool = False) -> dict:
    """读取磁盘上已有的智能体，按指令改进后输出新版（不立即落盘）。"""
    from agent_manager import load_agent as am_load
    cur = am_load(agent_id)
    if not cur:
        return {"ok": False, "error": "找不到智能体: %s" % agent_id}
    extra = _context_block(context_paths)
    if use_history:
        extra += _history_block("agent", exclude_key=agent_id)
    cur_s = json.dumps(cur, ensure_ascii=False, indent=2)
    user = (
        "以下是已有的智能体定义（agent.json）：\n```json\n%s\n```\n\n"
        "改进要求：%s\n\n"
        "请基于已有定义进行改进，输出**完整**的新版 JSON（结构同上，不要保留明显缺陷）。%s"
        % (cur_s, instruction or "整体优化", extra)
    )
    last_err = ""
    last_raw = None
    for i in range(2):
        u = user if i == 0 else (
            "上一版未通过校验，错误：%s\n请修正后重新输出完整 JSON。\n%s" % (last_err, user)
        )
        try:
            data = await _llm_json(AGENT_SYSTEM, u, model=model, think=think, provider=provider)
        except Exception as e:
            return {"ok": False, "error": "改进失败: %r" % e}
        last_raw = data
        norm, err = _normalize_agent(data)
        if err:
            last_err = err
            continue
        _record_history("improve", "agent", instruction, True, norm,
                        rounds=i + 1, model=model, provider=provider, key=agent_id)
        return {"ok": True, "mode": "agent", "data": norm, "improved_from": agent_id, "rounds": i + 1}
    _record_history("improve", "agent", instruction, False, None, rounds=2,
                    model=model, provider=provider, key=agent_id, error=last_err)
    return {"ok": False, "error": "改进后仍未通过校验（最后错误：%s）" % last_err, "raw": last_raw}


async def improve_plugin(name: str, instruction: str, model: str = None,
                       think: bool = False, provider: str = None,
                       context_paths: list = None, use_history: bool = False) -> dict:
    """读取磁盘上已有的插件包，按指令改进后输出新版（不立即落盘）。"""
    pdir = os.path.join(PLUGINS_DIR, name)
    mpath = os.path.join(pdir, "manifest.json")
    cpath = os.path.join(pdir, "plugin.py")
    if not (os.path.isfile(mpath) and os.path.isfile(cpath)):
        return {"ok": False, "error": "找不到插件包: %s" % name}
    try:
        with open(mpath, "r", encoding="utf-8") as f:
            manifest = json.load(f)
        with open(cpath, "r", encoding="utf-8") as f:
            code = f.read()
    except Exception as e:
        return {"ok": False, "error": "读取插件失败: %r" % e}
    kind = (manifest.get("kind") or "").strip().lower()
    system = PLUGIN_SYSTEM
    if kind:
        system += "\n本次要求 kind 必须为: %s" % kind
    extra = _context_block(context_paths)
    if use_history:
        extra += _history_block("plugin", exclude_key=name)
    user = (
        "以下是已有的插件包：\nmanifest.json:\n```json\n%s\n```\n\nplugin.py:\n```python\n%s\n```\n\n"
        "改进要求：%s\n\n请输出**完整**的新版 JSON（结构同生成插件，含 manifest 与 code），"
        "保持插件契约不变，只做改进。%s"
        % (json.dumps(manifest, ensure_ascii=False, indent=2), code, instruction or "整体优化", extra)
    )
    last_err = ""
    last_raw = None
    for i in range(3):
        u = user if i == 0 else (
            "上一版未通过校验，错误：%s\n请修正后重新输出完整 JSON。\n%s" % (last_err, user)
        )
        try:
            data = await _llm_json(system, u, model=model, think=think, provider=provider)
        except Exception as e:
            return {"ok": False, "error": "改进失败: %r" % e}
        last_raw = data
        norm, err = _normalize_plugin(data)
        if err:
            last_err = "规范化错误: %s" % err
            continue
        if kind and norm["kind"] != kind:
            last_err = "kind 不符：要求 %s，实得 %s" % (kind, norm["kind"])
            continue
        dl = _dry_load_plugin(norm["code"], norm["kind"], norm["name"])
        if dl:
            last_err = dl
            continue
        _record_history("improve", "plugin", instruction, True, norm,
                        rounds=i + 1, model=model, provider=provider, key=name)
        return {"ok": True, "mode": "plugin", "data": norm, "improved_from": name, "rounds": i + 1}
    _record_history("improve", "plugin", instruction, False, None, rounds=3,
                    model=model, provider=provider, key=name, error=last_err)
    return {"ok": False, "error": "改进后仍未通过校验（最后错误：%s）" % last_err, "raw": last_raw}


# ----------------------------------------------------------------------
# 落盘（异步）
# ----------------------------------------------------------------------
async def save_agent(bridge, data: dict) -> dict:
    norm, err = _normalize_agent(data)
    if err:
        return {"ok": False, "error": err}
    try:
        from agent_manager import save_agent as am_save
    except Exception as e:
        return {"ok": False, "error": "无法导入 agent_manager: %r" % e}
    slug = re.sub(r"[^a-z0-9]+", "_", norm["name"].lower()).strip("_") or "agent"
    aid = ("%s_%d" % (slug, int(time.time() * 1000)))[-40:]
    r = am_save(aid, norm)
    if "error" in r:
        return {"ok": False, "error": r["error"]}
    return {"ok": True, "id": aid, "agent": r.get("agent")}


def import_agent_sync(bridge, agent_def: dict) -> dict:
    """导入本地智能体配置（agent.json）并注册。"""
    from agent_manager import save_agent as am_save
    if not isinstance(agent_def, dict):
        return {"ok": False, "error": "agent 需为 JSON 对象"}
    if not str(agent_def.get("id", "")).strip():
        agent_def["id"] = "agent_" + time.strftime("%Y%m%d%H%M%S")
    if not str(agent_def.get("name", "")).strip():
        agent_def["name"] = agent_def["id"]
    try:
        ar = am_save(agent_def)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    if "error" in ar:
        return {"ok": False, "error": ar["error"]}
    return {"ok": True, "id": ar.get("id") or agent_def["id"], "name": agent_def.get("name")}


def connect_external_agent_sync(bridge, payload: dict) -> dict:
    """接入外部 API 智能体（OpenAI 兼容端点）：注册为模型供应商 + 智能体。"""
    from bridge import provider_api
    from agent_manager import save_agent as am_save

    name = str(payload.get("name") or "").strip()
    base_url = str(payload.get("base_url") or "").strip()
    api_key = payload.get("api_key") or ""
    model = str(payload.get("model") or "").strip()
    if not name or not base_url or not model:
        return {"ok": False, "error": "名称 / BaseURL / 模型 均为必填"}

    caps = payload.get("capabilities") or ["chat"]
    if isinstance(caps, str):
        caps = [caps]
    caps = [c for c in caps if c in ("chat", "reasoning", "vision", "role")]
    if not caps:
        caps = ["chat"]

    provider_name = "ext_" + (re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "agent")
    r = provider_api.save_model({
        "name": provider_name, "preset": "", "api_key": api_key,
        "base_url": base_url, "model": model, "api_style": "openai",
        "capabilities": caps,
    })
    if not r.get("ok"):
        return {"ok": False, "error": r.get("error", "注册供应商失败")}

    agent_def = {
        "id": provider_name,
        "name": name,
        "emoji": payload.get("emoji") or "🔌",
        "system_prompt": payload.get("system_prompt") or "",
        "profile": payload.get("profile") or {},
        "model": {"provider": provider_name, "model": model},
        "enabled_brains": [], "enabled_plugins": [], "bindings": [], "auto_start": False,
    }
    try:
        ar = am_save(agent_def)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    if "error" in ar:
        return {"ok": False, "error": ar["error"]}
    return {"ok": True, "id": ar.get("id") or provider_name, "name": name, "provider": provider_name}


async def save_plugin(bridge, name: str, manifest: dict, code: str) -> dict:
    payload = {"manifest": manifest, "code": code}
    norm, err = _normalize_plugin(payload)
    if err:
        return {"ok": False, "error": err}
    name = norm["name"]
    kind = norm["kind"]
    err2 = _check_plugin_code(norm["code"], kind)
    if err2:
        return {"ok": False, "error": err2}
    err3 = _dry_load_plugin(norm["code"], kind, name)
    if err3:
        return {"ok": False, "error": "干加载校验未通过: " + err3}
    target = os.path.join(PLUGINS_DIR, name)
    if os.path.exists(target):
        return {"ok": False, "error": "插件目录已存在: %s（请换名或先删除）" % name}
    os.makedirs(target, exist_ok=True)
    man = {
        "name": name,
        "title": norm["title"],
        "description": norm["description"],
        "kind": kind,
        "version": norm["version"],
        "config_schema": norm["config_schema"],
    }
    if norm["author"]:
        man["author"] = norm["author"]
    try:
        with open(os.path.join(target, "manifest.json"), "w", encoding="utf-8") as f:
            json.dump(man, f, ensure_ascii=False, indent=2)
        with open(os.path.join(target, "plugin.py"), "w", encoding="utf-8") as f:
            f.write(norm["code"])
    except Exception as e:
        return {"ok": False, "error": "写入失败: %r" % e}
    try:
        import plugins_api
        plugins_api.rescan(bridge)
    except Exception:
        pass
    return {"ok": True, "name": name, "dir": target}


# ----------------------------------------------------------------------
# 同步包装（供 server.py 在 BaseHTTPRequestHandler 中调用）
# ----------------------------------------------------------------------
def generate_agent_sync(bridge, requirement: str, model: str = None, think: bool = False,
                         provider: str = None, context_paths: list = None,
                         use_history: bool = False) -> dict:
    return bridge.lt.run_coro(
        generate_agent(requirement, max_rounds=2, model=model, think=think, provider=provider,
                       context_paths=context_paths, use_history=use_history),
        timeout=120)


def generate_plugin_sync(bridge, requirement: str, kind: str = "", model: str = None,
                         think: bool = False, provider: str = None, context_paths: list = None,
                         use_history: bool = False) -> dict:
    return bridge.lt.run_coro(
        generate_plugin(requirement, kind, max_rounds=3, model=model, think=think, provider=provider,
                        context_paths=context_paths, use_history=use_history),
        timeout=300)


def list_context_files_sync() -> dict:
    return list_context_files()


def read_context_file_sync(rel: str) -> dict:
    return read_context_file(rel)


def get_history_sync(limit: int = 60) -> dict:
    return get_history(limit)


def improve_agent_sync(bridge, agent_id: str, instruction: str, model: str = None,
                       think: bool = False, provider: str = None, context_paths: list = None,
                       use_history: bool = False) -> dict:
    return bridge.lt.run_coro(
        improve_agent(agent_id, instruction, model=model, think=think, provider=provider,
                      context_paths=context_paths, use_history=use_history),
        timeout=180)


def improve_plugin_sync(bridge, name: str, instruction: str, model: str = None,
                        think: bool = False, provider: str = None, context_paths: list = None,
                        use_history: bool = False) -> dict:
    return bridge.lt.run_coro(
        improve_plugin(name, instruction, model=model, think=think, provider=provider,
                       context_paths=context_paths, use_history=use_history),
        timeout=300)


def save_agent_sync(bridge, data: dict) -> dict:
    r = bridge.lt.run_coro(save_agent(bridge, data or {}), timeout=60)
    if r.get("ok"):
        _record_history("save", "agent", data.get("name") or r.get("id"),
                        True, None, rounds=1, key=r.get("id"))
    return r


def save_plugin_sync(bridge, payload: dict) -> dict:
    payload = payload or {}
    r = bridge.lt.run_coro(
        save_plugin(bridge, payload.get("name", ""), payload.get("manifest") or {}, payload.get("code") or ""),
        timeout=60,
    )
    if r.get("ok"):
        _record_history("save", "plugin", r.get("name"), True, None, rounds=1, key=r.get("name"))
    return r


# ============================================================================
# 工作区读写：让构建助手像代码 Agent 一样直接读 / 写工作区源码文件
# ============================================================================
# 安全根白名单：写文件 / 删文件只能落在这些目录及其子目录内。
_WORKSPACE_ROOTS = (
    "plugins",                  # 插件包根目录（plugins/<pkg>/manifest.json + plugin.py）
    "agents",                   # 智能体根（若某天改放仓库内）
    "bridge",                   # 桥接层源码
    "libs",                     # 公共库（含 libs/qq_bot_runtime/agents/）
    "webui",                    # 前端静态资源
    "config",                   # 配置目录（若存在）
)

# 危险文件 / 隐藏目录黑名单：绝对不允许读写
_WORKSPACE_DENY = (
    ".git", ".codebuddy", "__pycache__", "node_modules",
    ".env", ".env.local", "settings_store.py",   # 包含运行时密钥的入口
    "ai_providers.json", "user_profiles.json",   # 运行时敏感数据
    "data",                                     # 运行期污染目录
)
# 文件大小上限（防止一次性写超大文件 / 读取触发 OOM）
_MAX_FILE_BYTES = 512 * 1024


def _resolve_rooted(rel: str) -> str:
    """解析相对工作区路径到绝对路径；越界 / 黑名单一律拒绝。"""
    rel = (rel or "").replace("\\", "/").lstrip("/")
    if not rel:
        raise ValueError("路径为空")
    # 路径中任一段不允许落在黑名单内
    parts = rel.split("/")
    if any(p in _WORKSPACE_DENY for p in parts):
        raise ValueError(f"禁止访问: {rel}")
    # 必须落在某个允许的根之内
    if not any(rel == root or rel.startswith(root + "/") for root in _WORKSPACE_ROOTS):
        raise ValueError(f"路径必须在允许的根内 ({'/'.join(_WORKSPACE_ROOTS)}): {rel}")
    abs_path = os.path.join(APP_DIR, rel)
    abs_path = os.path.abspath(abs_path)
    # 二次校验：解析后仍必须在 APP_DIR 之下
    app_abs = os.path.realpath(APP_DIR)
    real = os.path.realpath(abs_path)
    if not (real == app_abs or real.startswith(app_abs + os.sep)):
        raise ValueError(f"路径越界: {rel}")
    return abs_path


def _is_text_file(abs_path: str) -> bool:
    """简单判定是否文本文件，避免写入二进制（图片 / 模型权重）触发乱码。"""
    try:
        with open(abs_path, "rb") as f:
            chunk = f.read(2048)
    except Exception:
        return True
    if not chunk:
        return True
    # NUL 字节 = 二进制特征
    if b"\x00" in chunk:
        return False
    return True


def list_workspace_sync(rel_dir: str = "") -> dict:
    """列出工作区子目录。rel_dir 必须在白名单根之下；空字符串=根视图（直接列根目录下的允许项）。"""
    rel_dir = (rel_dir or "").replace("\\", "/").strip("/")
    if rel_dir and not any(rel_dir == r or rel_dir.startswith(r + "/") for r in _WORKSPACE_ROOTS):
        return {"ok": False, "error": f"路径必须在白名单根内: {rel_dir}"}
    base = os.path.join(APP_DIR, rel_dir) if rel_dir else APP_DIR
    base = os.path.abspath(base)
    if not os.path.isdir(base):
        return {"ok": False, "error": f"目录不存在: {rel_dir or '根'}"}
    out = []
    try:
        for fn in sorted(os.listdir(base)):
            full = os.path.join(base, fn)
            rel = (os.path.relpath(full, APP_DIR)).replace("\\", "/")
            if any(part in _WORKSPACE_DENY for part in rel.split("/")):
                continue
            # 仅展示白名单根下的项
            if rel_dir == "":
                if fn not in _WORKSPACE_ROOTS and not fn.endswith((".md", ".txt", ".py", ".json", ".html", ".js", ".css")):
                    # 根视图只展示白名单根 + 顶层关键文件
                    if fn not in _WORKSPACE_ROOTS:
                        continue
            try:
                stat = os.stat(full)
                entry = {
                    "name": fn,
                    "rel": rel,
                    "is_dir": os.path.isdir(full),
                    "size": stat.st_size if not os.path.isdir(full) else None,
                    "mtime": int(stat.st_mtime),
                }
                out.append(entry)
            except OSError:
                continue
    except Exception as e:
        return {"ok": False, "error": f"读取失败: {e!r}"}
    return {"ok": True, "dir": rel_dir, "items": out}


def read_workspace_sync(rel_path: str) -> dict:
    """读工作区单个文件，返回内容（文本）。"""
    try:
        abs_path = _resolve_rooted(rel_path)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    if not os.path.isfile(abs_path):
        return {"ok": False, "error": f"不是文件: {rel_path}"}
    if not _is_text_file(abs_path):
        return {"ok": False, "error": "二进制文件不允许读取/写入（防止破坏资源）"}
    try:
        size = os.path.getsize(abs_path)
        if size > _MAX_FILE_BYTES:
            return {"ok": False, "error": f"文件过大 ({size} bytes > {_MAX_FILE_BYTES})"}
        with open(abs_path, "r", encoding="utf-8") as f:
            content = f.read()
        return {"ok": True, "path": rel_path, "content": content, "size": size}
    except UnicodeDecodeError:
        return {"ok": False, "error": "非 UTF-8 文本，请用其他工具读取"}
    except Exception as e:
        return {"ok": False, "error": f"读取失败: {e!r}"}


def _make_backup(abs_path: str, rel: str) -> str | None:
    """写入前自动备份到 .builder_bak/<rel-with-slashes>/<filename>.<timestamp>，最近 20 个。"""
    bak_root = os.path.join(APP_DIR, "data", "builder_bak")
    bak_path = os.path.join(bak_root, rel.replace("/", os.sep))
    os.makedirs(os.path.dirname(bak_path), exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    target = f"{bak_path}.{ts}"
    if os.path.isfile(abs_path):
        try:
            shutil.copy2(abs_path, target)
        except Exception:
            return None
    # 每个文件最多保留 20 个备份
    parent = os.path.dirname(bak_path)
    prefix = os.path.basename(bak_path)
    if os.path.isdir(parent):
        siblings = sorted(
            (os.path.join(parent, f) for f in os.listdir(parent) if f.startswith(prefix)),
            key=os.path.getmtime, reverse=True,
        )
        for old in siblings[20:]:
            try:
                os.remove(old)
            except OSError:
                pass
    return target if os.path.isfile(target) else None


def write_workspace_sync(rel_path: str, content: str, *, backup: bool = True) -> dict:
    """写工作区单个文件。自动备份原文件到 data/builder_bak/<rel>.<ts>。"""
    try:
        abs_path = _resolve_rooted(rel_path)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    if not _is_text_file(abs_path) if os.path.exists(abs_path) else False:
        return {"ok": False, "error": "目标路径是二进制文件，禁止写入"}
    if isinstance(content, str) and len(content.encode("utf-8")) > _MAX_FILE_BYTES:
        return {"ok": False, "error": f"内容过大 (> {_MAX_FILE_BYTES} bytes)"}
    bak = None
    if backup and os.path.isfile(abs_path):
        bak = _make_backup(abs_path, rel_path)
    try:
        os.makedirs(os.path.dirname(abs_path), exist_ok=True)
        with open(abs_path, "w", encoding="utf-8") as f:
            f.write(content)
        size = os.path.getsize(abs_path)
        return {"ok": True, "path": rel_path, "size": size, "backup": bak}
    except Exception as e:
        return {"ok": False, "error": f"写入失败: {e!r}"}


def workspace_diff_sync(rel_path: str, content: str, *, max_lines: int = 200) -> dict:
    """把待写入的内容与磁盘现状做 diff（仅返回前 N 行），便于 UI 展示修改前后的对比。"""
    cur = read_workspace_sync(rel_path)
    if not cur.get("ok"):
        return cur
    old = cur.get("content", "").splitlines()
    new = (content or "").splitlines()
    import difflib
    diff = list(difflib.unified_diff(old, new, fromfile=f"a/{rel_path}", tofile=f"b/{rel_path}", lineterm=""))
    if len(diff) > max_lines:
        truncated = diff[:max_lines] + [f"... 省略 {len(diff) - max_lines} 行 ..."]
    else:
        truncated = diff
    return {"ok": True, "path": rel_path, "diff": truncated, "old_size": len(old), "new_size": len(new)}


def list_plugin_files_sync(name: str) -> dict:
    """列出一个插件包（plugins/<name>/）下所有相对路径文件，方便 UI 一键选择要改的文件。"""
    name = (name or "").strip().strip("/").replace("\\", "/")
    if not name or "/" in name or name.startswith("."):
        return {"ok": False, "error": "非法插件名"}
    pkg_dir = os.path.join(APP_DIR, "plugins", name)
    pkg_dir = os.path.abspath(pkg_dir)
    app_abs = os.path.realpath(APP_DIR)
    if not (pkg_dir == app_abs or pkg_dir.startswith(app_abs + os.sep)):
        return {"ok": False, "error": "路径越界"}
    if not os.path.isdir(pkg_dir):
        return {"ok": False, "error": f"插件目录不存在: plugins/{name}"}
    out = []
    for root, dirs, files in os.walk(pkg_dir):
        # 跳过隐藏 / 缓存
        dirs[:] = [d for d in dirs if not d.startswith((".", "_", "__pycache__"))]
        for fn in files:
            full = os.path.join(root, fn)
            rel = os.path.relpath(full, pkg_dir).replace("\\", "/")
            try:
                size = os.path.getsize(full)
            except OSError:
                size = 0
            out.append({"rel": rel, "size": size, "text": _is_text_file(full)})
    out.sort(key=lambda x: x["rel"])
    return {"ok": True, "name": name, "files": out}


# ============================================================================
# 对话式构建 Agent（类 Codex / WorkBuddy：一个对话框 + 工具调用 + 会话历史）
# ============================================================================
# 设计：把上面所有能力（读文件 / 写文件 / diff / 生成 / 改进 / 保存）包装成 LLM 工具，
# 在一个多轮 tool-calling 循环里由模型自己决定调用顺序，实现
# 「说需求 → 自己看代码 → 自己改 → 自己查语法 → 自报结果」的闭环。
CHAT_DIR = os.path.join(DATA_DIR, "builder_chat")
CHAT_MAX_STEPS = 14                 # 单轮最多工具步数（防死循环）
CHAT_TOOL_RESULT_MAX = 6000          # 单条工具结果回喂模型的最大字符数
CHAT_SEARCH_MAX = 40                 # search_code 最多返回条数

CHAT_SYSTEM = """你是「构建助手」——肥鱼娘桌面 App 内置的代码 / 构建 Agent（类似 Codex、WorkBuddy）。
你可以通过工具读写用户工作区源码、生成或改进智能体与插件。

可用能力：
- 列目录 / 读文件 / 写文件（写前自动备份到 data/builder_bak/）/ 查看 diff / 全库搜索 / 语法检查
- 生成智能体（agent.json）与插件包（manifest.json + plugin.py）；改进已有智能体 / 插件
- 查看当前已有的智能体与插件列表

工作准则：
1. 修改代码前先 read_file 看清现状，不要凭空猜文件名或字段名。
2. 写完 Python 代码必须调 check_syntax 确认语法通过；报错就继续改，直到通过。
3. write_file 会自动返回 diff，请核对改动是否符合预期。
4. 生成类工具（generate_agent / generate_plugin）只产出**草稿**，会展示给用户点保存；
   只有用户明确说「保存 / 落盘 / 装上」时才调用 save_agent / save_plugin。
5. 改已有的插件/智能体优先用 improve_plugin / improve_agent，不要从零重写。
6. 一次只做用户要求的事，不要顺手改无关文件。
7. 回复用中文、简短：先说做了什么（含改动的文件路径），再说下一步建议。
8. 无法完成时直说原因，不要假装成功。"""


def _tool(name: str, desc: str, props: dict, required: list) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": desc,
        "parameters": {"type": "object", "properties": props, "required": required},
    }}


def builder_tools() -> list:
    S = {"type": "string"}
    return [
        _tool("list_dir", "列出工作区目录内容（相对路径，空字符串=根）。",
              {"dir": S}, ["dir"]),
        _tool("read_file", "读取工作区某个文本文件的内容。",
              {"path": S}, ["path"]),
        _tool("write_file", "写入（覆盖）工作区文本文件，写入前自动备份，返回 diff。",
              {"path": S, "content": S}, ["path", "content"]),
        _tool("diff_file", "对比「磁盘现状」与「将要写入的内容」，返回 unified diff 文本。",
              {"path": S, "content": S}, ["path", "content"]),
        _tool("search_code", "在工作区源码里全文搜索关键字，返回 文件:行号:内容。",
              {"query": S, "limit": {"type": "integer"}}, ["query"]),
        _tool("check_syntax", "检查 Python 代码语法（可传 content 检查待写入内容，或只传 path 检查磁盘文件）。",
              {"path": S, "content": S}, []),
        _tool("list_agents", "列出当前已存在的智能体（id / 名称）。", {}, []),
        _tool("list_plugins", "列出当前已存在的插件包（名称 / 类型 / 说明）。", {}, []),
        _tool("list_history", "查看最近的构建历史（生成/改进/保存记录）。",
              {"limit": {"type": "integer"}}, []),
        _tool("generate_agent", "根据需求生成一个智能体定义草稿（不落盘）。",
              {"requirement": S}, ["requirement"]),
        _tool("generate_plugin", "根据需求生成一个插件包草稿（manifest + plugin.py，不落盘）。",
              {"requirement": S, "kind": S}, ["requirement"]),
        _tool("improve_agent", "读取磁盘上已有智能体并按指令改进，产出新版草稿。",
              {"id": S, "instruction": S}, ["id", "instruction"]),
        _tool("improve_plugin", "读取磁盘上已有插件包并按指令改进，产出新版草稿。",
              {"name": S, "instruction": S}, ["name", "instruction"]),
        _tool("save_agent", "把智能体草稿落盘注册（不传 data 时保存最近一次生成/改进的草稿）。",
              {"data": {"type": "object"}}, []),
        _tool("save_plugin", "把插件包草稿落盘到 plugins/<name>/（不传参时保存最近一次草稿）。",
              {}, []),
    ]


def _check_syntax(path: str = "", content: str = None) -> dict:
    """语法检查：优先检查传入内容，否则检查磁盘文件。"""
    if content is None:
        if not path:
            return {"ok": False, "error": "需要 path 或 content"}
        r = read_workspace_sync(path)
        if not r.get("ok"):
            return r
        content = r.get("content", "")
    if not path.endswith(".py"):
        return {"ok": False, "error": "只能检查 .py 文件语法"}
    with tempfile.TemporaryDirectory() as td:
        f = os.path.join(td, "check.py")
        try:
            with open(f, "w", encoding="utf-8") as fh:
                fh.write(content)
            py_compile.compile(f, doraise=True)
            return {"ok": True, "path": path, "message": "语法检查通过"}
        except py_compile.PyCompileError as e:
            return {"ok": False, "path": path, "error": str(e)[:1500]}
        except Exception as e:
            return {"ok": False, "path": path, "error": "检查失败: %r" % e}


def _search_workspace(query: str, limit: int = CHAT_SEARCH_MAX) -> dict:
    """在工作区白名单根下做全文搜索（跳过黑名单/二进制/超大文件）。"""
    q = (query or "").strip()
    if not q:
        return {"ok": False, "error": "关键字为空"}
    limit = max(1, min(int(limit or CHAT_SEARCH_MAX), 200))
    hits = []
    scanned = 0
    for root in ("plugins", "bridge", "webui", "agents", "config"):
        base = os.path.join(APP_DIR, root)
        if not os.path.isdir(base):
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in _WORKSPACE_DENY
                           and not d.startswith(".") and d != "__pycache__"]
            for fn in filenames:
                if fn.startswith(".") or not fn.endswith(
                        (".py", ".json", ".js", ".css", ".html", ".md", ".txt", ".yml", ".yaml", ".bat")):
                    continue
                fp = os.path.join(dirpath, fn)
                try:
                    if os.path.getsize(fp) > _MAX_FILE_BYTES:
                        continue
                except OSError:
                    continue
                scanned += 1
                try:
                    with open(fp, "r", encoding="utf-8", errors="replace") as f:
                        for i, line in enumerate(f, 1):
                            if q in line:
                                rel = os.path.relpath(fp, APP_DIR).replace("\\", "/")
                                hits.append({"path": rel, "line": i, "text": line.strip()[:200]})
                                if len(hits) >= limit:
                                    return {"ok": True, "query": q, "scanned": scanned,
                                            "truncated": True, "hits": hits}
                except Exception:
                    continue
    return {"ok": True, "query": q, "scanned": scanned, "truncated": False, "hits": hits}


def _list_plugins_brief() -> dict:
    out = []
    if os.path.isdir(PLUGINS_DIR):
        for name in sorted(os.listdir(PLUGINS_DIR)):
            d = os.path.join(PLUGINS_DIR, name)
            if not os.path.isdir(d):
                continue
            man = {}
            mf = os.path.join(d, "manifest.json")
            if os.path.isfile(mf):
                try:
                    with open(mf, "r", encoding="utf-8") as f:
                        man = json.load(f)
                except Exception:
                    man = {}
            out.append({"name": man.get("name") or name, "kind": man.get("kind") or "?",
                        "title": man.get("title") or "", "description": (man.get("description") or "")[:120]})
    return {"ok": True, "plugins": out}


async def _exec_tool(bridge, name: str, args: dict, state: dict) -> dict:
    """执行一个构建工具调用，返回 JSON 结果（同时供 UI 展示）。"""
    a = args if isinstance(args, dict) else {}
    llm = state.get("_llm", {})
    m, th, pv = llm.get("model"), llm.get("think"), llm.get("provider")
    ctx = state.get("ctx") or None
    use_hist = bool(state.get("use_history"))

    if name == "list_dir":
        return list_workspace_sync(a.get("dir", "") or "")
    if name == "read_file":
        return read_workspace_sync(a.get("path", ""))
    if name == "diff_file":
        return workspace_diff_sync(a.get("path", ""), a.get("content", "") or "")
    if name == "search_code":
        return _search_workspace(a.get("query", ""), a.get("limit", CHAT_SEARCH_MAX))
    if name == "check_syntax":
        return _check_syntax(a.get("path", ""), a.get("content"))
    if name == "list_agents":
        try:
            import agent_manager
            return {"ok": True, "agents": [{"id": x.get("id"), "name": x.get("name")}
                                           for x in agent_manager.list_agents()]}
        except Exception as e:
            return {"ok": False, "error": "读取智能体列表失败: %r" % e}
    if name == "list_plugins":
        return _list_plugins_brief()
    if name == "list_history":
        return get_history(limit=int(a.get("limit", 20) or 20))
    if name == "write_file":
        path, content = a.get("path", ""), a.get("content", "") or ""
        d = workspace_diff_sync(path, content)
        r = write_workspace_sync(path, content, backup=True)
        if r.get("ok"):
            r["diff"] = (d.get("diff") or [])[:120]
            r["old_size"], r["new_size"] = d.get("old_size"), d.get("new_size")
        return r
    # ---- 生成 / 改进（产物进草稿，用户或 save_* 才落盘） ----
    if name == "generate_agent":
        r = await generate_agent(a.get("requirement", ""), model=m, think=th, provider=pv,
                                 context_paths=ctx, use_history=use_hist)
        if r.get("ok"):
            state.setdefault("drafts", {})["agent"] = r["data"]
            r = {"ok": True, "mode": "agent", "rounds": r.get("rounds"),
                 "draft": r["data"], "note": "草稿已生成，等待用户确认保存"}
        return r
    if name == "generate_plugin":
        r = await generate_plugin(a.get("requirement", ""), a.get("kind", "") or "",
                                  model=m, think=th, provider=pv,
                                  context_paths=ctx, use_history=use_hist)
        if r.get("ok"):
            state.setdefault("drafts", {})["plugin"] = r["data"]
            r = {"ok": True, "mode": "plugin", "rounds": r.get("rounds"),
                 "draft": r["data"], "note": "草稿已生成，等待用户确认保存"}
        return r
    if name == "improve_agent":
        r = await improve_agent(a.get("id", ""), a.get("instruction", "") or "",
                                model=m, think=th, provider=pv,
                                context_paths=ctx, use_history=use_hist)
        if r.get("ok"):
            state.setdefault("drafts", {})["agent"] = r["data"]
        return r
    if name == "improve_plugin":
        r = await improve_plugin(a.get("name", ""), a.get("instruction", "") or "",
                                 model=m, think=th, provider=pv,
                                 context_paths=ctx, use_history=use_hist)
        if r.get("ok"):
            state.setdefault("drafts", {})["plugin"] = r["data"]
        return r
    if name == "save_agent":
        data = a.get("data") or (state.get("drafts") or {}).get("agent") or {}
        if not data:
            return {"ok": False, "error": "没有可保存的智能体草稿，请先生成"}
        return await save_agent(bridge, data)
    if name == "save_plugin":
        d = (state.get("drafts") or {}).get("plugin") or {}
        if not d or not d.get("name"):
            return {"ok": False, "error": "没有可保存的插件草稿，请先生成"}
        return await save_plugin(bridge, d.get("name"), d.get("manifest") or d, d.get("code") or "")
    return {"ok": False, "error": "未知工具: %s" % name}


# ---------------- 会话存取 ----------------
def _safe_sid(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_\-]", "", (s or ""))[:64]


def _chat_path(sid: str) -> str:
    return os.path.join(CHAT_DIR, _safe_sid(sid) + ".json")


def save_chat_session(state: dict) -> None:
    sid = _safe_sid(state.get("id") or "")
    if not sid:
        return
    try:
        os.makedirs(CHAT_DIR, exist_ok=True)
        out = {k: v for k, v in state.items() if not k.startswith("_")}
        out["id"] = sid
        out["updated"] = time.time()
        tmp = _chat_path(sid) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        os.replace(tmp, _chat_path(sid))
    except Exception:
        pass


def load_chat_session(sid: str) -> dict:
    p = _chat_path(sid)
    if not os.path.isfile(p):
        return {}
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def list_chat_sessions() -> dict:
    out = []
    if os.path.isdir(CHAT_DIR):
        for fn in os.listdir(CHAT_DIR):
            if not fn.endswith(".json"):
                continue
            try:
                with open(os.path.join(CHAT_DIR, fn), "r", encoding="utf-8") as f:
                    d = json.load(f)
            except Exception:
                continue
            out.append({"id": d.get("id") or fn[:-5], "title": d.get("title") or "",
                        "updated": d.get("updated") or 0,
                        "count": len(d.get("messages") or [])})
    out.sort(key=lambda x: x.get("updated", 0), reverse=True)
    return {"ok": True, "sessions": out}


def delete_chat_session(sid: str) -> dict:
    p = _chat_path(sid)
    try:
        if os.path.isfile(p):
            os.remove(p)
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": "%r" % e}


def new_chat_session(title: str = "") -> dict:
    sid = "chat_" + time.strftime("%Y%m%d_%H%M%S")
    state = {"id": sid, "title": title or "新会话", "created": time.time(),
             "messages": [], "drafts": {}}
    save_chat_session(state)
    return {"ok": True, "session": sid, "state": state}


# ---------------- 主循环 ----------------
async def _chat_with_tools(llm, convo: list, model=None, think=False, provider=None) -> dict:
    """带工具调用的对话。

    多数部署只配了 chat / reasoning / vision 能力路由（没有单独的 tools 路由），
    而工具调用本身仍是该供应商的 chat 接口能力，故 tools 路由不可用时回退到 chat。
    """
    try:
        return await llm.chat(convo, capability="tools", tools=builder_tools(),
                              model=model, think=think, provider=provider, timeout=300)
    except Exception as e:
        msg = str(e)
        if "tools" in msg or "无可用供应商" in msg or "不支持能力" in msg:
            return await llm.chat(convo, capability="chat", tools=builder_tools(),
                                  model=model, think=think, provider=provider, timeout=300)
        raise


async def run_chat(bridge, session_id: str = "", message: str = "", model: str = None,
                   think: bool = False, provider: str = None, context_paths: list = None,
                   use_history: bool = False, max_steps: int = CHAT_MAX_STEPS) -> dict:
    """跑一轮对话：模型自己决定调用哪些构建工具，最多 max_steps 步。"""
    message = (message or "").strip()
    if not message:
        return {"ok": False, "error": "消息为空"}
    sid = _safe_sid(session_id)
    state = load_chat_session(sid) if sid else {}
    if not state:
        sid = "chat_" + time.strftime("%Y%m%d_%H%M%S")
        state = {"id": sid, "title": message[:30], "created": time.time(),
                 "messages": [], "drafts": {}}
    history = state.setdefault("messages", [])
    state["_llm"] = {"model": model, "think": think, "provider": provider}
    state["ctx"] = [p for p in (context_paths or []) if isinstance(p, str) and p.strip()][:20]
    state["use_history"] = use_history

    sys_parts = [CHAT_SYSTEM]
    if state["ctx"]:
        sys_parts.append(_context_block(state["ctx"]))
    if use_history:
        sys_parts.append(_history_block("plugin") + _history_block("agent"))
    drafts = state.get("drafts") or {}
    if drafts:
        names = [k for k, v in drafts.items() if v]
        sys_parts.append("\n#### 当前未保存草稿\n" + "、".join(names)
                         + "（用户说保存时直接用 save_agent / save_plugin）\n")
    system_msg = {"role": "system", "content": "\n".join(p for p in sys_parts if p)}

    convo = [system_msg] + list(history)
    convo.append({"role": "user", "content": message + ("\n\n（当前工作区根目录：%s）"
                                                       % APP_DIR)})
    steps = []
    reply = ""
    try:
        from ai_provider import get_llm
        llm = get_llm()
        for i in range(max(1, int(max_steps))):
            assistant = await _chat_with_tools(llm, convo, model=model, think=think, provider=provider)
            if isinstance(assistant, str):
                assistant = {"role": "assistant", "content": assistant}
            if assistant.get("content"):
                reply = assistant["content"]
            convo.append(assistant)
            tcs = assistant.get("tool_calls") or []
            if not tcs:
                break
            for tc in tcs:
                fn = (tc.get("function") or {})
                tname = fn.get("name") or ""
                try:
                    targs = json.loads(fn.get("arguments") or "{}")
                except Exception:
                    targs = {}
                try:
                    res = await _exec_tool(bridge, tname, targs, state)
                except Exception as e:
                    res = {"ok": False, "error": "工具执行异常: %r" % e}
                steps.append({"tool": tname, "args": targs, "ok": bool(res.get("ok")),
                              "result": _trim_tool_result(res)})
                convo.append({"role": "tool", "tool_call_id": tc.get("id") or tname,
                              "content": json.dumps(res, ensure_ascii=False)[:CHAT_TOOL_RESULT_MAX]})
                if not reply and isinstance(res, dict) and res.get("error"):
                    reply = "执行 %s 时出错：%s" % (tname, res.get("error"))
    except Exception as e:
        return {"ok": False, "error": "对话失败: %r" % e, "session": sid, "steps": steps,
                "drafts": state.get("drafts") or {}}

    # 只把「原始用户消息 + 助手/工具消息」写入历史（不落上下文文件正文，避免会话文件膨胀）
    turn = convo[len(history) + 1:]
    if turn:
        turn[0] = {"role": "user", "content": message}
    history.extend(turn)
    state.pop("_llm", None)
    save_chat_session(state)
    _record_history("chat", "chat", message[:200], bool(reply), None,
                    rounds=len(steps) or 1, model=model, provider=provider, key=sid)
    return {"ok": True, "session": sid, "reply": reply or "（完成）",
            "steps": steps, "drafts": state.get("drafts") or {},
            "messages": history, "title": state.get("title", "")}


def _trim_tool_result(res: dict) -> dict:
    """给 UI 展示用的工具结果（裁掉大字段，避免前端卡片过长）。"""
    if not isinstance(res, dict):
        return {"value": str(res)[:800]}
    out = {}
    for k, v in res.items():
        if isinstance(v, str):
            out[k] = v if len(v) <= 800 else v[:800] + " …"
        elif isinstance(v, list):
            out[k] = v[:60]
        elif isinstance(v, dict):
            out[k] = {kk: (vv if not isinstance(vv, str) or len(vv) <= 400 else vv[:400] + " …")
                      for kk, vv in list(v.items())[:30]}
        else:
            out[k] = v
    return out


def run_chat_sync(bridge, body: dict) -> dict:
    body = body or {}
    return bridge.lt.run_coro(
        run_chat(bridge, body.get("session", "") or "", body.get("message", "") or "",
                 model=body.get("model") or None, think=body.get("think") or "low",
                 provider=body.get("provider") or None,
                 context_paths=body.get("context_paths") or None,
                 use_history=bool(body.get("use_history", True)),
                 max_steps=int(body.get("max_steps", CHAT_MAX_STEPS) or CHAT_MAX_STEPS)),
        timeout=900,
    )
