# -*- coding: utf-8 -*-
"""B 站每日定时自主学习任务（移植 BLB 的「自己看视频→总结→沉淀→分享」）。

与 /b站看 命令的区别：这里是**无人触发**的定时任务。
- 按 config.BILIBILI_LEARN_KEYWORDS 给出的「方向关键词」自行搜索相关视频
  （也可同时用 BILIBILI_LEARN_UP_UIDS 指定必看的 UP 主，二者可选其一或并用）；
- 用 bili_learn.summarize 看 + 总结，knowledge_service 沉淀进知识库；
- 按 config.BILIBILI_LEARN_SHARE 把「今日学习」分享出去（默认发给白名单私信 UID）；
- 每天 10-15 条，铺在 BILIBILI_LEARN_START_HOUR~END_HOUR 之间，按间隔发送；
- 已学过的 bvid 写入本地 json 去重，跨天不重复。

总开关：config.ENABLE_BILIBILI_LEARN_SCHEDULE（默认 False）。
"""
import asyncio
import json
import os
import random
import time

import config
from bili_api import BiliSession, has_cookies, fetch_up_videos, fetch_search_videos
from bili_dm import send_bili_dm, _split_text
from plugin_base import FeaturePlugin

_STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "bili_learned.json")


def _load_learned() -> dict:
    try:
        with open(_STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"date": "", "bvids": []}


def _save_learned(state: dict):
    os.makedirs(os.path.dirname(_STATE_FILE), exist_ok=True)
    with open(_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)


