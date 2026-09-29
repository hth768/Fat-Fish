# -*- coding: utf-8 -*-
"""Node 运行时桥：在 Node 子进程里真跑 cortico-world-* 的 TS 代码。

Python 侧以 `World` 契约的方式使用 TS World：
    bridge = NodeWorldBridge(manifest, ctx_cfg=..., ...)
    await bridge.start()          # 拉起 node + create + start
    tools = await bridge.tools()  # 工具声明（转成 ToolDef，handler 走 RPC）
    await bridge.stop()

通信是 stdio 上的换行分隔 JSON-RPC（见 node/cortico_host.mjs）。
宿主能力（pushEvent / 落库 / 持久化 / 密钥）由 Python 实现，Node 侧回调过来。

前置：Node >= 22；跑 TS 源码需要 tsx（CORTICO_NODE_LOADER 指定，缺省 `--import tsx`）。
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import time
from typing import Any, Callable, Dict, List, Optional

from .manifest import CorticoManifest
from .types import (ConfigGroup, PromptDocDecl, ToolCallContext, ToolDef, ToolOutcome,
                    World, WorldConsoleDecl, WorldHost)

_HERE = os.path.dirname(os.path.abspath(__file__))
_NODE_DIR = os.path.join(_HERE, "node")
_HOST_JS = os.path.join(_NODE_DIR, "cortico_host.mjs")
_LOADER_JS = os.path.join(_NODE_DIR, "cortico_loader.mjs")

DEFAULT_TIMEOUT = 30.0


class BridgeError(RuntimeError):
    """Node 桥调用失败。"""


def _default_node() -> str:
    return shutil.which("node") or shutil.which("node.exe") or "node"


def _file_url(path_str: str) -> str:
    """Windows 盘符路径 -> file:// URL（`--import` 与入口脚本都只认这种形式）。"""
    try:
        from pathlib import Path
        return Path(os.path.abspath(path_str)).as_uri()
    except Exception:
        return path_str


