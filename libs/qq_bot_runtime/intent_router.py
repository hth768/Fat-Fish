# -*- coding: utf-8 -*-
"""自然语言游戏意图识别与执行。

把用户的"正常聊天"转换成游戏操作意图，让 AI 不用斜杠命令也能执行动作。

例如：
- "去看看前面" -> 前进/转向
- "开门" -> interact
- "吃个东西" -> 使用食物
- "整理背包" -> 整理
- "挖点矿" -> 挖矿
- "做个剑" -> 合成

流程：
1. 关键词初筛（快速，低开销）
2. 命中意图 -> 返回 (intent_type, params)
3. 执行 -> 调用 mc_watcher 对应方法

保留斜杠命令作为兼容（bot.py 现有逻辑不变），本模块只在普通聊天时启用。
"""
import asyncio
import re
import time
from typing import Dict, List, Optional, Tuple
from quiet import degrade

try:
    from mc_watcher import mc_watcher
except Exception:  # MC 大脑已随插件分发（brain_mc_mod），未安装时游戏意图自然离线
    mc_watcher = None

# 是否启用自然语言意图
_ENABLED = True

# Minecraft 在线状态探测缓存（30 秒），避免每条消息都打一次 HTTP
_game_online_cache = {"ts": 0.0, "ok": False}


def _game_online() -> bool:
    """FeiyuAPI mod 接口是否在线（带 30 秒缓存）。

    游戏不在线时所有游戏意图都无从执行，识别层直接短路，
    也省掉每条普通聊天消息一次 AI 意图判断的开销。
    """
    if mc_watcher is None:  # MC 插件未安装：游戏意图整体离线
        return False
    now = time.time()
    if now - _game_online_cache["ts"] < 30:
        return _game_online_cache["ok"]
    ok = mc_watcher.is_http_api_up()
    _game_online_cache["ts"] = now
    _game_online_cache["ok"] = ok
    return ok

