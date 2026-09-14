# -*- coding: utf-8 -*-
"""CodeBuddy CLI 封装模块。

让 AI 能通过 CodeBuddy CLI（无头模式）查看/修改自己的代码、配置、插件。

架构：
  AI 自主游戏 -> code_task 动作
    -> codebuddy_cli.run_task(指令)
      -> 调用 codebuddy CLI 无头模式
        -> 返回结构化结果给 AI

安全边界：
- 限制 allowedTools 为 Read/Write/Edit（AI 只能读写编辑，不能执行危险操作）
- 限制工作目录在项目内
- 超时保护，避免 AI 卡住
"""
import json
import os
import shutil
import subprocess
import time
import threading
import uuid
from typing import Dict, List, Optional

import config

# CLI 路径（已迁移到 E 盘）
CLI_PATH = getattr(config, "CODEBUDDY_CLI_PATH", r"E:\codebuddy\bin\codebuddy.exe")

# 允许的工具（安全边界：只读/写/编辑，禁止执行命令等危险操作）
_ALLOWED_TOOLS = getattr(config, "CODEBUDDY_ALLOWED_TOOLS", "Read,Write,Edit")
# 工作目录（限制在项目内）
_WORK_DIR = getattr(config, "CODEBUDDY_WORK_DIR", r"E:\qq_bot")
# 默认超时（秒）
_TIMEOUT = getattr(config, "CODEBUDDY_TIMEOUT", 120)

# 核心代码文件（修改需人工确认）——运行主逻辑，改错会导致系统崩溃
_CORE_FILES = getattr(config, "CODEBUDDY_CORE_FILES", [
    "bot.py",           # 主程序
    "mc_agent.py",      # 自主游戏智能体
    "mc_watcher.py",    # Minecraft 监控
    "codebuddy_cli.py", # 自我编程模块（防止 AI 改掉自己的保护机制）
    "message_bus.py",   # 消息抽象层
    "qq_adapter.py",    # QQ 适配器
    "config.py",        # 全局配置
    "agent_core.py",    # 智能体核心
    "scheduler.py",     # 调度器
    "deepseek_client.py", # AI 客户端
])

# 待确认的修改请求队列（核心文件修改需人工确认）
_pending_edits: Dict[str, Dict] = {}
_pending_lock = threading.Lock()


def is_available() -> bool:
    """检查 CLI 是否可执行。"""
    return os.path.isfile(CLI_PATH)


def _run_cli(prompt: str, tools: str = None, timeout: int = None) -> Dict:
    """运行 CodeBuddy CLI 无头模式，返回结构化结果。"""
    tools = tools or _ALLOWED_TOOLS
    timeout = timeout or _TIMEOUT
    cmd = [
        CLI_PATH,
        "-p", prompt,
        "--output-format", "json",
        "--allowedTools", tools,
        "-y",
        "--permission-mode", "bypassPermissions",
    ]
    env = os.environ.copy()
    env["CODEBUDDY_SKIP_GIT_BASH_CHECK"] = "1"
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=_WORK_DIR,
            env=env,
            timeout=timeout,
        )
        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        # 解析 JSON 输出
        try:
            data = json.loads(stdout)
            return {"ok": True, "data": data, "stdout": stdout, "stderr": stderr, "exit": proc.returncode}
        except json.JSONDecodeError:
            # 输出不是 JSON，返回原始文本
            return {"ok": True, "data": None, "stdout": stdout, "stderr": stderr, "exit": proc.returncode}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"CLI 超时（>{timeout}秒）", "stdout": "", "stderr": ""}
    except FileNotFoundError:
        return {"ok": False, "error": f"找不到 CLI: {CLI_PATH}（请确认已安装到 E 盘）"}
    except Exception as e:
        return {"ok": False, "error": f"CLI 调用失败: {e}"}


def run_task(task: str, timeout: int = None) -> Dict:
    """执行一个代码任务（查看/修改代码）。

    返回结果 dict：{"ok": bool, "result": 摘要, "raw": 原始JSON, "error": 错误}
    """
    if not is_available():
        return {"ok": False, "error": f"CodeBuddy CLI 不可用（{CLI_PATH} 不存在）"}

    result = _run_cli(task, timeout=timeout)
    if not result.get("ok"):
        return result

    # 从 JSON 输出提取结果
    data = result.get("data")
    summary = _extract_summary(data, result.get("stdout", ""))
    # 检查是否有认证错误
    raw = result.get("stdout", "") + result.get("stderr", "")
    if "Authentication required" in raw or "Please use /login" in raw:
        return {"ok": False, "error": "CodeBuddy CLI 需要登录。请在终端运行 codebuddy 并输入 /login 登录账号。"}

    return {
        "ok": True,
        "result": summary,
        "raw": result.get("stdout", ""),
        "exit": result.get("exit"),
    }


