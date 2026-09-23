# -*- coding: utf-8 -*-
"""临时自检脚本：校验 manifest JSON 与 plugin.py 导入/契约（可删）。"""
import importlib.util
import json
import os
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))

# 1) manifest 合法 JSON 且字段齐全
meta = json.load(open(os.path.join(HERE, "manifest.json"), encoding="utf-8"))
assert meta["name"] == "reminder_plugin"
assert meta["kind"] in ("platform", "feature", "brain", "sidecar", "local", "world")
assert isinstance(meta.get("config_schema"), list)
print("[OK] manifest.json valid:", meta["name"], meta["version"], meta["kind"])

# 2) plugin.py 可加载且暴露 create_plugin / on_config
spec = importlib.util.spec_from_file_location("reminder_plugin", os.path.join(HERE, "plugin.py"))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
assert hasattr(mod, "create_plugin"), "missing create_plugin"
assert hasattr(mod, "on_config"), "missing on_config"


# 3) 用 mock core 走一遍契约
class _Bridge:
    def __init__(self):
        self.events = []

    def push(self, session, event):
        self.events.append(event)


class _Core:
    def __init__(self):
        self.app_bridge = _Bridge()


core = _Core()
inst = mod.create_plugin(core)
mod.on_config({"reminders": '[{"text":"测试","at":"08:30"},{"text":"间隔","every":1},'
                            '{"text":"一次性","delay":1},{"text":"叫起床","once_at":"07:00","task":"闹钟"}]',
               "check_interval": "5", "quiet_hours": ""})
print("[OK] reminders parsed:", len(inst._reminders), inst._reminders)
assert len(inst._reminders) == 4
assert inst._cfg["check_interval"] == 5
assert inst._reminders[3].get("task") == "闹钟"
assert isinstance(inst._reminders[3].get("once_at"), float)

# 解析坏 JSON 不应清空旧清单
mod.on_config({"reminders": "{bad json"})
assert len(inst._reminders) == 4, "坏 JSON 不应清空"
# 全部项非法（once_at 解析失败）→ 保留旧清单，避免误清空
mod.on_config({"reminders": '[{"text":"x","once_at":"不是时间"}]'})
assert len(inst._reminders) == 4, "非法 once_at 应保留旧清单"
# 显式清空
mod.on_config({"reminders": "[]"})
assert len(inst._reminders) == 0
print("[OK] config robustness passed")

# 4) once_at 解析：HH:MM 顺延到未来；绝对时间可解析；垃圾返回 None
ts = mod._parse_when("07:00")
assert isinstance(ts, float) and ts > time.time(), "HH:MM 应顺延到未来"
assert mod._parse_when("2026-09-21 07:00") is not None
assert mod._parse_when("garbage") is None
print("[OK] _parse_when passed")

# 5) 免打扰时段（含跨天）
inst._cfg["quiet_hours"] = "23:00-07:00"
assert inst._in_quiet(datetime(2026, 9, 21, 23, 30)) is True
assert inst._in_quiet(datetime(2026, 9, 21, 6, 0)) is True
assert inst._in_quiet(datetime(2026, 9, 21, 12, 0)) is False
inst._cfg["quiet_hours"] = ""
assert inst._in_quiet(datetime(2026, 9, 21, 12, 0)) is False
print("[OK] quiet hours passed")

print("ALL OK")