# 意图关键词规则：意图类型 -> 触发关键词
_INTENT_RULES: Dict[str, List[str]] = {
    "move_forward": ["往前走", "向前走", "前进", "走走", "往前", "走几步", "去看看", "去那边", "去前面", "朝前", "向前"],
    "move_back": ["往后退", "向后走", "后退", "往回走", "退后", "撤退", "往后"],
    "move_left": ["往左走", "向左", "左转走", "往左边", "靠左"],
    "move_right": ["往右走", "向右", "右转走", "往右边", "靠右"],
    "turn_look": ["看看", "望向", "看向", "朝那看", "转身看看", "往那边看", "回头看看", "环顾四周", "四周看看", "瞧一眼", "望望"],
    "interact": ["开门", "开箱子", "开箱", "交互", "点一下", "按一下", "开个门", "按开关", "拉闸", "打开箱子"],
    "eat": ["吃东西", "吃个", "吃饭", "吃点", "吃食物", "饿了", "吃口", "补充体力", "回血", "吃点东西"],
    "use_item": ["使用物品", "用一下", "用个东西", "射箭", "用弓箭", "射一箭", "用火把", "用工具"],
    "organize": ["整理背包", "收拾背包", "整理一下", "收拾一下", "整理物品", "背包乱", "背包快满", "没地方放了", "腾背包"],
    "dig": ["挖矿", "挖点矿", "挖矿石", "采矿", "挖一下", "挖个", "挖地道", "挖隧道", "采点矿", "挖矿去", "挖点铁", "挖点煤", "挖点石头", "去挖", "帮我挖"],
    "craft": ["合成", "做个", "做一把", "做点东西", "造个", "造东西", "制作", "弄个", "造一把", "做几个"],
    "auto_play": ["自己玩", "自动玩", "你玩吧", "自主玩", "你去玩", "托管", "自己看着办", "随便逛", "你决定吧", "你来玩"],
    "stop_auto": ["停下", "别玩了", "停手", "暂停", "别动了", "站住", "停下来"],
    "show_state": ["看看状态", "你状态", "现在怎么样", "你在哪", "位置", "你在干嘛", "在哪呢", "在做什么"],
    "query_time": ["现在几点", "几点了", "什么时间", "现在时间", "几号", "今天几号", "现在日期", "星期几", "今天星期", "现在什么时候"],
    "escape": ["快跑", "跑路", "快逃", "跑开", "逃跑", "赶紧跑", "躲开", "避开"],
    "explore": ["探索", "探查", "打探", "侦察", "搜一搜", "探索一下", "看看周围有什么"],
    "chop_tree": ["砍树", "砍点树", "伐木", "砍木头", "弄点木头", "砍几棵树"],
    "check_danger": ["有危险吗", "危险吗", "安全吗", "有怪物吗", "附近有怪吗", "周围安全吗", "前面有危险", "附近安全", "安全不", "看看安全"],
    "go_home": ["回家", "回基地", "回基地", "回住处", "回去"],
    "pickup": ["捡东西", "拾取", "捡起来", "收拾战利品", "捡掉落物", "捡一下", "捡起", "收起来"],
    "build": ["盖房子", "搭房子", "建房子", "建造", "搭个房子", "盖个屋", "建个家", "搭围栏", "盖围栏", "修个房子", "盖个房子", "盖个", "搭个", "建个"],
    "plant": ["种地", "种小麦", "种树", "种个", "种植", "种点东西", "种田", "耕田", "种棵树", "种点", "种种"],
    "fish": ["钓鱼", "钓个鱼", "去钓鱼", "钓钓鱼", "钓一下", "去钓"],
    "smelt": ["烧炼", "烧矿", "熔炼", "烧铁", "炼铁", "烧个矿", "烧东西", "用熔炉", "烧一下", "烧个", "炼一下"],
    "equip": ["换装备", "拿武器", "拿剑", "拿工具", "装备", "换上", "掏武器", "拿个武器", "换把剑", "拿把", "掏出", "换上武器"],
    "sleep": ["睡觉", "过夜", "睡一觉", "该睡了", "天黑了睡觉", "躺床上", "睡吧", "去睡"],
    "light": ["点灯", "放火把", "插火把", "照明", "点火把", "放个火把", "照亮", "放火把"],
    "bridge": ["搭桥", "垫方块", "铺路", "搭路", "铺个桥", "垫脚", "铺个路", "搭个桥", "垫个", "铺一下"],
    "hide": ["躲起来", "藏起来", "躲好", "藏身", "隐蔽", "躲一下", "找地方躲", "躲躲", "藏一下"],
    "find_village": ["找村庄", "找个村庄", "找村民", "找村落", "去村庄", "找村子", "找村庄去"],
    "find_ore": ["找矿石", "找钻石", "找铁矿", "找金矿", "寻矿", "找矿脉", "找点矿", "探矿", "帮我找", "找找矿"],
    "attack": ["打怪", "攻击", "打一下", "打它", "杀怪", "打怪去", "打僵尸", "打苦力怕", "砍它", "去打", "打打"],
    "follow": ["跟着", "跟随", "跟过来", "跟我走", "跟着我", "一起走", "跟好"],
    "search_block": ["找木头", "找石头", "找食物", "找箱子", "搜刮", "找东西", "找宝箱", "找点食物", "找点吃的", "找物资", "找点物资"],
    "jump": ["跳一下", "跳起来", "跳过去", "蹦一下", "跳跳", "跳一跳"],
    "crouch": ["蹲下", "潜行", "蹲着", "小心走", "蹲好"],
    "map_view": ["看地图", "打开地图", "查地图", "看小地图", "看下地图", "看看地图", "看地图去"],
    "drop_item": ["扔掉", "丢了", "丢弃", "扔了", "丢东西", "扔掉垃圾", "扔掉点", "扔个"],
    "repair": ["修装备", "修复", "修理", "修一下", "修工具", "修补", "修修"],
}

# 斜杠命令保留，本模块只处理普通文本
_SLASH_RE = re.compile(r"^/+")


def is_enabled() -> bool:
    return _ENABLED