def _extract_summary(data, stdout: str) -> str:
    """从 CLI JSON 输出提取可读的结果摘要。"""
    if data is None:
        return stdout[:500] if stdout else "(无输出)"
    try:
        # CodeBuddy CLI JSON 输出是消息列表，提取 assistant 的最终回复
        if isinstance(data, list):
            # 找最后一个 assistant 消息的文本内容
            for msg in reversed(data):
                if isinstance(msg, dict) and msg.get("role") == "assistant":
                    content = msg.get("content", [])
                    texts = _extract_text_from_content(content)
                    if texts:
                        return texts[:500]
            # 没有 assistant，取最后一个有文本的消息
            for msg in reversed(data):
                texts = _extract_text_from_content(msg.get("content", [])) if isinstance(msg, dict) else ""
                if texts:
                    return texts[:500]
            return json.dumps(data, ensure_ascii=False)[:500]

        if isinstance(data, dict):
            # 尝试 result/response 字段
            if "result" in data:
                r = data["result"]
                if isinstance(r, str):
                    return r[:500]
            if "response" in data:
                return str(data["response"])[:500]
            if "messages" in data:
                msgs = data["messages"]
                return _extract_summary(msgs, stdout)
            if "output" in data:
                return str(data["output"])[:500]
            return json.dumps(data, ensure_ascii=False)[:500]
        return str(data)[:500]
    except Exception:
        return stdout[:500] if stdout else "(无法解析输出)"


def _extract_text_from_content(content) -> str:
    """从消息 content 中提取纯文本。content 可能是 str 或 list。"""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        texts = []
        for item in content:
            if isinstance(item, dict):
                t = item.get("text") or item.get("content") or ""
                if t and isinstance(t, str):
                    texts.append(t)
            elif isinstance(item, str):
                texts.append(item)
        return "".join(texts).strip()
    return ""


def view_file(filepath: str) -> Dict:
    """让 AI 查看一个文件的内容。"""
    # 限制在工作目录内
    if not _is_in_workdir(filepath):
        return {"ok": False, "error": "路径超出允许的工作目录"}
    return run_task(f"请读取并总结文件 {filepath} 的内容，只读不改。")


def edit_file(filepath: str, instruction: str) -> Dict:
    """让 AI 修改一个文件。

    核心文件（运行主逻辑）修改前需人工确认；非核心文件直接改。
    """
    if not _is_in_workdir(filepath):
        return {"ok": False, "error": "路径超出允许的工作目录"}
    if _is_core_file(filepath):
        # 核心文件：需人工确认
        return request_edit_approval(filepath, instruction)
    # 非核心文件：直接改
    return run_task(f"请修改文件 {filepath}。要求：{instruction}。修改后简要说明改动。")


def _is_core_file(filepath: str) -> bool:
    """判断文件是否为核心代码（修改需确认）。"""
    try:
        name = os.path.basename(filepath).lower()
        return name in [f.lower() for f in _CORE_FILES]
    except Exception:
        return False


def request_edit_approval(filepath: str, instruction: str) -> Dict:
    """为核心文件修改请求人工确认。

    生成一个待确认请求，通过事件总线通知用户。返回"待确认"结果。
    """
    req_id = str(uuid.uuid4())[:8]
    request = {
        "id": req_id,
        "filepath": filepath,
        "instruction": instruction,
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "status": "pending",
    }
    with _pending_lock:
        _pending_edits[req_id] = request
    # 通过事件总线通知用户确认
    try:
        from message_bus import get_event_bus
        bus = get_event_bus()
        # 异步通知（不阻塞当前调用）
        import asyncio
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(_notify_edit_request(request))
        except RuntimeError:
            pass
    except Exception:
        pass
    return {
        "ok": False,
        "requires_approval": True,
        "request_id": req_id,
        "detail": f"修改核心文件 {os.path.basename(filepath)} 需要确认",
    }


async def _notify_edit_request(request: Dict):
    """把待确认的修改请求发给用户。"""
    try:
        from message_bus import get_event_bus
        bus = get_event_bus()
        await bus.emit("code.edit_request", {
            "request_id": request["id"],
            "file": request["filepath"],
            "instruction": request["instruction"],
        })
    except Exception:
        pass


def confirm_edit(request_id: str, approve: bool) -> Dict:
    """人工确认核心文件修改。

    approve=True 执行修改，False 拒绝。
    """
    with _pending_lock:
        request = _pending_edits.pop(request_id, None)
    if not request:
        return {"ok": False, "error": f"没有找到待确认的修改请求:{request_id}"}
    if not approve:
        return {"ok": True, "detail": "已拒绝修改", "approved": False}
    # 执行修改
    result = run_task(
        f"请修改文件 {request['filepath']}。要求：{request['instruction']}。修改后简要说明改动。"
    )
    result["approved"] = True
    result["request_id"] = request_id
    return result


def list_pending_edits() -> List[Dict]:
    """列出所有待确认的修改请求。"""
    with _pending_lock:
        return list(_pending_edits.values())


def _is_in_workdir(filepath: str) -> bool:
    """检查路径是否在允许的工作目录内（安全边界）。"""
    try:
        work = os.path.abspath(_WORK_DIR).lower()
        fp = os.path.abspath(filepath).lower()
        return fp.startswith(work)
    except Exception:
        return False