class BilibiliLearnScheduler(FeaturePlugin):
    """每日定时从 UP 主列表拉视频、学习并分享。"""

    name = "bilibili_learn_schedule"

    def __init__(self, core):
        super().__init__(core)
        self._task = None
        self._session = BiliSession()
        self._http = None
        self._state = _load_learned()
        self._day = time.strftime("%Y-%m-%d")
        self._count_today = 0
        self._queue = []
        self._last_proc = 0.0

    async def start(self):
        if not getattr(config, "ENABLE_BILIBILI_LEARN_SCHEDULE", False):
            return
        if not has_cookies():
            print("[BILI_LEARN_SCH] 未配置 BILIBILI_SESSDATA/CSRF，定时学习不可用")
            return
        self._http = __import__("httpx").AsyncClient(
            timeout=20,
            headers={"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                                    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")})
        try:
            await self._session.ensure_buvid(self._http)
        except Exception as e:
            print("[BILI_LEARN_SCH] buvid 初始化警告: %s" % e)
        ups = [str(u) for u in getattr(config, "BILIBILI_LEARN_UP_UIDS", []) or []]
        kws = [str(k).strip() for k in getattr(config, "BILIBILI_LEARN_KEYWORDS", []) or [] if str(k).strip()]
        if not ups and not kws:
            print("[BILI_LEARN_SCH] BILIBILI_LEARN_UP_UIDS / BILIBILI_LEARN_KEYWORDS 均为空：未配置学习方向，定时任务空闲（请填写）")
        else:
            print("[BILI_LEARN_SCH] 监控 UP 主：%s；学习方向关键词：%s"
                  % (", ".join(ups) if ups else "（无）", ", ".join(kws) if kws else "（无）"))
        self._task = asyncio.get_event_loop().create_task(self._loop())
        await super().start()

    async def stop(self):
        if self._task:
            self._task.cancel()
            self._task = None
        if self._http is not None:
            try:
                await self._http.aclose()
            except Exception:
                pass
            self._http = None
        await super().stop()

    # ---------------- 主循环 ----------------
    async def _loop(self):
        try:
            while True:
                await asyncio.sleep(60)
                await self._tick()
        except asyncio.CancelledError:
            pass

    async def _tick(self):
        try:
            now = time.localtime()
            today = time.strftime("%Y-%m-%d")
            # 跨天重置
            if today != self._day:
                self._day = today
                self._count_today = 0
                self._queue = []
                self._state = {"date": today, "bvids": []}
            # 活跃时段外不处理
            start_h = int(getattr(config, "BILIBILI_LEARN_START_HOUR", 9))
            end_h = int(getattr(config, "BILIBILI_LEARN_END_HOUR", 23))
            if not (start_h <= now.tm_hour < end_h):
                return
            daily = int(getattr(config, "BILIBILI_LEARN_DAILY_COUNT", 12))
            if self._count_today >= daily:
                return
            # 补充队列
            if not self._queue:
                self._queue = await self._collect(daily - self._count_today)
                if not self._queue:
                    return
            # 间隔控制：把一天的量铺开
            interval = max(10, int(getattr(config, "BILIBILI_LEARN_INTERVAL_MIN", 75)) * 60)
            if time.time() - self._last_proc < interval and self._count_today > 0:
                return
            bvid = self._queue.pop(0)
            if bvid in self._state.get("bvids", []):
                return
            await self._learn_one(bvid)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print("[BILI_LEARN_SCH] tick 异常: %s" % e)

    async def _collect(self, need: int) -> list:
        """按「方向关键词 + 可选 UP 主」收集未学过的 bvid（去重、乱序）。

        方向优先：每个关键词搜索一轮，再可选地把指定 UP 的最新投稿并入；
        最终打乱顺序，避免每天顺序雷同。
        """
        ups = [str(u) for u in getattr(config, "BILIBILI_LEARN_UP_UIDS", []) or []]
        kws = [str(k).strip() for k in getattr(config, "BILIBILI_LEARN_KEYWORDS", []) or [] if str(k).strip()]
        learned = set(self._state.get("bvids", []))
        got = []
        # 方向：按关键词搜索
        for kw in kws:
            try:
                bvids = await fetch_search_videos(self._http, self._session, kw, ps=20)
            except Exception as e:
                print("[BILI_LEARN_SCH] 搜索关键词「%s」失败: %s" % (kw, e))
                continue
            for b in bvids:
                if b and b not in learned and b not in got:
                    got.append(b)
        # 可选：指定 UP 主最新投稿
        for uid in ups:
            try:
                bvids = await fetch_up_videos(self._http, self._session, uid, ps=30)
            except Exception as e:
                print("[BILI_LEARN_SCH] 拉 UP %s 视频失败: %s" % (uid, e))
                continue
            for b in bvids:
                if b and b not in learned and b not in got:
                    got.append(b)
        random.shuffle(got)
        return got[:need]

    async def _learn_one(self, bvid: str):
        from bili_learn import summarize
        import knowledge_service
        try:
            res = await summarize(bvid)
        except Exception as e:
            print("[BILI_LEARN_SCH] 总结 %s 失败: %s" % (bvid, e))
            return
        title = res.get("title", "")
        summary = res.get("summary", "")
        # 沉淀进知识库
        try:
            knowledge_service.get_knowledge().learn_async(title, summary)
        except Exception as e:
            print("[BILI_LEARN_SCH] 知识沉淀失败（不影响分享）: %s" % e)
        print("[BILI_LEARN_SCH] 已学习《%s》" % title)
        # 记录去重（无论是否分享都记，避免重复学）
        self._state.setdefault("bvids", []).append(bvid)
        _save_learned(self._state)
        self._count_today += 1
        self._last_proc = time.time()
        # 分享
        share = str(getattr(config, "BILIBILI_LEARN_SHARE", "dm")).lower()
        if share in ("", "none"):
            return
        targets = [str(u) for u in (getattr(config, "BILIBILI_LEARN_SHARE_UIDS", []) or [])]
        if not targets:
            targets = [str(u) for u in getattr(config, "BILIBILI_DM_WHITELIST", []) or []]
        if not targets:
            return
        text = "【今日学习】%s\n%s" % (title, summary)
        for uid in targets:
            try:
                for part in _split_text(text, getattr(config, "BILIBILI_DM_MAX_CHARS", 500)):
                    await send_bili_dm(uid, part)
            except Exception as e:
                print("[BILI_LEARN_SCH] 分享给 %s 失败: %s" % (uid, e))
