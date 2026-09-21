# -*- coding: utf-8 -*-
"""BOT Self Coding：智能体用内置「构建助手」构建/改进自己。

设计（取代旧版依赖外部 CodeBuddy CLI 的自我编程）：
- 总开关 BOT_SELF_CODING_ENABLED（默认关），开启后智能体才获权自我编程。
- 开启后默认权限 BOT_SELF_CODING_PERM = "full"（完全访问），可在 UI/命令改。
- 智能体工作受阻或想要新功能时，调用 file_issue() 向用户提 Issue；
  Issue 默认自动执行（BOT_SELF_CODING_ISSUE_AUTO=True），关闭后需用户同意。
- 智能体把需求/bug/traceback 交给构建助手，构建助手做完回流结果，二者协作闭环。
- 新构建出问题，智能体把 bug+回传 traceback 再次交给构建助手修，直到通过。
- 构建产物默认自动装载并启动（BOT_SELF_CODING_AUTO_LOAD=True），关闭后需用户允许。
"""
import os
import threading
import time
import uuid
from typing import Dict, List, Optional

import config
from quiet import degrade

_SELF_ROOT = getattr(config, "DATA_DIR", os.path.join(config._HERE, "..", "data"))
SELF_CODING_DIR = os.path.abspath(os.path.join(_SELF_ROOT, "self_coding"))
ISSUES_PATH = os.path.join(SELF_CODING_DIR, "issues.jsonl")

# Issue 状态
ISSUE_OPEN = "open"          # 待处理（自动执行模式下已被派给构建助手）
ISSUE_PENDING = "pending"    # 待用户同意（自动执行关闭时）
ISSUE_DONE = "done"          # 已构建完成（可能已装载）
ISSUE_FAILED = "failed"      # 构建失败且超出重试
ISSUE_REJECTED = "rejected"  # 用户拒绝


# ---------------------------------------------------------------------------
# 统一提 Issue：所有场景/world（chat / mc / pvz / pc / bilibili …）的 AI 调用失败时，
# 经此统一入口提自编程 Issue。默认关闭（BOT_SELF_CODING_ENABLED=False）时不触发。
# ---------------------------------------------------------------------------
import contextvars
import traceback as _tb_mod

# 当前场景/world 标签：各 world 在循环入口 set_world("mc")，用于 Issue 定位。
SC_WORLD = contextvars.ContextVar("sc_world", default="general")
_WORLD_DEFAULT = "general"
_LAST_REPORT = {}                 # (world, err_type, hint) -> 时间戳，冷却去重
_REPORT_COOLDOWN = 600.0          # 同一世界+异常类型 10 分钟内只提一次


def set_world(name: str):
    """由各 world/brain 在循环入口设置当前场景标签（chat/mc/pvz/...），用于 Issue 定位。"""
    try:
        SC_WORLD.set(str(name or _WORLD_DEFAULT))
    except Exception:
        pass


def build_capability_hint() -> str:
    """返回「自身能力」系统提示（已装载插件清单 + 自我编程能力），作为所有聊天/大脑管线的
    单一真相源。chat_service / mc_bot_brain / pvz_agent 等统一调用本函数，避免各管线各写一份。

    让模型知道当前已装载启用的插件，以及具备自我编程（可自主提 issue 构建新插件）的能力，
    从而避免对「插件能做的」或「可自建的」需求回答「做不到了 / 我做不到 / 不支持」。
    自编程未开启（is_enabled()=False）时返回空串，下游据此不注入。
    """
    if not is_enabled():
        return ""
    parts = []

    # 当前已装载启用的插件清单
    try:
        from bridge import plugins_api
    except Exception:
        try:
            import plugins_api
        except Exception:
            plugins_api = None
    plug_lines = []
    if plugins_api is not None:
        pm = plugins_api.manager()
        if pm is not None:
            try:
                data = pm.list_all()
                for it in data.get("items", []):
                    if not it.get("enabled"):
                        continue
                    if it.get("kind") in ("invalid",):
                        continue
                    desc = (it.get("description") or "").strip() or "（无描述）"
                    title = it.get("title") or it.get("name")
                    status = "（运行中）" if it.get("running") else "（已启用，未运行）"
                    plug_lines.append(f"- {title}（{it.get('name')}，{it.get('kind')}）{status}：{desc}")
            except Exception:
                pass
    if plug_lines:
        parts.append("【你当前已装载并启用的插件】当主人提出的需求能被下列插件覆盖时，"
                     "优先调用这些插件的能力，而不是说自己做不到：\n" + "\n".join(plug_lines))

    parts.append(
        "【你的自我编程（自主扩展）能力】你具备自主扩展能力：当主人提出的功能你当前"
        "无法直接用内置能力或上面列出的插件实现时，系统会自动为你创建一个开发任务"
        "（issue）并自主构建对应的插件/功能，构建完成后会自动装载、启用并启动。\n"
        "因此，遇到暂时做不到的需求，不要回答「做不到了」「我做不到」「目前还不支持」之类"
        "的话；应主动说明你打算为它构建/扩展一个插件，并让系统去实现，例如："
        "「这个我暂时没有现成能力，不过我可以自动做一个插件来实现，已经帮你提交开发任务了，"
        "完成后会自动启用。」"
    )

    return "\n\n".join(parts)