def detect_intent(text: str) -> Optional[Tuple[str, Dict]]:
    """从自然语言文本检测游戏操作意图。

    返回 (intent_type, params) 或 None（无明确意图，按正常聊天处理）。
    """
    if not text or not text.strip():
        return None
    # 斜杠命令跳过（由 bot.py 原有命令逻辑处理）
    if _SLASH_RE.match(text.strip()):
        return None

    t = text.strip()

    # 特殊优先级：停止 > 状态 > 自主（避免误判）
    for keyword in ["停下", "别玩了", "停手", "暂停", "别动了"]:
        if keyword in t:
            return ("stop_auto", {})

    # 时间查询高优先级（避免被其他意图误判）
    for kw in _INTENT_RULES.get("query_time", []):
        if kw in t:
            return ("query_time", {})

    for keyword in ["看看状态", "现在怎么样", "你在哪", "位置", "你在干嘛"]:
        if keyword in t:
            return ("show_state", {})

    for keyword in ["自己玩", "自动玩", "你玩吧", "自主玩", "你去玩", "托管", "自己看着办"]:
        if keyword in t:
            return ("auto_play", {})

    # 数字解析：移动距离（秒）
    duration = 2.0
    dm = re.search(r"(\d+(?:\.\d+)?)\s*秒", t)
    if dm:
        duration = float(dm.group(1))

    # 中优先级：明确的动作意图（避免被"看看/移动/做"等误吞）
    mid_priority = ["check_danger", "pickup", "escape", "explore", "attack",
                    "sleep", "bridge", "build", "plant", "fish", "smelt", "repair",
                    "light", "hide", "find_village", "find_ore",
                    "search_block", "go_home", "craft", "chop_tree", "drop_item",
                    "equip", "follow", "jump", "crouch"]
    for intent in mid_priority:
        for kw in _INTENT_RULES.get(intent, []):
            if kw in t:
                params = {}
                if intent in ("escape", "attack"):
                    params["duration"] = min(duration, 3.0)
                if intent == "craft":
                    params["item"] = _extract_craft_item(t)
                if intent == "follow":
                    params["duration"] = min(duration, 3.0)
                return (intent, params)

            # 遍历其他意图
            for intent, keywords in _INTENT_RULES.items():
                for kw in keywords:
                    if kw in t:
                        params = {"duration": duration}
                        # 移动意图：解析方向
                        if intent in ("move_forward", "move_back", "move_left", "move_right"):
                            # 提取目标描述（如"去看看前面的村庄"）
                            params["target"] = t
                        elif intent == "turn_look":
                            # 解析角度（如"看向90度"）；未指定角度则执行时随机看看
                            am = re.search(r"(-?\d+(?:\.\d+)?)\s*度", t)
                            params["yaw"] = float(am.group(1)) if am else None
                        elif intent == "craft":
                            # 提取要合成的物品
                            params["item"] = _extract_craft_item(t)
                        elif intent == "dig":
                            # 提取挖掘时长
                            params["duration"] = min(duration, 5.0)
                        elif intent in ("escape", "move_back"):
                            params["duration"] = min(duration, 3.0)  # 逃跑走快些
                        return (intent, params)
        return None


def _extract_craft_item(text: str) -> str:
    """从文本提取要合成的物品名。"""
    # 常见物品映射
    item_map = {
        "钻石剑": "diamond_sword", "铁剑": "iron_sword", "石剑": "stone_sword",
        "钻石镐": "diamond_pickaxe", "铁镐": "iron_pickaxe", "石镐": "stone_pickaxe",
        "面包": "bread", "牛排": "cooked_beef", "烤猪肉": "cooked_porkchop",
        "工作台": "crafting_table", "箱子": "chest", "熔炉": "furnace",
        "火把": "torch", "木板": "oak_planks", "木棍": "stick",
        "床": "red_bed", "盾牌": "shield", "弓": "bow", "箭": "arrow",
    }
    for cn, mid in item_map.items():
        if cn in text:
            return mid
    # 尝试提取英文物品 id（如 "做个 diamond_sword"）；
    # 限定至少 4 个字符的完整单词，避免把 "make"、"a" 之类的普通英文当成物品名
    m = re.search(r"\b[a-z][a-z_]{3,}\b", text)
    if m:
        return m.group(0)
    return ""