class NodeWorldBridge:
    """管理一个 Node 子进程，并把 TS World 暴露成 Python 可调用的对象。"""

    def __init__(self,
                 manifest: CorticoManifest,
                 cfg: Optional[Dict[str, Any]] = None,
                 timezone: str = "",
                 bot_name: str = "",
                 data_dir: str = "",
                 secrets: Optional[Dict[str, str]] = None,
                 host_callbacks: Optional[Dict[str, Callable]] = None,
                 env: Optional[Dict[str, str]] = None,
                 core_root: str = ""):
        self.manifest = manifest
        self.cfg = dict(cfg or {})
        self.timezone = timezone
        self.bot_name = bot_name
        self.data_dir = data_dir
        self.secrets = dict(secrets or {})
        self.host_callbacks = host_callbacks or {}
        self.core_root = core_root
        self.env_extra = dict(env or {})
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._seq = 0
        self._pending: Dict[int, asyncio.Future] = {}
        self._reader_task: Optional[asyncio.Task] = None
        self._timeout = float(getattr(_cfg(), "CORTICO_NODE_TIMEOUT_SEC", DEFAULT_TIMEOUT))
        self._world_id = ""
        self._stderr_tail: List[str] = []

    # ---- 进程 ----
    def _build_cmd(self) -> List[str]:
        node = getattr(_cfg(), "CORTICO_NODE_BIN", "") or _default_node()
        loader = getattr(_cfg(), "CORTICO_NODE_LOADER", "") or "--import tsx"
        cmd = [node]
        if loader:
            cmd += loader.split()
        # 解析钩子放在最后注册：Node 的模块钩子里「后注册的先执行」，
        # 这样 cortico/... 的裸导入先被我们拦下，再交给 tsx 处理 TS 源码。
        # Windows 上 --import 只接受 file:// URL，直接给盘符路径会被当成 URL 协议而报错。
        cmd += ["--import", _file_url(_LOADER_JS)]
        # 入口脚本保持普通路径：Node 在 Windows 上会把 file:// URL 形式的入口当成相对说明符
        cmd += [_HOST_JS, self.manifest.root]
        return cmd

    def _cwd(self) -> str:
        """子进程工作目录：需要能解析到 tsx 等工具依赖，缺省用 Cortico 仓库根。"""
        cfg = _cfg()
        return (str(getattr(cfg, "CORTICO_NODE_CWD", "") or "")
                or self.core_root or self.manifest.root)

    def _build_env(self) -> Dict[str, str]:
        env = dict(os.environ)
        if self.core_root:
            env["CORTICO_CORE_ROOT"] = self.core_root
        paths = list(getattr(_cfg(), "CORTICO_NODE_MODULES_PATHS", []) or [])
        if paths:
            env["NODE_PATH"] = os.pathsep.join(paths)
        env.update({k: str(v) for k, v in self.env_extra.items()})
        return env

    async def start(self) -> Dict[str, Any]:
        ok, why = self.manifest.compatible()
        if not ok:
            raise BridgeError(f"{self.manifest.name}: {why}")
        cmd = self._build_cmd()
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._build_env(),
                cwd=self._cwd(),
            )
        except FileNotFoundError as e:
            raise BridgeError(f"启动 Node 失败（{cmd[0]}）: {e}")
        self._reader_task = asyncio.ensure_future(self._read_loop())
        asyncio.ensure_future(self._drain_stderr())
        try:
            hello = await self.request("hello", timeout=min(self._timeout, 60.0))
        except Exception as e:
            await self._kill()
            raise BridgeError(f"{self.manifest.name} 握手失败: {e}；stderr={self.stderr_tail()}")
        self._world_id = str(hello.get("worldId") or "")
        await self.request("create", {
            "id": self._world_id,
            "cfg": self.cfg,
            "timezone": self.timezone,
            "botName": self.bot_name,
            "dataDir": self.data_dir,
            "secrets": self.secrets,
        }, timeout=max(self._timeout, 60.0))
        return hello

    async def start_world(self, host: WorldHost) -> None:
        """调用 TS World 的 start(host)——host 能力已通过回调暴露给 Node。"""
        await self.request("start", timeout=max(self._timeout, 60.0))

    async def stop_world(self) -> None:
        try:
            await self.request("stop", timeout=15.0)
        except Exception as e:
            print(f"[CORTICO] World {self.manifest.name} stop 失败: {e}")

    async def close(self) -> None:
        """关闭 Node 子进程。"""
        try:
            await self.request("shutdown", timeout=5.0)
        except Exception:
            pass
        await self._kill()

    async def _kill(self) -> None:
        if self._reader_task:
            self._reader_task.cancel()
            self._reader_task = None
        if self._proc and self._proc.returncode is None:
            try:
                self._proc.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=5.0)
            except (asyncio.TimeoutError, Exception):
                try:
                    self._proc.kill()
                except Exception:
                    pass
        self._proc = None

    # ---- RPC ----
    def _send(self, msg: Dict[str, Any]) -> None:
        if not self._proc or self._proc.stdin is None:
            raise BridgeError("Node 子进程未启动")
        try:
            self._proc.stdin.write((json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8"))
        except Exception as e:
            raise BridgeError(f"写入 Node 失败: {e}")

    async def request(self, method: str, params: Optional[Dict[str, Any]] = None,
                      timeout: Optional[float] = None) -> Any:
        if not self._proc:
            raise BridgeError("Node 子进程未启动")
        self._seq += 1
        rid = self._seq
        fut = asyncio.get_event_loop().create_future()
        self._pending[rid] = fut
        self._send({"id": rid, "method": method, "params": params or {}})
        try:
            return await asyncio.wait_for(fut, timeout=timeout or self._timeout)
        except asyncio.TimeoutError:
            self._pending.pop(rid, None)
            raise BridgeError(f"{method} 超时（{timeout or self._timeout}s）")

    async def _read_loop(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        try:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                await self._dispatch(msg)
        except asyncio.CancelledError:
            return
        except Exception as e:
            print(f"[CORTICO] Node 读取循环异常: {e}")
        finally:
            for fut in list(self._pending.values()):
                if not fut.done():
                    fut.set_exception(BridgeError("Node 子进程已退出"))
            self._pending.clear()

    async def _drain_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        try:
            while True:
                line = await proc.stderr.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip()
                if not text:
                    continue
                self._stderr_tail.append(text)
                if len(self._stderr_tail) > 50:
                    self._stderr_tail.pop(0)
                print(f"[CORTICO:{self.manifest.name}] {text}")
        except (asyncio.CancelledError, Exception):
            return

    def stderr_tail(self, n: int = 5) -> str:
        return " | ".join(self._stderr_tail[-n:])

    async def _dispatch(self, msg: Dict[str, Any]) -> None:
        """处理来自 Node 的应答 / 回调请求。"""
        if "id" in msg and ("result" in msg or "error" in msg) and "method" not in msg:
            fut = self._pending.pop(int(msg["id"]), None)
            if fut is None or fut.done():
                return
            if "error" in msg:
                fut.set_exception(BridgeError(str(msg["error"])))
            else:
                fut.set_result(msg.get("result"))
            return

        method = str(msg.get("method") or "")
        if not method:
            return
        params = msg.get("params") or {}
        rid = msg.get("id")
        cb = self.host_callbacks.get(method)
        if cb is None:
            if rid is not None:
                self._send({"id": rid, "error": f"宿主未实现 {method}"})
            return
        try:
            result = cb(params)
            if asyncio.iscoroutine(result) or isinstance(result, asyncio.Future):
                result = await result
        except Exception as e:
            if rid is not None:
                self._send({"id": rid, "error": str(e)})
            else:
                print(f"[CORTICO] 宿主回调 {method} 失败: {e}")
            return
        if rid is not None:
            self._send({"id": rid, "result": result if result is not None else None})

    # ---- World 能力 ----
    @property
    def id(self) -> str:
        return self._world_id or self.manifest.name

    async def tools(self) -> List[ToolDef]:
        raw = await self.request("tools", timeout=15.0)
        out: List[ToolDef] = []
        for t in (raw or []):
            name = str(t.get("name") or "")
            if not name:
                continue
            out.append(ToolDef(
                name=name,
                description=str(t.get("description") or ""),
                parameters=dict(t.get("parameters") or {"type": "object", "properties": {}}),
                tags=list(t.get("tags") or []),
                barrier_after=bool(t.get("barrierAfter")),
                ends_turn=bool(t.get("endsTurn")),
                handler=lambda args, ctx, _n=name: self.call_tool(_n, args, ctx),
            ))
        return out

    async def call_tool(self, name: str, args: Dict[str, Any],
                        ctx: Optional[ToolCallContext] = None) -> ToolOutcome:
        ctx = ctx or ToolCallContext()
        res = await self.request("callTool", {
            "name": name,
            "args": dict(args or {}),
            "role": ctx.role,
            "callId": ctx.call_id,
            "round": ctx.round,
        }, timeout=max(self._timeout, 60.0))
        return ToolOutcome.coerce(res)

    async def env_prompt_vars(self) -> Optional[Dict[str, str]]:
        v = await self.request("envPromptVars", timeout=15.0)
        if v is None:
            return None
        return {str(k): str(val) for k, val in (v or {}).items()}

    async def console(self, language: str = "zh") -> Optional[WorldConsoleDecl]:
        raw = await self.request("console", {"language": language}, timeout=15.0)
        if not raw:
            return None
        decl = WorldConsoleDecl.coerce(raw)
        if raw.get("hasInvoke"):
            decl.invoke = lambda panel, method, args: self.request(
                "invoke", {"panel": panel, "method": method, "args": list(args or []),
                           "language": language}, timeout=30.0)
        return decl

    async def defaults(self) -> Dict[str, Any]:
        return dict(await self.request("defaults", timeout=15.0) or {})

    def alive(self) -> bool:
        return bool(self._proc and self._proc.returncode is None)


def _cfg():
    import config
    return config


class NodeWorld(World):
    """把 Node 子进程里的 TS World 包装成 Python `World` 契约对象。"""

    def __init__(self, bridge: NodeWorldBridge, fallback_id: str = ""):
        self.bridge = bridge
        self.id = bridge.id or fallback_id
        self._tools: List[ToolDef] = []
        self._console: Optional[WorldConsoleDecl] = None

    async def _prepare(self) -> None:
        self.id = self.bridge.id or self.id
        self._tools = await self.bridge.tools()
        try:
            self._console = await self.bridge.console("zh")
        except Exception as e:
            print(f"[CORTICO] console() 获取失败: {e}")
            self._console = None

    async def start(self, host: WorldHost) -> None:
        await self.bridge.start_world(host)

    async def stop(self) -> None:
        await self.bridge.stop_world()

    def tools(self) -> List[ToolDef]:
        return list(self._tools)

    def env_prompt_vars(self) -> Optional[Dict[str, str]]:
        """同步接口：取最近一次缓存值；首次由装配层用 refresh_vars() 异步拉取。"""
        return getattr(self, "_vars_cache", None)

    async def refresh_vars(self) -> Optional[Dict[str, str]]:
        v = await self.bridge.env_prompt_vars()
        self._vars_cache = v
        return v

    def console(self, language: str = "zh") -> Optional[WorldConsoleDecl]:
        return self._console