def report_ai_error(error, *, context: str = "", kind: str = "fix",
                   title: str = "") -> Optional[Dict]:
    """统一入口：任何 world 的 AI 调用失败时调用。

    - 总开关关闭时不触发；
    - 自编程构建过程内（world='self_coding'）不二次提 Issue，避免递归；
    - 同一 world + 异常类型在冷却期内只提一次，避免刷屏；
    - 永不抛异常（失败只打印，不影响主流程）。
    """
    try:
        if not is_enabled():
            return None
        world = SC_WORLD.get() or _WORLD_DEFAULT
        if world == "self_coding":
            return None
        err_type = type(error).__name__
        key = (world, err_type, (title or context)[:40])
        now = time.time()
        if key in _LAST_REPORT and (now - _LAST_REPORT[key]) < _REPORT_COOLDOWN:
            return None
        _LAST_REPORT[key] = now
        tb = _tb_mod.format_exc()
        t = title or f"[{world}] AI 调用失败：{err_type}"
        body = f"场景/world：{world}\n"
        if context:
            body += context + "\n"
        body += f"异常：{error}"
        return file_issue(title=t, body=body, kind=kind, traceback_text=tb)
    except Exception as e:
        print(f"[SELF_CODING] report_ai_error 失败（不影响主流程）: {e}")
        return None


def _ensure_dir():
    try:
        os.makedirs(SELF_CODING_DIR, exist_ok=True)
    except Exception:
        pass


# 写锁：自编程可能在后台任务并发重写 issues.jsonl，串行化避免互相覆盖
_WRITE_LOCK = threading.Lock()


def _coalesce_delta(log: List[Dict], ev: Dict) -> bool:
    """把连续的 delta 文本合并到同 round+kind 的最后一条，避免日志条目爆炸。"""
    r = ev.get("round")
    k = ev.get("kind")
    if log:
        last = log[-1]
        if (last.get("type") == "delta" and last.get("round") == r
                and last.get("kind") == k):
            last["text"] = (last.get("text") or "") + (ev.get("text") or "")
            return True
    log.append(dict(ev))
    return False