async def execute_intent(intent_type: str, params: Dict) -> Dict:
    """执行游戏操作意图。返回执行结果 dict。"""
    result = {"intent": intent_type, "ok": False, "detail": ""}
    if mc_watcher is None:
        result["detail"] = "MC 插件未安装（brain_mc_mod），可在插件市场安装"
        return result
    try:
        if intent_type == "move_forward":
            r = mc_watcher.control_move({"forward": True}, params.get("duration", 2.0))
            result.update(ok=r.get("ok", False), detail="往前走了一段" if r.get("ok") else f"移动失败:{r.get('error')}")

        elif intent_type == "move_back":
            r = mc_watcher.control_move({"back": True}, params.get("duration", 2.0))
            result.update(ok=r.get("ok", False), detail="往后退了退" if r.get("ok") else f"移动失败:{r.get('error')}")

        elif intent_type == "move_left":
            r = mc_watcher.control_move({"left": True}, params.get("duration", 2.0))
            result.update(ok=r.get("ok", False), detail="往左走了走" if r.get("ok") else f"移动失败:{r.get('error')}")

        elif intent_type == "move_right":
            r = mc_watcher.control_move({"right": True}, params.get("duration", 2.0))
            result.update(ok=r.get("ok", False), detail="往右走了走" if r.get("ok") else f"移动失败:{r.get('error')}")

        elif intent_type == "turn_look":
            yaw = params.get("yaw")
            if yaw is None:
                # 随机转个角度看看
                import random
                yaw = random.choice([0, 45, 90, 135, 180, -45, -90, -135])
            r = mc_watcher.control_look(yaw, 0, True)
            result.update(ok=r.get("ok", False), detail=f"转身看了看（{yaw}°）" if r.get("ok") else f"转向失败:{r.get('error')}")

        elif intent_type == "interact":
            r = mc_watcher.interact()
            result.update(ok=r.get("ok", False), detail=f"交互了「{r.get('target','方块')}」" if r.get("ok") else f"交互失败:{r.get('error')}")

        elif intent_type == "eat":
            r = mc_watcher.use_hand_item()
            result.update(ok=r.get("ok", False), detail=f"吃了「{r.get('used_item','食物')}」" if r.get("ok") else f"使用失败:{r.get('error')}")

        elif intent_type == "use_item":
            r = mc_watcher.use_hand_item()
            result.update(ok=r.get("ok", False), detail=f"使用了「{r.get('used_item','物品')}」" if r.get("ok") else f"使用失败:{r.get('error')}")

        elif intent_type == "organize":
            r = mc_watcher.organize_inventory()
            result.update(ok=r.get("ok", False), detail=f"整理了背包，合并{r.get('stacked',0)}组" if r.get("ok") else f"整理失败:{r.get('error')}")

        elif intent_type == "dig":
            # 精准挖掘前方（让 mod 持续挖到方块破坏）
            r = mc_watcher.dig(min(params.get("duration", 2.0), 5.0), timeout=15.0)
            result.update(ok=r.get("ok", False), detail=f"开始挖矿（{r.get('target_block','前方方块')}）" if r.get("ok") else f"挖矿失败:{r.get('error')}")

        elif intent_type == "craft":
            item = params.get("item", "")
            if not item:
                result.update(ok=False, detail="没听清要合成什么")
            else:
                # 走真实合成（摆材料取成品），不用 /give 作弊接口
                from mc_nav import get_nav
                r = await get_nav().craft(item)
                result.update(ok=r.get("ok", False), detail=r.get("detail", f"合成了{item}"))

        elif intent_type == "auto_play":
            from mc_agent import get_agent
            agent = get_agent()
            ok = agent.start()
            result.update(ok=ok, detail="我开始自己玩啦" if ok else "自主游戏启动失败")

        elif intent_type == "stop_auto":
            from mc_agent import get_agent
            get_agent().stop()
            mc_watcher.control_stop()
            result.update(ok=True, detail="好，我停下啦")

        elif intent_type == "query_time":
            # 查询当前实时时间
            try:
                from realtime import format_now, get_time_dict
                now_info = format_now()
                d = get_time_dict()
                detail = now_info
                if d.get("is_night"):
                    detail += "，已经是晚上了，注意休息"
                result.update(ok=True, detail=detail)
            except Exception:
                import datetime
                result.update(ok=True, detail=f"现在是 {datetime.datetime.now().strftime('%Y年%m月%d日 %H:%M')}")
            return result

        elif intent_type == "show_state":
            state_text = mc_watcher.format_state_text()
            result.update(ok=True, detail=state_text or "读不到游戏状态")

        elif intent_type == "escape":
            # 逃跑：后退 + 转向
            r = mc_watcher.control_move({"back": True}, params.get("duration", 2.0))
            mc_watcher.control_look(180, 0, True)
            result.update(ok=r.get("ok", False), detail="快跑，往后退啦" if r.get("ok") else f"逃跑失败:{r.get('error')}")

        elif intent_type == "explore":
            # 探索：向前走 + 环顾四周
            r1 = mc_watcher.control_move({"forward": True}, 2.0)
            for yaw in [0, 90, 180, -90]:
                mc_watcher.control_look(yaw, 0, True)
                await asyncio.sleep(0.3)
            result.update(ok=r1.get("ok", False), detail="我探了一圈周围的环境~" if r1.get("ok") else f"探索失败:{r1.get('error')}")

        elif intent_type == "chop_tree":
            # 砍树：挖面前方块（木头）
            r = mc_watcher.dig(3.0, timeout=10.0)
            result.update(ok=r.get("ok", False), detail=f"开始砍树（{r.get('target_block','前方木头')}）" if r.get("ok") else f"砍树失败:{r.get('error')}")

        elif intent_type == "check_danger":
            # 检查危险：感知附近生物
            from mc_nav import get_nav
            state = mc_watcher.fetch_state()
            entities = (state or {}).get("entities") or []
            if entities:
                dangerous = []
                for e in entities:
                    t = e.get("type", "").split(":")[-1]
                    d = e.get("dist", "?")
                    dangerous.append(f"{t}({d}m)")
                result.update(ok=True, detail=f"附近有这些生物：{'、'.join(dangerous)}，注意安全")
            else:
                result.update(ok=True, detail="附近暂时没发现危险生物，挺安全的~")

        elif intent_type == "go_home":
            # 回家（朝记忆里的基地地标走）
            from mc_explored import list_landmarks
            lms = list_landmarks()
            base = None
            for lm in lms:
                if lm.get("type") == "base":
                    base = lm
                    break
            if base:
                result.update(ok=True, detail=f"我记得基地在 X={base.get('x')} Y={base.get('y')} Z={base.get('z')}，朝那个方向走~")
            else:
                result.update(ok=False, detail="我还没记下基地位置呢，先 /记地标 记住吧")

        elif intent_type == "pickup":
            # 拾取战利品
            r = mc_watcher.pickup_loot()
            items = r.get("items_found", 0)
            result.update(ok=r.get("ok", False), detail=f"捡到了{items}个掉落物" if r.get("ok") else f"拾取失败:{r.get('error')}")

        elif intent_type == "build":
            # 建造：先确保手上有方块，再放一块在面前
            from mc_nav import get_nav
            bridge = await get_nav().place_bridge_block()
            result.update(ok=bridge.get("ok", False), detail=f"开始建造（{bridge.get('detail','')}）" if bridge.get("ok") else f"建造失败:{bridge.get('detail')}")

        elif intent_type == "plant":
            # 种地：挖面前方块准备耕地，然后使用（手持种子）
            r = mc_watcher.dig(2.0, timeout=8.0)
            result.update(ok=r.get("ok", False), detail="开始种地，挖了个坑" if r.get("ok") else f"种地失败:{r.get('error')}")

        elif intent_type == "fish":
            # 钓鱼：使用手持物品（鱼竿）
            r = mc_watcher.use_hand_item()
            result.update(ok=r.get("ok", False), detail="抛出鱼竿钓鱼啦" if r.get("ok") else f"钓鱼失败:{r.get('error')}")

        elif intent_type == "smelt":
            # 烧炼：使用熔炉（若面前有熔炉会打开，用命令给结果）
            r = mc_watcher.interact()
            result.update(ok=r.get("ok", False), detail="打开熔炉开始烧炼" if r.get("ok") else f"烧炼失败:{r.get('error')}")

        elif intent_type == "equip":
            # 换装备/拿武器：从背包选第一个武器类物品到主手
            inv = mc_watcher.inventory_items()
            weapon = None
            for it in inv:
                iid = str(it.get("item", ""))
                if any(k in iid for k in ["sword", "pickaxe", "axe", "shovel", "bow", "crossbow"]):
                    weapon = it
                    break
            if weapon:
                r = mc_watcher.select_slot(weapon.get("slot", 0))
                result.update(ok=r.get("ok", False), detail=f"换上了「{weapon.get('item','武器')}」" if r.get("ok") else f"换装失败:{r.get('error')}")
            else:
                result.update(ok=False, detail="背包里没有武器或工具")

        elif intent_type == "sleep":
            # 睡觉：交互面前的床
            r = mc_watcher.interact()
            result.update(ok=r.get("ok", False), detail="尝试躺下睡觉" if r.get("ok") else "面前没有床，我站着睡吧（开玩笑的）")

        elif intent_type == "light":
            # 点灯/放火把：从背包选火把，右键放置
            torch_slot = None
            for it in mc_watcher.inventory_items():
                if "torch" in str(it.get("item", "")).split(":")[-1]:
                    torch_slot = it.get("slot")
                    break
            if torch_slot is None:
                result.update(ok=False, detail="背包里没有火把，做几个再来放吧")
            else:
                mc_watcher.select_slot(int(torch_slot))
                r = mc_watcher.place_block(face="up")
                result.update(ok=r.get("ok", False), detail="放个火把照明" if r.get("ok") else f"放火把失败:{r.get('error')}")

        elif intent_type == "bridge":
            # 搭桥/垫方块
            from mc_nav import get_nav
            bridge = await get_nav().place_bridge_block()
            result.update(ok=bridge.get("ok", False), detail=f"搭了个桥（{bridge.get('detail','')}）" if bridge.get("ok") else f"搭桥失败:{bridge.get('detail')}")

        elif intent_type == "hide":
            # 躲起来：蹲下潜行
            r = mc_watcher.control_move({"sneak": True}, 3.0)
            result.update(ok=r.get("ok", False), detail="我蹲下躲起来啦" if r.get("ok") else f"躲藏失败:{r.get('error')}")

        elif intent_type == "find_village":
            # 找村庄：记忆/探索
            from mc_nav import get_nav
            r = mc_watcher.control_move({"forward": True}, 3.0)
            result.update(ok=r.get("ok", False), detail="我朝前走，帮你找找村庄~" if r.get("ok") else f"探索失败:{r.get('error')}")

        elif intent_type == "find_ore":
            # 找矿石：向前探索找矿
            r = mc_watcher.control_move({"forward": True}, 3.0)
            result.update(ok=r.get("ok", False), detail="我四处找找矿石~" if r.get("ok") else f"探索失败:{r.get('error')}")

        elif intent_type == "attack":
            # 攻击：长按攻击
            r = mc_watcher.control_move({"attack": True}, params.get("duration", 2.0))
            result.update(ok=r.get("ok", False), detail="我攻击啦！" if r.get("ok") else f"攻击失败:{r.get('error')}")

        elif intent_type == "follow":
            # 跟随：朝前走（跟随主人）
            r = mc_watcher.control_move({"forward": True}, params.get("duration", 3.0))
            result.update(ok=r.get("ok", False), detail="我跟着你走啦~" if r.get("ok") else f"跟随失败:{r.get('error')}")

        elif intent_type == "search_block":
            # 找物资：探索周围
            r = mc_watcher.control_move({"forward": True}, 2.0)
            result.update(ok=r.get("ok", False), detail="我帮你找找物资~" if r.get("ok") else f"搜索失败:{r.get('error')}")

        elif intent_type == "jump":
            # 跳跃
            r = mc_watcher.control_move({"jump": True}, 1.0)
            result.update(ok=r.get("ok", False), detail="跳了一下~" if r.get("ok") else f"跳跃失败:{r.get('error')}")

        elif intent_type == "crouch":
            # 蹲下/潜行
            r = mc_watcher.control_move({"sneak": True}, 2.0)
            result.update(ok=r.get("ok", False), detail="我小心蹲下走~" if r.get("ok") else f"蹲下失败:{r.get('error')}")

        elif intent_type == "map_view":
            # 看地图：读取当前地形/群系
            state = mc_watcher.fetch_state() or {}
            terrain = state.get("terrain") or {}
            biome = str(terrain.get("biome") or (state.get("player") or {}).get("biome") or "未知群系").split(":")[-1]
            result.update(ok=True, detail=f"当前在「{biome}」，我看看地形~")

        elif intent_type == "drop_item":
            # 扔掉：丢弃主手物品（简化：告知需要选中）
            result.update(ok=True, detail="想让我扔什么？用 /mc 命令更精确（当前暂不支持自然语言丢弃）")

        elif intent_type == "repair":
            # 修装备：用命令修复（需作弊）
            r = mc_watcher.craft("experience_bottle", 1)
            result.update(ok=r.get("ok", False), detail="我试试修一下装备" if r.get("ok") else f"修理失败:{r.get('error')}")

        else:
            result.update(ok=False, detail=f"未知意图:{intent_type}")
    except Exception as e:
        result.update(ok=False, detail=f"执行异常:{e}")
    return result


