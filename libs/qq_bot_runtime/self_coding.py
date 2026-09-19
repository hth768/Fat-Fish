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


def _ensure_dir():
    try:
        os.makedirs(SELF_CODING_DIR, exist_ok=True)
    except Exception:
        pass


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
        with open(ISSUES_PATH, "a", encoding="utf-8") as f:
            f.write(repr(issue) + "\n")
    except Exception as e:
        degrade("libs/qq_bot_runtime/self_coding.py:_append_issue", e, "降级：写入 issue 失败")


def _rewrite_issues(issues: List[Dict]):
    _ensure_dir()
    try:
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
    }
    auto = bool(getattr(config, "BOT_SELF_CODING_ISSUE_AUTO", True))
    if auto:
        issue["state"] = ISSUE_OPEN
        _append_issue(issue)
        # 自动派发：异步交给构建助手（不阻塞提 Issue 的调用方）
        try:
            import asyncio
            loop = asyncio.get_event_loop()
            loop.run_until_complete(_dispatch(issue_id))
        except Exception as e:
            degrade("libs/qq_bot_runtime/self_coding.py:file_issue", e,
                    "降级：自动派发 issue 失败，转为待用户同意")
            issue["state"] = ISSUE_PENDING
            _rewrite_one(issue)
    else:
        _append_issue(issue)
    return {"ok": True, "issue_id": issue_id, "auto": auto, "state": issue["state"]}


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
                import asyncio
                loop = asyncio.get_event_loop()
                loop.run_until_complete(_dispatch(issue_id))
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


# ---------------------------------------------------------------------------
# 派发 + 协作构建（与构建助手对话，出 bug 反馈 traceback 再修）
# ---------------------------------------------------------------------------
async def _dispatch(issue_id: str, max_rounds: int = 4) -> Dict:
    """把 Issue 派给构建助手：多轮协作，bug 回传 traceback 直到通过。"""
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
        res = await _run_builder(turn)
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
    parts.append("请生成或改进对应的智能体/插件代码，产出可保存的产物草稿，"
                 "并尽量让产物能通过语法检查与基本导入。")
    return "\n\n".join(parts)


async def _run_builder(turn: str) -> Dict:
    """调用内置构建助手（builder_api.run_chat）执行一轮构建。"""
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
    try:
        res = await builder_api.run_chat(
            bridge,
            message=turn,
            use_history=True,
        )
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


async def _try_load(build_res: Dict, issue: Dict) -> Dict:
    """尝试把构建产物装载进核心；自动装载关闭时只返回「建议装载」不实际装载。"""
    # 构建助手生成的产物可能含 manifest 包；这里根据产物名尝试 pkg_manager.load
    name = _extract_pkg_name(build_res) or issue.get("loaded_name")
    if not name:
        return {"ok": False, "error": "未能从产物识别包名，无法装载"}
    auto = bool(getattr(config, "BOT_SELF_CODING_AUTO_LOAD", True))
    if not auto:
        return {"ok": False, "error": f"自动装载已关闭，请用户允许或手动 /装载 {name}",
                "suggest_load": name}
    try:
        import bridge.pkg_manager as pm
        core = _get_core()
        if core is None:
            return {"ok": False, "error": "核心未运行，无法装载"}
        ok = pm.load(name, core)
        if ok:
            # 对新 brain/world 类型尝试启动
            try:
                br = core.brains.get(name)
                if br is not None and getattr(br, "auto_start_on_core", False):
                    await br.start()
            except Exception:
                pass
            return {"ok": True, "name": name, "detail": f"已自动装载并启动 {name}"}
        return {"ok": False, "error": f"pkg_manager.load({name}) 失败"}
    except Exception as e:
        return {"ok": False, "error": f"装载异常：{e!r}"}


def _extract_pkg_name(build_res: Dict) -> Optional[str]:
    """从构建助手返回里尽量提取保存的包名（优先 manifest 中的 name）。"""
    if not isinstance(build_res, dict):
        return None
    saved = build_res.get("saved") or build_res.get("artifacts") or []
    if isinstance(saved, list):
        for art in saved:
            if isinstance(art, dict) and art.get("kind") in ("plugin", "agent", "brain", "world"):
                nm = art.get("name")
                if nm:
                    return nm
    # 兼容字符串回执
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