def _read_issues() -> List[Dict]:
    _ensure_dir()
    if not os.path.isfile(ISSUES_PATH):
        return []
    out = []
    try:
        with open(ISSUES_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(eval(line))  # 受控：仅本机生成的 dict 文本
                except Exception:
                    pass
    except Exception:
        pass
    return out


def _append_issue(issue: Dict):
    _ensure_dir()
    try:
        with _WRITE_LOCK:
            with open(ISSUES_PATH, "a", encoding="utf-8") as f:
                f.write(repr(issue) + "\n")
    except Exception as e:
        degrade("libs/qq_bot_runtime/self_coding.py:_append_issue", e, "降级：写入 issue 失败")


def _rewrite_issues(issues: List[Dict]):
    _ensure_dir()
    try:
        with _WRITE_LOCK:
            with open(ISSUES_PATH, "w", encoding="utf-8") as f:
                for it in issues:
                    f.write(repr(it) + "\n")
    except Exception as e:
        degrade("libs/qq_bot_runtime/self_coding.py:_rewrite_issues", e, "降级：重写 issue 失败")


def is_enabled() -> bool:
    return bool(getattr(config, "BOT_SELF_CODING_ENABLED", False))


def get_perm() -> str:
    return getattr(config, "BOT_SELF_CODING_PERM", "full")


def set_enabled(flag: bool) -> Dict:
    config.BOT_SELF_CODING_ENABLED = bool(flag)
    return {"ok": True, "enabled": is_enabled()}


def set_perm(mode: str) -> Dict:
    from bridge.builder_api import PERM_MODES
    if mode not in PERM_MODES:
        return {"ok": False, "error": f"未知权限档：{mode}，可选 {list(PERM_MODES)}"}
    config.BOT_SELF_CODING_PERM = mode
    return {"ok": True, "perm": mode}


def set_issue_auto(flag: bool) -> Dict:
    config.BOT_SELF_CODING_ISSUE_AUTO = bool(flag)
    return {"ok": True, "issue_auto": bool(flag)}


def set_auto_load(flag: bool) -> Dict:
    config.BOT_SELF_CODING_AUTO_LOAD = bool(flag)
    return {"ok": True, "auto_load": bool(flag)}


def list_issues(state: Optional[str] = None) -> List[Dict]:
    issues = _read_issues()
    if state:
        issues = [i for i in issues if i.get("state") == state]
    return issues


# ---------------------------------------------------------------------------
# 提 Issue（智能体受阻/需要新功能时调用）
# ---------------------------------------------------------------------------
def file_issue(title: str, body: str = "", kind: str = "feature",
               traceback_text: str = "") -> Dict:
    """向用户提一个自我编程 Issue。

    kind: feature（想要新功能）/ fix（受阻需要修 bug）/ improve（改进现有）。
    返回 {ok, issue_id, auto}；auto=True 表示已自动派给构建助手执行。
    """
    if not is_enabled():
        return {"ok": False, "error": "BOT Self Coding 未开启，不能提 Issue"}
    issue_id = "SC-" + uuid.uuid4().hex[:8]
    issue = {
        "id": issue_id,
        "title": title,
        "body": body,
        "kind": kind,
        "traceback": traceback_text or "",
        "state": ISSUE_PENDING,
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
        "rounds": 0,
        "result": "",
        "build_log": [],            # 实时构建过程事件流（供 UI 展开查看）
    }
    auto = bool(getattr(config, "BOT_SELF_CODING_ISSUE_AUTO", True))
    if auto:
        issue["state"] = ISSUE_OPEN
        _append_issue(issue)
        # 自动派发：交给构建助手（异步上下文下作为后台任务，不阻塞调用方）
        try:
            _launch_dispatch(issue_id)
        except Exception as e:
            degrade("libs/qq_bot_runtime/self_coding.py:file_issue", e,
                    "降级：自动派发 issue 失败，转为待用户同意")
            issue["state"] = ISSUE_PENDING
            _rewrite_one(issue)
    else:
        _append_issue(issue)
    return {"ok": True, "issue_id": issue_id, "auto": auto, "state": issue["state"]}


def _launch_dispatch(issue_id: str):
    """派发 Issue 给构建助手（兼容同步与异步上下文）。

    - 若当前已有运行中的事件循环（如在 async chat() 内被 report_ai_error 调用），
      作为后台任务提交，不阻塞调用方、也不破坏外层循环；
    - 否则新建一个事件循环同步跑完（兼容命令触发的同步路径）。
    """
    import asyncio
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None:
        loop.create_task(_dispatch(issue_id))
        return
    new_loop = asyncio.new_event_loop()
    try:
        new_loop.run_until_complete(_dispatch(issue_id))
    finally:
        new_loop.close()


def _rewrite_one(issue: Dict):
    issues = _read_issues()
    replaced = False
    for i, it in enumerate(issues):
        if it.get("id") == issue.get("id"):
            issues[i] = issue
            replaced = True
            break
    if not replaced:
        issues.append(issue)
    _rewrite_issues(issues)


def approve_issue(issue_id: str) -> Dict:
    """用户同意一个 pending 的 Issue，派给构建助手执行。"""
    issues = _read_issues()
    for it in issues:
        if it.get("id") == issue_id and it.get("state") == ISSUE_PENDING:
            it["state"] = ISSUE_OPEN
            it["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
            _rewrite_issues(issues)
            try:
                _launch_dispatch(issue_id)
            except Exception as e:
                degrade("libs/qq_bot_runtime/self_coding.py:approve_issue", e,
                        "降级：派发 issue 失败")
            return {"ok": True, "issue_id": issue_id}
    return {"ok": False, "error": "未找到待同意的 Issue"}


def reject_issue(issue_id: str) -> Dict:
    issues = _read_issues()
    for it in issues:
        if it.get("id") == issue_id and it.get("state") == ISSUE_PENDING:
            it["state"] = ISSUE_REJECTED
            it["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
            _rewrite_issues(issues)
            return {"ok": True, "issue_id": issue_id}
    return {"ok": False, "error": "未找到待处理的 Issue"}


def retry_issue(issue_id: str) -> Dict:
    """把失败（failed）的 Issue 重新派发给构建助手执行（失败重提）。"""
    issues = _read_issues()
    for it in issues:
        if it.get("id") == issue_id and it.get("state") == ISSUE_FAILED:
            it["state"] = ISSUE_OPEN
            it["result"] = ""
            it.setdefault("build_log", [])
            it["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
            _rewrite_issues(issues)
            try:
                _launch_dispatch(issue_id)
            except Exception as e:
                degrade("libs/qq_bot_runtime/self_coding.py:retry_issue", e,
                        "降级：重提 issue 失败")
            return {"ok": True, "issue_id": issue_id}
    return {"ok": False, "error": "未找到失败状态的 Issue"}


# ---------------------------------------------------------------------------
# 派发 + 协作构建（与构建助手对话，出 bug 反馈 traceback 再修）
# ---------------------------------------------------------------------------
async def _dispatch(issue_id: str, max_rounds: int = 4) -> Dict:
    """把 Issue 派给构建助手：多轮协作，bug 回传 traceback 直到通过。"""
    # 标记当前处于自编程构建过程：期间 LLM 失败不二次提 Issue，避免递归
    tok = SC_WORLD.set("self_coding")
    try:
        issues = _read_issues()
        issue = next((i for i in issues if i.get("id") == issue_id), None)
        if not issue:
            return {"ok": False, "error": "issue 不存在"}
        issue["state"] = ISSUE_OPEN
        issue["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
        _rewrite_issues(issues)

        prompt = _build_prompt(issue)
        last_err = ""
        for rnd in range(1, max_rounds + 1):
            issue["rounds"] = rnd
            issue["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
            _rewrite_issues(issues)
            # 第一轮给需求；后续轮把上一轮的 bug+回传 traceback 交回构建助手
            if rnd == 1:
                turn = prompt
            else:
                turn = (f"上一次构建后运行报错，请修复。\n"
                        f"错误/失败信息：\n{last_err}\n"
                        f"请定位并修正后重新给出可装载的产物。")
            res = await _run_builder(turn, issue, issues, issue_id)
            if not res.get("ok"):
                last_err = res.get("error", "未知错误")
                issue["result"] = last_err
                issue["state"] = ISSUE_FAILED
                _rewrite_issues(issues)
                continue
            # 构建助手产出了草稿；尝试装载（自动装载开启时）
            load_res = await _try_load(res, issue)
            if load_res.get("ok"):
                issue["state"] = ISSUE_DONE
                issue["result"] = load_res.get("detail", "构建并装载完成")
                issue["loaded_name"] = load_res.get("name")
                _rewrite_issues(issues)
                return {"ok": True, "issue_id": issue_id, "round": rnd,
                        "loaded": load_res.get("name")}
            # 装载失败：把 traceback 回传构建助手再修
            last_err = load_res.get("error", "装载失败")
            issue["result"] = last_err
            _rewrite_issues(issues)
        issue["state"] = ISSUE_FAILED
        issue["result"] = last_err or "超出最大重试轮数"
        _rewrite_issues(issues)
        return {"ok": False, "issue_id": issue_id, "error": issue["result"]}
    finally:
        # 无论成功/失败/异常，复位 world 标签，避免泄漏到外层上下文
        SC_WORLD.reset(tok)


def _build_prompt(issue: Dict) -> str:
    kind = issue.get("kind", "feature")
    head = {
        "feature": "我需要一个新功能",
        "fix": "我在工作中遇到阻碍，需要修复一个 bug",
        "improve": "我要改进一个已有功能",
    }.get(kind, "我有一个构建需求")
    parts = [f"{head}：{issue.get('title','')}"]
    if issue.get("body"):
        parts.append("需求描述：\n" + issue["body"])
    if issue.get("traceback"):
        parts.append("相关报错/traceback：\n" + issue["traceback"])
    parts.append(
        "你是一个内置构建助手。请直接动手实现一个【可被装载的插件(plugin)包】，"
        "不要只停留在分析/阅读代码。\n\n"
        "硬性要求：\n"
        "1) 产物必须以插件包形式落到 `plugins/<包名>/` 目录：至少含 `plugin.py` 与 "
        "MANIFEST（含 name/title/version/kind=feature 或 platform/sidecar/local/description）。\n"
        "2) 必须调用工具 `save_plugin`（或在 `plugins/<包名>/` 下用 `write_file`）把产物真正"
        "保存下来，不能只输出代码片段。\n"
        "3) 工作区仅允许写入 plugins/agents/bridge/libs/webui/config；"
        "**不要**尝试修改 app.py / server.py / settings_store.py 等核心入口（它们不在允许根内）。"
        "若功能需要核心改动，请用插件方式扩展（如 brain/sidecar/local 类型插件，或 hook 进现有 bridge）。\n"
        "4) 包名用简短英文小写+下划线，由需求推导（例：「定时提醒」-> reminder_plugin）。\n"
        "5) 完成后确保包能通过 Python 语法检查（import 不报错）。\n\n"
        "请开始调查并产出可保存的插件包。"
    )
    return "\n\n".join(parts)


async def _run_builder(turn: str, issue: Optional[Dict] = None,
                      issues: Optional[List[Dict]] = None,
                      issue_id: str = "") -> Dict:
    """调用内置构建助手（builder_api.run_chat）执行一轮构建。

    若传入 issue/issues，则把构建过程事件（emit 回调）实时写入 issue['build_log']
    并节流落盘，供 UI 点击 Issue 展开查看实时构建过程。
    """
    try:
        import bridge.builder_api as builder_api
    except Exception as e:
        return {"ok": False, "error": f"构建助手不可用：{e!r}"}
    # 把自编程权限档写入构建助手设置（run_chat 内部读 permission_mode）
    try:
        builder_api.set_settings({"permission_mode": get_perm()})
    except Exception:
        pass
    try:
        from bridge.core_bridge import get_bridge
        bridge = get_bridge()
    except Exception:
        bridge = None
    if bridge is None:
        return {"ok": False, "error": "核心未运行，无法调用构建助手"}

    emit_last = {"t": 0.0}

    def _emit(ev: Dict):
        if not issue or not issues:
            return
        try:
            log = issue.setdefault("build_log", [])
            if ev.get("type") == "delta":
                _coalesce_delta(log, ev)
            else:
                log.append(dict(ev))
            issue["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
            now = time.time()
            # 节流落盘：至少 0.4s 一次，关键节点（工具/思考/完成）立即落盘
            if (now - emit_last["t"]) >= 0.4 or ev.get("type") in ("done", "tool_end", "think_end"):
                emit_last["t"] = now
                _rewrite_issues(issues)
        except Exception as e:
            degrade("libs/qq_bot_runtime/self_coding.py:_run_builder.emit",
                    e, "降级：构建日志落盘失败")

    try:
        res = await builder_api.run_chat(
            bridge,
            message=turn,
            use_history=True,
            stream=True,
            emit=_emit,
            max_steps=22,
        )
        # 记录构建产物识别到的包名，便于排错/重提
        if issue is not None:
            try:
                nm = _extract_pkg_name(res)
                if nm:
                    issue["built_name"] = nm
            except Exception:
                pass
        # 确保最终状态落盘（避免末尾 delta 被节流丢弃）
        try:
            _rewrite_issues(issues)
        except Exception:
            pass
        return {"ok": True, "result": res}
    except Exception as e:
        return {"ok": False, "error": f"构建助手执行异常：{e!r}"}


def _get_core():
    try:
        from bridge.core_bridge import get_bridge
        b = get_bridge()
        if b is not None:
            return getattr(b, "core", None)
    except Exception:
        pass
    try:
        from agent_core import get_core
        return get_core()
    except Exception:
        return None


def _newest_plugin_since(issue: Dict) -> Optional[str]:
    """兜底：扫描 plugins/ 下构建期间（晚于 issue 创建时间）新建/改动过的插件目录，
    返回其包名。用于构建助手用 write_file 落到 plugins/<name>/ 但未显式 save_plugin 的场景。
    """
    import os
    try:
        import time as _t
        import bridge.pkg_manager as pm
        root = getattr(pm, "PACKAGE_DIR", None) or os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(pm.__file__))), "plugins")
        if not os.path.isdir(root):
            return None
        created = 0.0
        cs = issue.get("created")
        if cs:
            try:
                created = _t.mktime(_t.strptime(cs, "%Y-%m-%d %H:%M:%S"))
            except Exception:
                created = 0.0
        best, best_m = None, 0.0
        for d in os.listdir(root):
            dp = os.path.join(root, d)
            if not (os.path.isdir(dp) and os.path.exists(os.path.join(dp, "plugin.py"))):
                continue
            m = os.path.getmtime(dp)
            if m > best_m:
                best_m, best = m, d
        # 仅在确实晚于 issue 创建时才信任（排除历史插件）
        if best and best_m >= created:
            return best
    except Exception:
        pass
    return None


async def _try_load(build_res: Dict, issue: Dict) -> Dict:
    """尝试把构建产物装载进核心；自动装载关闭时只返回「建议装载」不实际装载。"""
    # 构建助手生成的产物可能含 manifest 包；这里根据产物名尝试 pkg_manager.load
    name = (_extract_pkg_name(build_res) or issue.get("loaded_name")
            or _newest_plugin_since(issue))
    if not name:
        return {"ok": False, "error": "未能从产物识别包名，无法装载"}
    auto = bool(getattr(config, "BOT_SELF_CODING_AUTO_LOAD", True))
    if not auto:
        return {"ok": False, "error": f"自动装载已关闭，请用户允许或手动 /装载 {name}",
                "suggest_load": name}
    try:
        from bridge.plugins_api import manager as _pm_manager
        pm = _pm_manager()
        if pm is None:
            return {"ok": False, "error": "插件管理器未初始化"}
        res = pm.load(name)
        if res.get("ok"):
            # 对新 brain/world 类型尝试启动
            try:
                core = _get_core()
                if core is not None:
                    br = core.brains.get(name)
                    if br is not None and getattr(br, "auto_start_on_core", False):
                        await br.start()
            except Exception:
                pass
            # 通知 UI/用户：插件已自动加入插件页并启动
            try:
                b = getattr(pm, "bridge", None)
                if b is not None:
                    b.push(None, {"type": "plugins_updated", "name": name})
                    b.push(None, {"type": "message", "role": "assistant",
                                  "text": f"✅ 已自动装载并启动插件「{name}」，可在「插件」页查看。"})
            except Exception:
                pass
            return {"ok": True, "name": name, "detail": f"已自动装载并启动 {name}"}
        return {"ok": False, "error": res.get("error", f"pkg_manager.load({name}) 失败")}
    except Exception as e:
        return {"ok": False, "error": f"装载异常：{e!r}"}


def _pkg_name_from_plugin_path(p) -> Optional[str]:
    """从 plugins/<name>/... 形式的相对路径取包名（目录第二段）。"""
    if not isinstance(p, str):
        return None
    rel = p.replace("\\", "/").lstrip("/")
    if rel.startswith("plugins/"):
        parts = rel.split("/")
        if len(parts) >= 2 and parts[1]:
            return parts[1]
    return None


def _extract_pkg_name(build_res: Dict) -> Optional[str]:
    """从构建助手返回里尽量提取保存的包名（优先 manifest 中的 name）。

    run_chat 不会回填 saved/artifacts，因此还要扫描工具步骤（save_plugin /
    write_file）与草稿，才能稳定拿到包名，否则会一直报「无法解析包名」。
    """
    if not isinstance(build_res, dict):
        return None
    # 1) 显式 saved / artifacts 字段
    saved = build_res.get("saved") or build_res.get("artifacts") or []
    if isinstance(saved, list):
        for art in saved:
            if isinstance(art, dict) and art.get("kind") in ("plugin", "agent", "brain", "world"):
                nm = art.get("name")
                if nm:
                    return nm
    # 2) 扫描工具步骤：save_plugin(name=...) 或 write_file(path=plugins/<name>/...)
    for src in (build_res.get("steps"), build_res.get("blocks")):
        if isinstance(src, list):
            for st in src:
                if not isinstance(st, dict):
                    continue
                t = st.get("tool") or ""
                a = st.get("args") or {}
                if t == "save_plugin":
                    nm = a.get("name") if isinstance(a, dict) else None
                    if nm:
                        return nm
                    man = a.get("manifest") if isinstance(a, dict) else None
                    if isinstance(man, dict) and man.get("name"):
                        return man["name"]
                elif t == "write_file":
                    nm = _pkg_name_from_plugin_path(a.get("path") if isinstance(a, dict) else None)
                    if nm:
                        return nm
    # 3) 草稿里的 plugin / agent / brain 名
    drafts = build_res.get("drafts") or {}
    if isinstance(drafts, dict):
        for v in drafts.values():
            if isinstance(v, dict) and v.get("name"):
                return v["name"]
    # 4) 兼容字符串回执
    txt = str(build_res.get("text", ""))
    import re
    m = re.search(r"manifest[\"']?\s*name[\"']?\s*[:=]\s*[\"']([\w\-]+)[\"']", txt)
    if m:
        return m.group(1)
    return None


def _get_core():
    try:
        from agent_core import get_core
        return get_core()
    except Exception:
        return None