# 返回给 AI 的一句话描述
def intent_to_reply(result: Dict) -> str:
    """把执行结果转成给用户的自然语言回复。"""
    detail = result.get("detail", "")
    if result.get("ok"):
        return f"好，{detail}~"
    return detail


# ---------------------------------------------------------------------------
# AI 语义判断层：处理深层/模糊话术（关键词没命中时兜底）
# ---------------------------------------------------------------------------
_AI_INTENT_SYSTEM_PROMPT = """你是「肥鱼娘」游戏智能体的意图解析器。判断用户的一句话是否包含对 Minecraft 游戏的操作指令。

可识别的意图（action）：
- move_forward: 前进/往前走/去某处
- move_back: 后退
- move_left: 向左
- move_right: 向右
- turn_look: 转身看/望向某方向
- interact: 开门/开箱/交互方块
- eat: 吃东西/补充体力/回血
- use_item: 使用物品/射箭/用工具
- organize: 整理背包/腾位置
- dig: 挖矿/挖方块/砍树
- craft: 合成/制作物品
- auto_play: 自主玩/托管/自己决定
- stop_auto: 停止/暂停/停手
- escape: 逃跑/撤退/快跑
- explore: 探索/侦察/看环境
- check_danger: 检查危险/附近安全吗
- go_home: 回家/回基地
- pickup: 拾取/捡东西
- build: 建造/盖房子/搭围栏
- plant: 种地/种植/种树
- fish: 钓鱼
- smelt: 烧炼/熔炉烧矿
- equip: 换装备/拿武器/拿工具
- sleep: 睡觉/过夜
- light: 点灯/放火把/照明
- bridge: 搭桥/垫方块/铺路
- hide: 躲起来/潜行藏身
- find_village: 找村庄/找村民
- find_ore: 找矿石/找钻石/挖矿寻矿
- attack: 打怪/攻击/杀怪
- follow: 跟随/跟我走
- search_block: 找物资/找食物/找箱子
- jump: 跳跃/跳一下
- crouch: 蹲下/潜行
- map_view: 看地图/查地形
- drop_item: 扔掉/丢弃物品
- repair: 修装备/修理工具
- show_state: 看状态/在哪
- query_time: 查询时间/几点/日期/星期几
- none: 没有游戏操作意图（只是闲聊）

【重要规则】
- 只有【明确】是对游戏的操作指令时才返回对应意图
- 纯闲聊、情感表达、无关话题返回 none
- 模糊表达优先给合理意图，但要谨慎，不明确就给 none

只输出 JSON，格式：
{"action":"<意图>","param":"<额外参数，如物品名/方向>","reason":"<理由>"}
"""


