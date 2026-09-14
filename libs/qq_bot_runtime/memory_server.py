# -*- coding: utf-8 -*-
"""记忆 sidecar：用通用 RPC 骨架托管 vector_memory 等重负载模块。

把 `vector_memory` 这类「加载重模型 + 阻塞式 IO」的模块从 bot 主进程剥离到独立进程，
主进程经 `memory_client` 走本地 HTTP 调用；进程崩溃/掉线会自动降级回进程内调用，
因此本服务不启动 bot 也照常工作（零破坏）。

启动：
    python memory_server.py [--host 127.0.0.1] [--port 8766] [--modules vector_memory]
或（bot 启动时由 main.py 按 ENABLE_MEMORY_SERVER 自动拉起）。
"""
from service_host import serve, read_args

DEFAULT_MODULES = ["vector_memory"]

# 向量库首次调用要懒加载 embedding 模型（sentence-transformers），阻塞时间远超通用默认值，
# 故显式放大单调用超时（骨架默认 120s）。
RPC_TIMEOUT = 600


def main():
    args = read_args(DEFAULT_MODULES)
    timeout = RPC_TIMEOUT if args.timeout is None else args.timeout
    serve(args.modules.split(","), args.host, args.port, banner="memory", timeout=timeout)


if __name__ == "__main__":
    main()
