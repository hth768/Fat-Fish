"""与新版 cortico-world-qq-better 对齐项的单元测试。

覆盖：
- reminder：时间解析（绝对/今天明天/相对/中文）与格式化
- qq_guard：群聊发言频率限制 + 话题自动结束（防死循环）
- chat_service.parse_memory_output：新增的【好感】【提醒】两类解析
- qq_plugin.parse_reply_segments：系统表情标记（全角/半角冒号）
"""
import datetime
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
ENGINE = os.path.join(ROOT, "libs", "qq_bot_runtime")
if ENGINE not in sys.path:
    sys.path.insert(0, ENGINE)

import config
import libs.qq_bot_runtime.reminder as reminder
import libs.qq_bot_runtime.qq_guard as qq_guard
from libs.qq_bot_runtime.chat_service import parse_memory_output

try:
    import libs.qq_bot_runtime.qq_plugin as qq_plugin
except Exception:  # 缺 websockets/httpx 时跳过表情解析用例
    qq_plugin = None


class TestReminderParse(unittest.TestCase):
    def setUp(self):
        self.now = datetime.datetime(2026, 9, 29, 10, 0)

    def _fmt(self, s):
        ts = reminder.parse_when(s, self.now)
        return reminder.format_when(ts, self.now) if ts else None

    def test_absolute_and_clock(self):
        self.assertEqual(self._fmt("15:00"), "今天 15:00")
        self.assertEqual(self._fmt("2026-09-30T09:00"), "明天 09:00")

    def test_relative_chinese_units(self):
        self.assertEqual(self._fmt("30分钟后"), "今天 10:30")
        self.assertEqual(self._fmt("2小时后"), "今天 12:00")
        self.assertEqual(self._fmt("3天后"), "10月2日 10:00")
        self.assertEqual(self._fmt("in 30m"), "今天 10:30")

    def test_cn_hour_and_tomorrow(self):
        self.assertEqual(self._fmt("明天9点"), "明天 09:00")
        self.assertEqual(self._fmt("后天8点"), "10月1日 08:00")

    def test_unparsable(self):
        self.assertEqual(reminder.parse_when("随便写点啥", self.now), 0.0)


class TestQQGuard(unittest.TestCase):
    def setUp(self):
        self._saved = {k: getattr(config, k, None) for k in (
            "QQ_GROUPSPEAK_ENABLED", "QQ_GROUPSPEAK_MAX_PER_WINDOW", "QQ_GROUPSPEAK_WINDOW_SEC",
            "QQ_ANTILOOP_ENABLED", "QQ_ANTILOOP_MAX_BOT_TURNS", "QQ_ANTILOOP_IDLE_TO_END_SEC")}
        qq_guard.reset()

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(config, k, v)
        qq_guard.reset()

    def test_group_rate_limit(self):
        config.QQ_GROUPSPEAK_ENABLED = True
        config.QQ_GROUPSPEAK_MAX_PER_WINDOW = 2
        config.QQ_GROUPSPEAK_WINDOW_SEC = 60
        config.QQ_ANTILOOP_ENABLED = False
        qq_guard.note_incoming("group", "g1")
        self.assertTrue(qq_guard.allow_outgoing("group", "g1")[0])
        qq_guard.note_outgoing("group", "g1")
        self.assertTrue(qq_guard.allow_outgoing("group", "g1")[0])
        qq_guard.note_outgoing("group", "g1")
        ok, why = qq_guard.allow_outgoing("group", "g1")
        self.assertFalse(ok)
        self.assertIn("上限", why)

    def test_anti_loop_resets_on_user_message(self):
        config.QQ_GROUPSPEAK_ENABLED = False
        config.QQ_ANTILOOP_ENABLED = True
        config.QQ_ANTILOOP_MAX_BOT_TURNS = 2
        config.QQ_ANTILOOP_IDLE_TO_END_SEC = 0
        qq_guard.note_incoming("private", "u1")
        qq_guard.note_outgoing("private", "u1")
        qq_guard.note_outgoing("private", "u1")
        ok, why = qq_guard.allow_outgoing("private", "u1")
        self.assertFalse(ok)
        self.assertIn("收尾", why)
        # 对方一开口就清零，可以继续说
        qq_guard.note_incoming("private", "u1")
        self.assertTrue(qq_guard.allow_outgoing("private", "u1")[0])


