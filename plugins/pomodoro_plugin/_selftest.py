# -*- coding: utf-8 -*-
"""自检脚本：校验 manifest 与 plugin.py 的契约（可删）。

运行：python plugins/pomodoro_plugin/_selftest.py
只依赖标准库，不需要 qq_bot 环境（plugin.py 内的 import 已做降级）。
"""
import importlib.util
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))

# 1) manifest 合法 JSON 且字段齐全
meta = json.load(open(os.path.join(HERE, "manifest.json"), encoding="utf-8"))
assert meta["name"] == "pomodoro_plugin"
assert meta["kind"] in ("platform", "feature", "brain", "sidecar", "local", "world")
assert isinstance(meta.get("config_schema"), list) and meta["config_schema"]
assert all(isinstance(it, dict) and it.get("key") for it in meta["config_schema"])
print("[OK] manifest.json valid:", meta["name"], meta["version"], meta["kind"])

# 2) plugin.py 可加载且暴露 create_plugin / on_config
spec = importlib.util.spec_from_file_location("pomodoro_plugin", os.path.join(HERE, "plugin.py"))
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
mod.on_config({"work_minutes": 1, "short_break_minutes": 1, "long_break_minutes": 1,
               "cycles_before_long": 2, "daily_goal": 3, "quiet_hours": ""})
assert inst._cfg["work_minutes"] == 1
assert inst._cfg["cycles_before_long"] == 2
assert inst._cfg["daily_goal"] == 3
print("[OK] config applied:", inst._cfg)

# 非法值应回退默认，不抛异常
mod.on_config({"work_minutes": "abc", "daily_goal": -5})
assert inst._cfg["work_minutes"] == 25, "非法值应回退默认"
assert inst._cfg["daily_goal"] == 0
print("[OK] config robustness passed")

# 免打扰解析
assert mod._parse_quiet("23:00-07:00") == (1380, 420)
assert mod._parse_quiet("") is None
assert mod._parse_quiet("bad") is None
print("[OK] quiet-hours parsing passed")

# 相位推进（不启动循环，直接驱动状态机）
inst._phase = "work"
inst._advance()
assert inst._done == 1 and inst._phase == "short_break", inst._phase
inst._advance()   # short_break -> work
assert inst._phase == "work"
inst._advance()   # 第 2 段完成 -> 长休
assert inst._done == 2 and inst._phase == "long_break", inst._phase
print("[OK] phase machine passed:", inst.status())

# daily_goal=3 时：再完成一段应停机
inst._phase = "work"
inst._advance()
assert inst._done == 3 and inst._phase == "idle", inst._phase
assert any("目标达成" in (e.get("text") or "") for e in core.app_bridge.events)
print("[OK] daily goal reached -> idle")

print("ALL OK")
