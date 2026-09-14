# -*- coding: utf-8 -*-
"""兼容入口（原 QQ 机器人主程序）。

原 bot.py 的全部逻辑已按「智能体为核心、QQ 降级为插件」完成架构迁移：
- 聊天大脑（命令/记忆/意图/语音/视频/游戏注入） -> chat_service.py
- QQ 传输（NapCat WebSocket / OneBot 协议 / 表情码 / 媒体抓取 / 私聊聚合） -> qq_plugin.py
- 插件系统 -> plugin_base.py；终端聊天 -> console_plugin.py
- 核心生命周期 -> agent_core.py；统一入口 -> main.py

本文件只保留向后兼容：python bot.py 等价于 python main.py（平台按 config.py
的 ENABLE_* 开关启动）。旧版强制 --qq 会无视 config 把 B 站等平台挡在门外，
已改为配置驱动。
"""
import asyncio

from main import main

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("[INFO] 已退出")
