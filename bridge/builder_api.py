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
import sys
import tempfile
import time

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
                      think: bool = False, provider: str = None) -> dict:
    ctx = _available_context()
    base_user = (
        "可用大脑: %s\n可用插件: %s\n\n需求: %s"
        % (ctx["brains"], [p["name"] for p in ctx["plugins"]], requirement)
    )
    last_err = ""
    last_raw = None
    for i in range(max_rounds):
        user = base_user if i == 0 else (
            "上一版 JSON 未能通过校验，错误：%s\n请修正后重新输出完整 JSON。需求不变：%s"
            % (last_err, requirement)
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
        return {"ok": True, "mode": "agent", "data": norm, "rounds": i + 1}
    return {"ok": False, "error": "多次修正仍未通过校验（最后错误：%s）" % last_err, "raw": last_raw}


async def generate_plugin(requirement: str, kind: str = "", max_rounds: int = 3,
                       model: str = None, think: bool = False, provider: str = None) -> dict:
    kind = (kind or "").strip().lower()
    system = PLUGIN_SYSTEM
    if kind:
        system += "\n本次要求 kind 必须为: %s" % kind
    last_err = ""
    last_raw = None
    for i in range(max_rounds):
        if i == 0:
            user = "需求: %s" % requirement
        else:
            user = (
                "你上一版代码没能通过系统校验，错误信息如下：\n%s\n\n"
                "请修正后重新输出**完整**的 JSON（结构不变），务必解决上面的错误。\n"
                "需求不变：%s" % (last_err, requirement)
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
        return {"ok": True, "mode": "plugin", "data": norm, "rounds": i + 1}
    return {"ok": False, "error": "经过 %d 次修正仍未通过校验（最后错误：%s）" % (max_rounds, last_err), "raw": last_raw}


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
                         provider: str = None) -> dict:
    return bridge.lt.run_coro(
        generate_agent(requirement, max_rounds=2, model=model, think=think, provider=provider),
        timeout=120)


def generate_plugin_sync(bridge, requirement: str, kind: str = "", model: str = None,
                         think: bool = False, provider: str = None) -> dict:
    return bridge.lt.run_coro(
        generate_plugin(requirement, kind, max_rounds=3, model=model, think=think, provider=provider),
        timeout=300)


def save_agent_sync(bridge, data: dict) -> dict:
    return bridge.lt.run_coro(save_agent(bridge, data or {}), timeout=60)


def save_plugin_sync(bridge, payload: dict) -> dict:
    payload = payload or {}
    return bridge.lt.run_coro(
        save_plugin(bridge, payload.get("name", ""), payload.get("manifest") or {}, payload.get("code") or ""),
        timeout=60,
    )