class TestNotebook(unittest.TestCase):
    """私人笔记本（对齐新版 notebook）：记 / 列表 / 查 / 忘 + 落盘。"""

    def setUp(self):
        import libs.qq_bot_runtime.notebook as nb
        self.nb = nb
        self._path = nb._store_file()
        self._backup = None
        if os.path.exists(self._path):
            with open(self._path, "r", encoding="utf-8") as f:
                self._backup = f.read()
            os.remove(self._path)

    def tearDown(self):
        p = self._path
        if os.path.exists(p):
            os.remove(p)
        if self._backup is not None:
            with open(p, "w", encoding="utf-8") as f:
                f.write(self._backup)

    def test_save_list_get_forget(self):
        e = self.nb.save("测试笔记一条", source="ut")
        self.assertTrue(e.get("id"))
        self.assertEqual(self.nb.count(), 1)
        self.assertEqual(self.nb.get(e["id"])["text"], "测试笔记一条")
        self.assertIn("测试笔记一条", self.nb.list_text())
        self.assertIn("测试笔记一条", self.nb.tail_text(5))
        self.assertTrue(self.nb.forget(e["id"]))
        self.assertEqual(self.nb.count(), 0)

    def test_save_dedup(self):
        a = self.nb.save("重复的话", source="ut")
        b = self.nb.save("重复的话", source="ut")
        self.assertEqual(a["id"], b["id"])
        self.assertEqual(self.nb.count(), 1)


class TestVoiceRuntime(unittest.TestCase):
    """语音运行时开关（对齐新版 voice.asr / tts / semanticJudge 命令）。"""

    def setUp(self):
        import libs.qq_bot_runtime.voice_client as vc
        self.vc = vc
        self._saved = dict(vc._RUNTIME)
        for k in vc._RUNTIME:
            vc._RUNTIME[k] = None

    def tearDown(self):
        for k, v in self._saved.items():
            self.vc._RUNTIME[k] = v

    def test_toggle_priority_over_config(self):
        self.assertTrue(self.vc.voice_enabled())
        self.vc.set_voice_runtime("enabled", False)
        self.assertFalse(self.vc.voice_enabled())
        self.assertFalse(self.vc.voice_asr_enabled())
        self.assertFalse(self.vc.voice_tts_enabled())
        self.vc.set_voice_runtime("enabled", None)
        self.assertTrue(self.vc.voice_enabled())

    def test_asr_tts_independent(self):
        self.vc.set_voice_runtime("asr", False)
        self.assertFalse(self.vc.voice_asr_enabled())
        self.assertTrue(self.vc.voice_tts_enabled())
        self.vc.set_voice_runtime("tts", False)
        self.assertFalse(self.vc.voice_tts_enabled())

    def test_status_text(self):
        text = self.vc.voice_status_text()
        self.assertIn("听语音(ASR)", text)
        self.assertIn("说语音(TTS)", text)


class TestMemoryParse(unittest.TestCase):
    def test_affinity_and_reminder_sections(self):
        out = (
            "【人物档案】\n用户叫小明\n"
            "【重要信息】\n约定|明天开会\n"
            "【心情】\n心情|开心|2|被夸了\n"
            "【好感】\n好感|+5|聊得很开心\n"
            "【提醒】\n提醒|明天9点|开会\n"
        )
        r = parse_memory_output(out, include_mood=True, include_affinity=True, include_reminder=True)
        self.assertIn("用户叫小明", r["facts"])
        self.assertEqual(r["mood"]["emotion"], "开心")
        self.assertEqual(r["affinity"]["delta"], 5)
        self.assertEqual(r["reminder"]["when_text"], "明天9点")
        self.assertEqual(r["reminder"]["text"], "开会")

    def test_no_new_sections_when_disabled(self):
        out = "【人物档案】\n用户喜欢猫\n"
        r = parse_memory_output(out)
        self.assertIsNone(r["affinity"])
        self.assertIsNone(r["reminder"])
        self.assertEqual(r["notes_self"], [])

    def test_notebook_section(self):
        out = (
            "【重要信息】\n约定|明天开会\n"
            "【笔记】\n刚才那个比喻挺有意思，回头能用\n想试试做个新的表情包\n"
        )
        r = parse_memory_output(out, include_notebook=True)
        self.assertEqual(len(r["notes"]), 1)
        self.assertEqual(len(r["notes_self"]), 2)
        self.assertIn("刚才那个比喻", r["notes_self"][0])

    def test_notebook_title_not_eaten_by_other_section(self):
        out = "【笔记】\n无\n【重要信息】\n约定|明天开会\n"
        r = parse_memory_output(out, include_notebook=True)
        self.assertEqual(r["notes_self"], [])
        self.assertEqual(len(r["notes"]), 1)


@unittest.skipIf(qq_plugin is None, "qq_plugin 依赖缺失（websockets/httpx）")
class TestSystemFaceSegment(unittest.TestCase):
    def test_fullwidth_colon_face(self):
        segs = qq_plugin.parse_reply_segments("给你个[表情22：白眼]")
        self.assertIn({"type": "face", "data": {"id": "22"}}, segs)

    def test_halfwidth_colon_face(self):
        segs = qq_plugin.parse_reply_segments("给你个[表情22:白眼]")
        self.assertIn({"type": "face", "data": {"id": "22"}}, segs)

    def test_named_face_still_works(self):
        segs = qq_plugin.parse_reply_segments("[旺柴]")
        self.assertIn({"type": "face", "data": {"id": "210"}}, segs)


if __name__ == "__main__":
    unittest.main()