async def detect_intent_ai(text: str):
    """用 AI（DeepSeek）判断深层话术的意图。返回 (intent_type, params) 或 None。"""
    try:
        import config
        from ai_provider import get_llm
        client = get_llm()
        messages = [{"role": "system", "content": _AI_INTENT_SYSTEM_PROMPT}]
        # 注入记忆（AI自我认知，帮助理解人设语境）
        try:
            from memory_context import build_memory_messages
            messages.extend(build_memory_messages("", include={"profile": False, "notes": False, "ai_profile": True}))
        except Exception as e:
            degrade("libs/qq_bot_runtime/intent_router.py:558 detect_intent_ai", e, "降级：from memory_context import build_memory_messages")
        messages.append({"role": "user", "content": f"用户说：\"{text}\"\n请判断意图。"})
        raw = await client.chat(messages, model=config.DEEPSEEK_MODEL, think=False, capability="chat")
        import json
        raw = raw.strip().strip("`")
        if raw.startswith("json"):
            raw = raw[4:].strip()
        start = raw.find("{")
        end = raw.rfind("}")
        if start != -1 and end > start:
            obj = json.loads(raw[start:end + 1])
            action = obj.get("action", "none")
            if action == "none":
                return None
            param = obj.get("param", "")
            params = {}
            if action == "craft" and param:
                params["item"] = param
            elif action == "move_forward":
                params["duration"] = 2.0
            return (action, params)
    except Exception as e:
        print(f"[INTENT-AI] AI 意图判断失败: {e}")
    return None


async def detect_intent_extended(text: str):
    """扩展意图检测：先关键词快筛，未命中再用 AI 语义判断。

    返回 (intent_type, params) 或 None。
    """
    # 0. 游戏不在线时没有可执行对象，直接跳过（省掉每条消息一次 AI 调用）
    if not _game_online():
        return None
    # 1. 关键词快筛
    result = detect_intent(text)
    if result is not None:
        return result
    # 2. AI 语义判断（深层话术）
    try:
        return await detect_intent_ai(text)
    except Exception:
        return None
