# -*- coding: utf-8 -*-
"""Cortico 协议兼容层（api=5）。

让 feiyu（Python）与 Cortico（TypeScript）**双架构兼容**：
- 协议层：本包是 Cortico `api=5` 扩展契约的 Python 镜像。按契约写的 Python World
  可直接挂在 feiyu 上跑；Cortico 的 `cortico-world-*` 包能被 feiyu 识别（清单、配置组、
  环境提示词、工具声明）。
- 运行时桥：TS 包经 Node 子进程（JSON-RPC over stdio）真跑起来，宿主能力由 Python 提供。

三层职责：
    types.py      契约类型（纯数据，零依赖）
    host.py       WorldHost 实现：事件落库 + 投递 + 模型能力查询
    registry.py   装配层：挂载/卸载/重启、工具撞名检查（对齐 Cortico WorldAssembly）
    node_bridge   Node 子进程桥（跑 TS World）
    brain.py      自主大脑：世界事件 -> 唤醒 -> 模型 -> 工具 -> 输出

配置见 `config.py` 的 `CORTICO_*` 段。设计参考：
    D:\\testing\\Cortico\\src\\world.ts          装配层
    D:\\testing\\Cortico\\src\\core\\types.ts     World / WorldHost / ToolDef / EventEnvelope
"""
from .types import API_VERSION  # noqa: F401

__all__ = ["API_VERSION"]

__version__ = "1.0"
