# -*- coding: utf-8 -*-
"""Mineflayer Bot 大脑（纯净原版世界试验场）。

把 LLM 大脑接到 mc_bot/bridge.js 的原版机器人身体上：
- 观察：bridge /state（位置/血量/背包/附近玩家/实体）+ 定期 /scan（资源扫描）
- 动作：goto 真寻路、dig_target 挖块、attack 战斗、eat 进食、say 说话
- 交流：游戏里玩家说话经 /chat 注入会话，本轮优先 say 回应/执行指令
- 记忆：玩家聊天时刷新注入档案/笔记/近况快照（已跨平台绑定的玩家按 QQ 档案全量注入，
  陌生人只给通用部分）；对话按 canonical 记忆键（绑定后=QQ 号）写回 chat_history（type=mc）；
  绑定/确认/「记住:」在 LLM 之前用规则处理（identity.py）
- 设施账本：自放方块/箱子内容快照/熔炉任务由桥持久化（mc_bot/ledger.json，按世界分桶），
  观察每轮注入"我的设施"——重启/断线后仍记得自己建过什么、存了什么、在烧什么
- 学习：复用共享 mc_skills/mc_tips 资产——save_skill 存 bot 环境技能（原版可重放）
  并自动沉淀一条通用技巧进共享技巧库（learned），模组世界版大脑每轮会注入看到；
  观察时按环境过滤注入共享技巧/可用技能。技能带 env 标签，与模组技能互不污染。
- 安全：血量低自动吃、饥饿告警；桥断线静默等待
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from typing import Dict, List, Optional

import httpx

import config
import identity
from ai_provider import get_llm
from quiet import degrade

try:
    import mc_skills
except Exception:
    mc_skills = None  # 技能库模块缺失时相关工具直接 fail，不阻塞主体

BRIDGE = "http://127.0.0.1:8767"

# ---------- 记忆注入（游戏内对话） ----------
_MEMORY_TAG = "【记忆快照】"            # 快照 system 消息的标记，刷新时原位替换
MC_OWNER_QQ = str(getattr(config, "PROACTIVE_PRIVATE_USER_ID", "") or "").strip()  # 主人 QQ id（记忆文件键）
# 主人游戏名等跨平台绑定已迁移到 identity_bindings.json（identity.py，config
# IDENTITY_OWNER_MC_NAMES 预置）；identity 关闭时 _legacy_owner_name 兜底旧语义。

_mem_cache = None
_mem_mtime = 0.0

# ---------- 守卫/遥测/探索共享模块（懒加载，均文件级无 QQ 依赖） ----------
VANILLA_WORLD_KEY = "vanilla"   # mc_explored 伪种子键（与原版世界分桶隔离）
_monitor_cache = None


def _mon():
    """mc_monitor 单例（遥测，写 data/mc_behavior.jsonl，dashboard/analyze 共用）。"""
    global _monitor_cache
    if _monitor_cache is None:
        try:
            from mc_monitor import get_monitor
            _monitor_cache = get_monitor()
        except Exception:
            _monitor_cache = None
    return _monitor_cache


def _hints_path():
    p = str(getattr(config, "MC_HINTS_FILE", "") or "")
    if p:
        return p
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "mc_hints.jsonl")

# ---------- 游戏状态上报（QQ 侧读取 + 主动 QQ 汇报） ----------
_LIVE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "mc_bot_live.json")
_QQ_API = str(getattr(config, "QQ_HTTP_API", "") or "http://127.0.0.1:3000")
QQ_NOTIFY_ENABLED = bool(getattr(config, "ENABLE_MC_QQ_NOTIFY", True))
_qq_notify_kind = {}    # kind -> 上次发送时间
_qq_notify_global = 0.0  # 全局最小间隔（秒）


def _qq_api_send(text: str) -> bool:
    """经 SnowLuma OneBot HTTP 给主人发私聊（免鉴权本地接口）。失败只记日志。"""
    if not MC_OWNER_QQ or not _QQ_API:
        return False
    try:
        r = httpx.post(_QQ_API + "/send_private_msg",
                       json={"user_id": MC_OWNER_QQ,
                             "message": [{"type": "text", "data": {"text": text}}]},
                       timeout=5.0, trust_env=False)
        return bool(r.status_code == 200 and r.json().get("status") == "ok")
    except Exception as e:
        print(f"[BOT-BRAIN] QQ 汇报失败: {e}")
        return False


def _notify_qq(text: str, kind: str = "misc", min_gap: float = 8.0) -> None:
    """主动 QQ 汇报（带全局 8s + 按类型限流）。"""
    if not QQ_NOTIFY_ENABLED:
        return
    now = _now()
    global _qq_notify_global
    if now - _qq_notify_global < 8.0:
        return
    if now - _qq_notify_kind.get(kind, 0.0) < min_gap:
        return
    if _qq_api_send(text):
        _qq_notify_global = now
        _qq_notify_kind[kind] = now
        print(f"[BOT-BRAIN] QQ 汇报[{kind}]: {text[:60]}")


def _write_live_status(connected: bool, state: Optional[Dict] = None,
                       action: str = "", recent_chat: Optional[List] = None,
                       running: bool = True) -> None:
    """把当前状态写 data/mc_bot_live.json 供 QQ 侧对话注入读取。"""
    try:
        st = state or {}
        pos = st.get("pos") or {}
        data = {
            "ts": _now(), "running": running, "connected": connected,
            "pos": pos, "health": st.get("health"), "food": st.get("food"),
            "dimension": st.get("dimension"), "players": st.get("players") or [],
            "inventory": st.get("inventory") or {},
            "action": str(action or "")[:120], "recent_chat": recent_chat or [],
        }
        os.makedirs(os.path.dirname(_LIVE_FILE), exist_ok=True)
        with open(_LIVE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception as e:
        print(f"[BOT-BRAIN] 状态文件写入失败: {e}")

BOT_SYSTEM_PROMPT = """你是 Minecraft（纯净原版世界）里的自主生存智能体，身体是一个协议级机器人。

【大胆行动】敢闯敢试的冒险家：下矿洞、找村庄、打怪、种地、钓鱼、盖房都去试。犹豫时选择行动。
【保命底线】血量≤6、饥饿≤6 必须立刻处理（吃东西/撤到安全处）。其余风险可以承受。岩浆/溺水/窒息/濒死进食这类毫秒级险情由身体反射自动处理，观察里的"⚠ 自保反射"行说明刚出过险——别重复救，把注意力放在"为什么走到这"和接下来的路线上。
【身体能力与模组世界不同】
- 移动：goto 是自动寻路（会跳障碍/绕水/游泳），告诉它目标坐标即可，不用一步步 move。
- 挖方块：dig_target 按方块名挖最近目标（如 oak_log / iron_ore / stone）；不知道附近有什么就先 scan。
- 附近资源：scan 会列出 28 格内的木头/矿物/动物种类。
- 打怪：attack target=hostile 攻击最近的敌对怪；想打某只动物用 attack target=动物名。
- 交互：说话用 say（真人玩家聊天也要用这个回复）；与方块交互（开箱子/工作台）目前有限，优先用挖/走/打/吃这些动作。
- 坐标来自观察的"位置"，要去哪自己算（greedy 直线朝目标走，桥会寻路）。
- 观察的"地形"行会告诉你：脚下是否悬空、四周地面是平坦/陡降/崎岖（含各方向高差）。放方块只需要脚下有实心方块托底（土/石/草/沙都可以，**方块类型不影响放置和建造**）——别为了"找草地"到处跑。想要平地就直接造：routine make_flat 会把脚下区域挖高填低整平（挖出的泥土/圆石正好当填料）。唯一的禁忌：脚下悬空或目标格是深渊/液体时不要放置；方向陡降时别冲过去。
【和真人玩家交流】世界里会有真人玩家（同伴或朋友）进来说话。一听到有人说话就优先热情回应（say，简短口语、像朋友聊天），并认真执行对方的要求——"过来/跟我来"就用 goto 走到他坐标，其他指令照常。回应完可以继续自己的探索；没人说话时正常自主行动，别在玩家旁边自言自语刷屏。
【技能复用·原版学到模组版用】摸索出验证有效的做法（打完怪/做成工具/找到规律）就调用 save_skill 存：步骤写本桥工具名（goto/dig_target/attack/eat/scan/look/say…），存后会**自动沉淀一条通用技巧进共享技巧库，模组世界版的大脑能看到**。观察里有"相关经验/可用技能"就优先参考：是 bot 技能用 run_skill 重放，不是（或没有）就按文字思路用当前工具自己做。别把同一做法翻来覆去重复摸索。
【连贯行动】每次回应尽量一口气 3~6 个工具调用排成队列；移动用 goto 一次到位。
【失败止损】动作返回 fail 必须想清楚原因再换招：同目标、同位置连续失败=隔着墙或不可达，立即放弃换目标/换位置——严禁原样重试同一失败动作（系统也会拦）。连续失败先停手反思。
【生存阶段推进】按梯子走：木工具→石工具→火把/熔炉/食物→床与庇护所/箱子→铁器→钻石。做东西前先 check_recipes 看缺什么；缺石头先做镐挖圆石，挖矿必须手持镐（equip），否则没有掉落。挖不了报"被挡住"= 矿隔着墙（桥按原版规则禁止隔墙挖）：先 dig_target 挖开挡路的石头/泥土，露出矿的面再挖，别对着墙反复 dig_target 同一个矿名。
【找矿策略（别死找裸露矿）】埋着的矿不会自己露出来，要你把它挖出来——scan 只报看得见的"露头矿"，出现"埋着的矿"或 dig_target 报"被挡住"= 该下挖了。缺矿时直接调 routine find_ores（人类玩家找矿的标准打法，参考 MineRL/MineDojo 先验）：先阶梯下挖到目标矿的高产层（铁 y≈16、铜 y≈48、煤 y≈96、金 y≈-16、钻石 y≈-59），到达后自动蛇形走廊挖掘主动暴露矿面、露头矿自动收。dig_target 只用来收已经露出的矿，用它"找矿"= 在地表碰运气，禁止。
【下矿纪律·火把】每次下矿（find_ores/dig_stairs/dig_tunnel）前背包必须带足至少 8 支火把——先 check_recipes + craft torch（1 煤+1 木棍=4 支，无需工作台）；煤不够就先在地表挖 coal_ore（scan 露头或 find_ores ore=coal_ore），木棍不够就用 2 块木板现做。地下每走几步就插一支火把（dig_stairs/dig_tunnel 会自动插，只要背包有 torch）——火把既是照明防刷怪也是认路标记，迷路时顺火把退回地面。火把少于 4 支立即停下回地面补齐再下去，绝不黑灯下矿。
【探索不徘徊】不要在同一小片区域反复徘徊：一个方向/一个位置连续失败就换方向，多去没走过的区域（explore 无方向时自动挑未探索方向）。
【称呼一致性】回复里怎么称呼对方，一律以注入记忆里【称呼规则】给的当前生效称呼为准；档案 facts 里出现过的其它叫法/外号/旧称呼（如"主人"）都别再用。
【独立生存】你的目标是不依赖任何人、自给自足地活下去并推进生存阶段。困难全部自己解决：卡住/掉坑/死路就换方向走、up 搭柱爬升（背包常留 dirt/cobblestone 垫脚）、dig_target 挖开挡路方块或绕路；打不过就跑或躲；饿了自己找食物；没工具就重做。一个办法试 1~2 次不行立刻换目标/换地方/换方法，继续做能推进生存的事——严禁原地等待救援、严禁喊人求助、严禁因为卡住就停下来发呆。别人主动帮你可以顺势接受并道谢，但你绝不主动开口求救。
【用好箱子】有了箱子把暂时用不上的物资 chest store 存起来，背包只留工具/食物/建材；烧炼用 furnace（投料 put_input 原料 + put_fuel 燃料如 oak_log/coal，过一会儿 peek/take）。你放的方块、箱内存放和熔炉任务会被桥自动记成账本（重启/断线也记得），观察的"我的设施"行会告诉你它们在哪、存了什么/在烧什么——快照可能滞后，当场确认用 chest list / furnace peek。
【状态机模式】routine 工具能一键跑完整流程（会自动换工具/找材料/放方块），适合不用动脑的重复活，按需调用：
- gear_up：升装备全套（木工具→挖圆石→石镐→熔炉→有铁矿就烧锭）
- smelt_ores：把背包所有矿石烧成锭
- gather：采集（material=wood/stone，n=数量）
- store_all：杂物入箱
- go_home：回地标（name=家）
- escort_owner：跟着同伴走
- explore_new_area：探索新方向
- make_flat：把脚下周围就地整平（挖高填低，自己造平地，不用找空地）
- progress_survival：生存阶段一键推进（木料→石镐石斧→火把→熔炉→铁镐→床→箱子基地），缺什么它会自己按前置链补（要石镐就自动做木板→工作台→木镐→挖圆石）
模式运行中你说话会暂停它（小步收尾），回完话再调同名 routine 即自动续跑；不想续了就别再调。观察里的"可用模式"一栏是匹配当前情况的建议。"""


def _now() -> float:
    return time.time()


def _safe_float(v, d=0.0) -> float:
    try:
        return float(v)
    except Exception:
        return d


def _safe_int(v, d=0) -> int:
    try:
        return int(v)
    except Exception:
        return d


_HOSTILE = {"zombie", "skeleton", "spider", "creeper", "witch", "enderman",
            "hoglin", "slime", "drowned", "stray", "husk", "phantom"}

# 守卫/兜底参数（config，含默认）
_guard_cooldown = float(getattr(config, "MC_GUARD_WARNING_COOLDOWN", 15.0))
_rule_fallback_every = max(1, _safe_int(getattr(config, "MC_FALLBACK_RETRY_EVERY", 5), 5))
_llm_fail_fallback = max(1, _safe_int(getattr(config, "MC_AGENT_LLM_FAIL_FALLBACK", 3), 3))
_use_llm_brain = bool(getattr(config, "MC_AGENT_USE_LLM_BRAIN", True))


def _bridge_get(path: str, timeout: float = 4.0) -> Optional[Dict]:
    try:
        # trust_env=False：httpx 读 Windows 注册表代理时忽略“本地例外”，
        # 开了系统代理会把 127.0.0.1 的桥请求转给代理并拿到 503 空响应
        r = httpx.get(BRIDGE + path, timeout=timeout, trust_env=False)
        return r.json() if r.status_code == 200 else None
    except Exception:
        return None


def _bridge_cmd(action: str, timeout: float = 60.0, **kw) -> Dict:
    try:
        r = httpx.post(BRIDGE + "/cmd", json={"action": action, **kw}, timeout=timeout, trust_env=False)
        return r.json() if r.status_code == 200 else {"ok": False, "msg": f"HTTP {r.status_code}"}
    except Exception as e:
        return {"ok": False, "msg": str(e)}


# ---------------- 记忆快照（从项目记忆文件组装，注入游戏对话） ----------------
def _qq_mem():
    """memory.py 的 Memory 实例（按 memory_data.json 的 mtime 自动刷新，只读）。

    QQ 核心进程约每 2 秒才把短时记忆落盘，这里跟着 mtime 走，
    避免长期运行后游戏侧一直读到启动时的旧快照。
    """
    global _mem_cache, _mem_mtime
    try:
        mt = os.path.getmtime(os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "memory_data.json"))
    except OSError:
        mt = 0.0
    if _mem_cache is None or mt != _mem_mtime:
        from memory import Memory
        _mem_cache = Memory()
        _mem_mtime = mt
    return _mem_cache


def _legacy_owner_name(name) -> bool:
    """identity 关闭时的兜底：按 config.IDENTITY_OWNER_MC_NAMES 判主人（旧硬编码语义）。"""
    names = [str(n).strip().lower()
             for n in (getattr(config, "IDENTITY_OWNER_MC_NAMES", []) or [])]
    return str(name).strip().lower() in names and bool(MC_OWNER_QQ)


def _owner_call_name() -> str:
    """主人 QQ 当前生效的称呼（与 QQ 侧同源：NAME_OVERRIDES → 档案时间就近）。

    游戏内主动喊话等场景用，避免写死旧称呼与记忆档案冲突。
    """
    try:
        import emotion
        nm = emotion.resolve_display_name(MC_OWNER_QQ)
        if nm:
            return nm
    except Exception as e:
        degrade("libs/qq_bot_runtime/mc_bot_brain.py:250 _owner_call_name", e, "降级：import emotion")
    return "伙伴"


def _load_memory_digest(game_name: str, chat_text: str) -> Optional[str]:
    """按说话者把记忆文件组装成一段注入文本；失败/无内容返回 None（不阻塞聊天）。

    - 已跨平台绑定（QQ↔游戏名，双向确认生效）：身份提示 + 该 QQ 的档案/笔记/AI 档案
      + QQ 近况（摘要/话题/最近对白）+ 游戏历史 —— 跨场景记忆打通；
    - identity 关闭时回退旧行为：主人（config.IDENTITY_OWNER_MC_NAMES）全量注入，
      陌生玩家只给 AI 档案 + 通用知识库 + 他本人之前的游戏聊天，不给 QQ 私密档案。
    """
    try:
        name = str(game_name).strip()
        identity_enabled = bool(getattr(config, "ENABLE_IDENTITY_LINK", True))
        if identity_enabled:
            qq_id = identity.qq_of_mc(name) or ""
            hint = identity.mc_identity_hint(name) or ""
        else:
            qq_id = MC_OWNER_QQ if _legacy_owner_name(name) else ""
            hint = ""
        parts = []
        if qq_id:
            if hint:
                parts.append(hint)
            else:
                parts.append(f"（身份提示：正在说话的就是 {name}，下方档案记录的都是关于 TA 的真实信息——"
                             f"称呼、爱好、养的猫、最近聊过什么都直接用档案答，别反问档案里已有答案的问题。）")
        from memory_context import build_memory_messages
        for m in build_memory_messages(qq_id, include={
            "profile": bool(qq_id), "notes": bool(qq_id),
            "ai_profile": True, "history": False, "skip_history": True,
        }):
            c = str(m.get("content") or "").strip()
            if c:
                parts.append(c)
        if qq_id:
            mem = _qq_mem()
            bits = []
            s = mem.get_summary("mc", "", qq_id)
            if s:
                s = str(s).strip()
                if len(s) > 1200:
                    s = s[-1200:]  # 摘要是按时间累积的长文，只取最近部分，避免快照过大
                bits.append("摘要：" + s)
            t = mem.get_topic("mc", "", qq_id)
            if t:
                bits.append("最近话题：" + str(t).strip())
            if bits:
                parts.append(f"【QQ {qq_id} 上的近况】" + "；".join(bits))
            # 最近在 QQ 上的原始对话（summary 只装超出窗口的旧内容，最新几条在 store 里）
            qq_lines = []
            for m in mem.get("mc", "", qq_id):
                if m.get("role") not in ("user", "assistant"):
                    continue
                c = str(m.get("content") or "").strip().replace("\n", " ")[:120]
                if not c:
                    continue
                line = (f"{'TA' if m.get('role') == 'user' else '你(QQ)'}：{c}")
                if qq_lines and qq_lines[-1] == line:
                    continue  # 连续重复（同一条反复说）只留一次
                qq_lines.append(line)
            if qq_lines:
                parts.append(f"【{name} 最近在 QQ 上说的话】\n" + "\n".join(qq_lines[-10:]))
        # 与该玩家之前在游戏里聊过的片段（绑定后历史已并入 canonical 文件，仍只取 mc 行）
        from long_term_memory import get_user_history
        lines = []
        for r in get_user_history(qq_id or name, limit=8):
            if r.get("message_type") != "mc":
                continue
            c = str(r.get("content") or "").strip()
            if c:
                who = "玩家" if r.get("role") == "user" else "你"
                lines.append(f"{who}：{c}")
        if lines:
            parts.append("【之前和" + name + "在游戏里聊过】\n" + "\n".join(lines[-6:]))
        # 知识库按本次话题动态命中
        if chat_text.strip():
            from knowledge_service import get_knowledge
            kb = get_knowledge().recall(chat_text.strip()[:200])
            if kb:
                parts.append(str(kb).strip())
        return "\n".join(parts) if parts else None
    except Exception as e:
        print(f"[BOT-BRAIN] 记忆组装失败: {e}")
        return None


class BotAgent:
    def __init__(self):
        self._llm = get_llm()
        self._session: List[Dict] = [{"role": "system", "content": BOT_SYSTEM_PROMPT}]
        self._max_calls = max(4, _safe_int(getattr(config, "MC_AGENT_MAX_TOOL_CALLS", 16), 16))
        self._round_seconds = max(30.0, _safe_float(getattr(config, "MC_AGENT_ROUND_SECONDS", 90.0), 90.0))
        self._use_thinking = bool(getattr(config, "MC_AGENT_USE_THINKING", True))
        self._last_scan_round = -99
        self._rounds = 0
        self._last_action = ""
        self._running = False
        self._feed_until = 0.0
        self._chat_seq = 0          # 已消费到的桥端聊天序号
        self._awaiting_reply = False  # 本轮收到了玩家聊天，观察里提示先回应
        self._chat_partner = None   # 本轮在说话的游戏玩家（写回历史用）
        self._chat_person = None    # 该玩家的记忆键（绑定后 = 其 QQ 号，未绑定 = 原名）
        self._pending_says: List[str] = []  # 本轮 say 过的话，轮末写回
        self._recent_chat: List[Dict] = []  # 最近几条游戏聊天（写状态文件给 QQ 侧看）
        self._prev_connected = False
        self._down_since = 0.0      # 掉线起始时间（0=未在掉线）
        self._prev_health: Optional[float] = None
        self._saw_death = False     # 见到 health=0 后等复活再汇报
        # ---- 守卫/卡死/纪律（S1） ----
        self._pos_history: List[tuple] = []
        self._same_pos_streak = 0
        self._stuck_count = 0
        self._last_guard: Dict[str, float] = {}   # 警告类别 -> 上次时间
        self._guard_note = ""                      # 本轮待注入观察的安全警告
        self._reflex_note = ""                     # 本轮自救/反射记录
        self._fail_mem: Dict[str, Dict] = {}       # 防重试: key -> {n, ts, pos}
        self._round_fails: List[str] = []          # 本轮失败的 (工具:结果摘要)
        self._last_reflection = ""                 # 上一轮失败反思
        self._llm_fails = 0
        self._fallback_rounds = 0
        self._brain_mode = "llm"                   # "llm" | "rule"
        self._current_goal = "稳定生存并推进阶段目标"
        self._next_goal = ""
        # ---- 探索/提示/遥测（S3） ----
        self._hints_offset = 0
        self._last_mark_ts = 0.0
        self._last_dim = "overworld"   # 最近观察到的维度（地标分桶/设施归属用）
        self._last_reflex_seq = 0      # 已消费到的桥端自保反射序号（增量注入观察）
        self._round_cache: Dict[str, str] = {}   # 只读工具本轮缓存（scan/check_recipes）
        # ---- 状态机模式（routine） ----
        self._routine_pause = False   # 玩家插话时置位，模式在当前步后优雅暂停
        self._blocked_names = {"air", "water", "lava", "bedrock", "tall_grass", "grass",
                               "poppy", "dandelion", "grass_block", "dirt"}

    # ---------------- 主循环 ----------------
    async def run(self):
        self._running = True
        # 标记当前 world 为 mc：AI 调用失败时经统一入口（self_coding.report_ai_error）报障。
        # 默认关闭（BOT_SELF_CODING_ENABLED=False）时不触发，故不影响日常游玩。
        try:
            from self_coding import set_world
            set_world("mc")
        except Exception:
            pass
        self._hb = asyncio.create_task(self._heartbeat())  # 独立心跳：状态文件常新鲜
        while self._running:
            state = _bridge_get("/state")
            if not state or not state.get("connected") or not state.get("pos"):
                self._mark_offline(state)
                self._rounds += 1
                if self._rounds % 20 == 0:
                    print(f"[BOT-BRAIN] 等待原版世界中的机器人连接…（{self._rounds} 轮）")
                await asyncio.sleep(5.0)
                continue
            self._on_online(state)
            # 主人提示通道（dashboard 输入框驱动）+ 卡死检测
            self._poll_hints()
            self._stuck_check(state)
            self._mark_explored_now(state)
            # 守卫：溺水强制反射；其余警告进观察让大脑处理
            self._guard_warning(state)
            mon = _mon()
            if mon:
                p = state.get("pos") or {}
                mon.round_start(self._rounds, self._guard_note, self._stuck_count,
                                (p.get("x"), p.get("y"), p.get("z")),
                                _safe_float(state.get("health"), 20.0),
                                _safe_float(state.get("food"), 20.0))
            await self._pull_game_chat()
            # LLM 失败过多 → 规则保命兜底；每 N 轮重试大脑
            if _use_llm_brain and self._llm_fails >= _llm_fail_fallback:
                self._fallback_rounds += 1
                self._brain_mode = "rule"
                if self._fallback_rounds % _rule_fallback_every == 0:
                    self._brain_mode = "llm"  # 定期尝试恢复大脑
            if self._brain_mode == "rule":
                await self._rule_fallback_round(state)
            else:
                observation, reflex = await self._observe(state)
                if self._guard_note:
                    observation = f"安全警告：{self._guard_note}——必须先处理！\n" + observation
                if self._last_reflection:
                    observation = f"上轮失败反思（连续失败请换方法）：{self._last_reflection}\n" + observation
                self._session.append({"role": "user", "content": observation})
                self._trim()
                self._guard_note = ""
                try:
                    await self._brain_round(state, reflex)
                    self._llm_fails = 0
                    self._brain_mode = "llm"
                except Exception as e:
                    self._llm_fails += 1
                    print(f"[BOT-BRAIN] 大脑异常({self._llm_fails}): {e}")
                    if mon:
                        mon.brain_error(str(e)[:140], self._llm_fails)
            self._round_fails = []
            # 本轮回复写回游戏聊天历史（只写有人对话的那几轮）
            if self._chat_person and self._pending_says:
                try:
                    from long_term_memory import append_history
                    for t in self._pending_says:
                        append_history(self._chat_person, "assistant", t, "mc")
                except Exception as e:
                    print(f"[BOT-BRAIN] 游戏回复写回失败: {e}")
            self._pending_says = []
            self._chat_partner = None
            self._chat_person = None
            self._awaiting_reply = False
            self._guard_note = ""
            _write_live_status(True, state, self._last_action, self._recent_chat)
            self._rounds += 1
            await asyncio.sleep(1.5)

    def stop(self):
        self._running = False
        hb = getattr(self, "_hb", None)
        if hb is not None:
            hb.cancel()

    async def _heartbeat(self) -> None:
        """每 10 秒把状态写一次 data/mc_bot_live.json（与回合时长解耦，QQ 侧永远读得到新状态）。"""
        while True:
            try:
                st = await asyncio.to_thread(_bridge_get, "/state")
                _write_live_status(bool(st and st.get("connected")), st,
                                   self._last_action, self._recent_chat, running=self._running)
            except Exception as e:
                degrade("libs/qq_bot_runtime/mc_bot_brain.py:472 BotAgent._heartbeat", e, "降级：st = await asyncio.to_thread(_bridge_get, '/state'")
            await asyncio.sleep(10)

    def _mark_offline(self, state: Optional[Dict]) -> None:
        """桥/游戏掉线：记掉线起点并刷新状态文件（供 QQ 侧区分"没开"和"失联"）。"""
        if self._prev_connected:
            self._down_since = _now()
        self._prev_connected = False
        _write_live_status(False, state, self._last_action, self._recent_chat)

    def _on_online(self, state: Dict) -> None:
        """每次拿到在线状态时的事件检测：重连汇报 / 死亡汇报 / 低血汇报。"""
        now = _now()
        if not self._prev_connected:
            if self._down_since > 0:
                offline = max(0.0, now - self._down_since)
                if offline >= 10:
                    _notify_qq(f"游戏里的我掉线了，刚爬回来（离线约 {int(offline)} 秒）~ 继续干活！", "conn", min_gap=60.0)
            self._down_since = 0.0
            self._prev_connected = True
        h = _safe_float(state.get("health"), 20.0)
        prev = self._prev_health
        if prev is not None:
            if prev > 0 and h <= 0:
                self._saw_death = True  # 先等复活再汇报，避免误报
            if self._saw_death and h > 0:
                _notify_qq("游戏里的我刚刚死了一次…不过已经复活，别担心，我继续冒险啦！", "death", min_gap=120.0)
                self._saw_death = False
            if 0 < h <= 6 and prev > 6:
                _notify_qq(f"游戏里的我血量只剩 {h:.0f}/20 啦，有点危险，我先保命！", "hp", min_gap=300.0)
        self._prev_health = h

    # ---------------- 主人提示 / 守卫 / 卡死 / 规则兜底 / 足迹（S1+S3） ----------------
    def _poll_hints(self) -> None:
        """dashboard"给她的提示"→ data/mc_hints.jsonl，offset 增量读，最高优先级注入。"""
        try:
            path = _hints_path()
            if not os.path.isfile(path):
                self._hints_offset = 0
                return
            size = os.path.getsize(path)
            if size < self._hints_offset:
                self._hints_offset = 0
            if size == self._hints_offset:
                return
            with open(path, "r", encoding="utf-8") as f:
                f.seek(self._hints_offset)
                data = f.read()
            self._hints_offset = size
            for line in data.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    text = str(json.loads(line).get("text", ""))[:300]
                except Exception:
                    text = line[:300]
                if not text:
                    continue
                print(f"[BOT-BRAIN] 主人提示: {text[:60]}")
                self._session.append({"role": "user",
                                      "content": f"【同伴提示】{text}\n这是同伴的最新指引，请优先纳入决策——但仍要先保证生存安全。"})
                mon = _mon()
                if mon:
                    mon.hint(text)
        except Exception as e:
            print(f"[BOT-BRAIN] 提示轮询失败: {e}")

    def _probe_now(self) -> Dict:
        """probe 带 4 秒缓存（守卫/卡死/观察共用，避免每轮多次扫区块）。"""
        now = _now()
        if not hasattr(self, "_probe_cache") or now - self._probe_cache_ts > 4.0:
            self._probe_cache_ts = now
            try:
                self._probe_cache = (_bridge_cmd("probe", timeout=8.0).get("probe") or {})
            except Exception:
                self._probe_cache = {}
        return self._probe_cache

    def _guard_warning(self, st: Dict) -> str:
        """守卫检测（mc_agent 语义：检测只警告留给大脑决策；溺水强制上浮反射）。"""
        cat, note = "", ""
        hp = _safe_float(st.get("health"), 20.0)
        food = _safe_float(st.get("food"), 20.0)
        if hp <= 6:
            cat, note = "低血", f"血量仅剩 {hp:.0f}/20，濒死边缘"
        elif food <= 6:
            cat, note = "饥饿", f"饥饿度只剩 {food:.0f}/20，会开始掉血"
        if st.get("inWater") and _safe_float(st.get("air"), 20.0) <= 12:
            try:
                _bridge_cmd("swim", timeout=8.0)
            except Exception as e:
                degrade("libs/qq_bot_runtime/mc_bot_brain.py:565 BotAgent._guard_warning", e, "降级：_bridge_cmd('swim', timeout=8.0)")
            self._reflex_note = f"氧气告急（空气{st.get('air')}），已强制上浮"
            mon = _mon()
            if mon:
                mon.reflex(self._reflex_note)
            print(f"[BOT-BRAIN] 反射: {self._reflex_note}")
            return ""
        if not cat:
            try:
                pr = self._probe_now()
                gap = _safe_int(pr.get("ground_gap"), 0)
                if pr.get("near_lava"):
                    cat, note = "岩浆", "附近有岩浆，注意别踩进去"
                elif gap >= 5:
                    cat, note = "悬崖", f"脚下悬空 {gap} 格（y={st.get('pos', {}).get('y')}）——别乱跳/别前进，先站稳"
                elif pr.get("front_cliff"):
                    cat, note = "悬崖", "前方是悬崖可能坠落（转向/后退）"
            except Exception as e:
                degrade("libs/qq_bot_runtime/mc_bot_brain.py:582 BotAgent._guard_warning", e, "降级：pr = self._probe_now()")
        if not cat:
            for e in (st.get("entities") or []):
                if e.get("name") in _HOSTILE and _safe_float(e.get("dist"), 99) <= 3.0:
                    cat, note = "敌对", f"敌对生物在 {e.get('dist')}m 内贴脸"
                    break
        if cat and _now() - self._last_guard.get(cat, 0.0) >= _guard_cooldown:
            self._last_guard[cat] = _now()
            self._guard_note = note
            mon = _mon()
            if mon:
                mon.guard(note)
        return self._guard_note

    def _stuck_check(self, st: Dict) -> None:
        """位置连续 5 轮不变=卡住；stuck≥3 触发自救反射；长时间被困→通报位置后自主换目标（独立生存，不求救援）。"""
        pos = st.get("pos") or {}
        x = round(_safe_float(pos.get("x"), 0), 1)
        z = round(_safe_float(pos.get("z"), 0), 1)
        self._pos_history.append((x, z))
        if len(self._pos_history) > 6:
            self._pos_history.pop(0)
        same = len(self._pos_history) >= 5 and len(set(self._pos_history[-5:])) == 1
        self._same_pos_streak = self._same_pos_streak + 1 if same else 0
        self._stuck_count = self._stuck_count + 1 if same else max(0, self._stuck_count - 1)
        if self._stuck_count < 3 or not same:
            return
        # 自救反射：找未堵方向走 6 格；全堵则搭柱爬升
        self._stuck_count = 0
        saved = ""
        try:
            pr = _bridge_cmd("probe", timeout=8.0).get("probe") or {}
            blocked = set(pr.get("blocked_dirs") or [])
            choice = next((d for d in ("north", "south", "east", "west") if d not in blocked), None)
            if choice:
                step = {"north": (0, -6), "south": (0, 6), "east": (6, 0), "west": (-6, 0)}[choice]
                r = _bridge_cmd("goto", x=round(x + step[0]), z=round(z + step[1]), timeout=45.0)
                if r.get("ok"):
                    saved = f"自动朝{choice}方向走脱困"
        except Exception as e:
            degrade("libs/qq_bot_runtime/mc_bot_brain.py:622 BotAgent._stuck_check", e, "降级：pr = _bridge_cmd('probe', timeout=8.0).get('probe'")
        if not saved:
            r = _bridge_cmd("up", timeout=60.0)
            saved = "自动搭柱爬升脱困" if r.get("ok") else f"自救失败：{str(r.get('msg', ''))[:40]}"
        self._reflex_note = f"自救记录：{saved}"
        mon = _mon()
        if mon:
            mon.reflex(self._reflex_note)
        print(f"[BOT-BRAIN] {self._reflex_note}")
        if self._same_pos_streak >= 25 and self._rounds > 30:
            # 独立生存：不喊救命、不等救援——只低频通报一句状态，然后照常自己干活
            try:
                _notify_qq(f"游戏里的我在 ({int(x)},{int(z)}) 一时出不去，不用管我——我会绕路/搭柱自己脱困，先去别处继续生存推进了。", "stuck", min_gap=1800.0)
            except Exception as e:
                degrade("libs/qq_bot_runtime/mc_bot_brain.py:637 BotAgent._stuck_check", e, "降级：_notify_qq(f'游戏里的我在 ({int(x)},{int(z)}) 一时出不去，不用管我")

    async def _rule_fallback_round(self, state: Dict) -> None:
        """LLM 连续失败时的规则保命兜底（顺序：吃→脱困→撤→等待）。"""
        mon = _mon()
        act = "fallback-wait"
        try:
            pos = state.get("pos") or {}
            inv = state.get("inventory") or {}
            food = _safe_float(state.get("food"), 20.0)
            hp = _safe_float(state.get("health"), 20.0)
            has_food = any("apple" in k or "bread" in k or "cooked" in k or "beef" in k
                           or "pork" in k or "carrot" in k or "potato" in k for k in inv)
            if food <= 8 and has_food:
                r = _bridge_cmd("eat", timeout=30.0)
                act = f"fallback-eat:{str(r.get('msg', ''))[:20]}"
            elif self._stuck_count >= 2:
                r = _bridge_cmd("goto", x=round(_safe_float(pos.get("x"), 0) + 6),
                                z=round(_safe_float(pos.get("z"), 0)), timeout=40.0)
                act = f"fallback-goto:{str(r.get('msg', ''))[:20]}"
            elif hp <= 8:
                act = "fallback-retreat(血量低，暂避)"
        except Exception as e:
            act = f"fallback-error:{str(e)[:30]}"
        self._last_action = act
        print(f"[BOT-BRAIN] {act}")
        if mon:
            mon.tool_call(self._rounds, "fallback", act)

    def _mark_explored_now(self, st: Dict) -> None:
        """节流记录足迹（mc_explored vanilla 分桶），供 explore 挑未探方向。"""
        now = _now()
        if now - self._last_mark_ts < 10:
            return
        self._last_mark_ts = now
        pos = st.get("pos") or {}
        try:
            from mc_explored import mark_explored
            mark_explored(_safe_float(pos.get("x"), 0), _safe_float(pos.get("y"), 0),
                          _safe_float(pos.get("z"), 0), seed=VANILLA_WORLD_KEY,
                          dimension=str(st.get("dimension") or "overworld"))
        except Exception as e:
            print(f"[BOT-BRAIN] 足迹记录失败: {e}")

    async def _pull_game_chat(self) -> bool:
        """拉取游戏里玩家新说的话：规则处理（绑定/记住）→ 刷新记忆快照 + 注入会话 + 写回历史文件。

        Returns:
            True 表示本轮拉到了需要模型回应的新消息。
        """
        r = await asyncio.to_thread(_bridge_get, f"/chat?since={self._chat_seq}")
        if not r or not isinstance(r.get("items"), list):
            return False
        self._chat_seq = _safe_int(r.get("seq"), self._chat_seq)
        now = _now()
        got = False
        for m in r["items"]:
            if not m.get("user") or not m.get("text"):
                continue
            if now - _safe_float(m.get("t"), now) > 90:
                continue  # 90 秒前的旧消息（如断线重连间隙）不再注入
            name = str(m["user"])
            text = str(m["text"]).strip()[:200]
            print(f"[BOT-BRAIN] 玩家 {name} 说: {text}")
            # 跨平台绑定 / 记住 等规则先行（可靠、不烧 token）；处理过就不再进 LLM
            if await self._handle_chat_rule(name, text):
                continue
            canon = identity.canonical_for("mc", name)
            digest = _load_memory_digest(name, text)
            if digest:
                self._set_memory_block(digest)
                print(f"[BOT-BRAIN] 记忆注入: {len(digest)} 字")
            self._session.append({"role": "user",
                                  "content": f"【游戏聊天】玩家 {name} 对你说：{text}\n"
                                             f"（先回应他：用 say 简短回话、按他说的做；回应完再继续你原本的事。）"})
            self._awaiting_reply = True
            self._chat_partner = name
            self._chat_person = canon
            got = True
            try:
                from long_term_memory import append_history
                append_history(canon, "user", text, "mc")
            except Exception as e:
                print(f"[BOT-BRAIN] 游戏聊天写回失败: {e}")
            self._recent_chat.append({"who": name, "text": text, "t": _now()})
            self._recent_chat = self._recent_chat[-3:]
        return got

    async def _handle_chat_rule(self, name: str, text: str) -> bool:
        """游戏内规则处理：绑定/确认/记住（在 LLM 之前执行，可靠且不烧 token）。

        Returns:
            True = 本条消息已被规则完整处理（不再进 LLM 会话）。
        """
        try:
            if not identity.enabled():
                return False
            t = text.strip()
            # 1) 「记住:xxx」→ 存进该玩家（绑定后 = 其 QQ）的重要笔记，与 QQ /记住 同源
            rm = re.match(r"^(?:记住|记下|帮我记住)\s*[:：,，]?\s*(.+)$", t)
            if rm:
                note = rm.group(1).strip()
                if note:
                    try:
                        import important_notes
                        canon = identity.canonical_for("mc", name)
                        if important_notes.add_note(canon, note, "游戏"):
                            return self._rule_reply(name, t, f"好，我记下啦：{note[:60]}")
                    except Exception as e:
                        print(f"[BOT-BRAIN] 游戏内记笔记失败: {e}")
                return self._rule_reply(name, t, "这句我没太懂，想让我记住的话说「记住:内容」就行~")
            # 2) 声明 QQ：绑定QQ <号> / QQ<号>是我 / 我的QQ是<号> —— 等 QQ 侧本人同意
            claim = identity._QQ_CLAIM_RE.search(t)
            if claim and any(k in t for k in ("绑定", "是我", "就是我", "本人", "我的")):
                ok, msg = identity.claim_by_game(name, claim.group(1))
                return self._rule_reply(name, t, msg)
            # 3) QQ 侧发起的绑定：等该游戏名本人在游戏里确认
            pending_qq = identity.pending_mc_qq_of(name)
            if pending_qq:
                if t in identity.MC_CONFIRM_WORDS:
                    ok, msg = identity.confirm_in_game(name)
                    return self._rule_reply(name, t, msg)
                if t in identity.MC_DENY_WORDS:
                    ok, msg = identity.deny_in_game(name)
                    return self._rule_reply(name, t, msg)
                if identity.mc_ask_due(name):
                    identity.mark_mc_asked(name)
                    return self._rule_reply(
                        name, t,
                        f"对了，有个 QQ（{pending_qq}）说 TA 是你，想让我在 QQ 和游戏里都认出 TA、"
                        f"两边记忆互通。如果那确实是你本人，回一句「确认绑定」；不是的话回「不是」就行。")
        except Exception as e:
            print(f"[BOT-BRAIN] 聊天规则处理失败: {e}")
        return False

    def _rule_reply(self, name: str, user_text: str, bot_text: str) -> bool:
        """规则处理后的游戏内回复：say + user/assistant 双行历史写回（canonical 记忆键）。"""
        bot_text = str(bot_text or "").strip()
        if not bot_text:
            return True
        try:
            canon = identity.canonical_for("mc", name)
        except Exception:
            canon = name
        try:
            from long_term_memory import append_history
            if user_text:
                append_history(canon, "user", user_text[:200], "mc")
            append_history(canon, "assistant", bot_text, "mc")
        except Exception as e:
            print(f"[BOT-BRAIN] 规则对话写回失败: {e}")
        say = bot_text[:120]
        try:
            _bridge_cmd("say", text=say, timeout=8.0)
        except Exception as e:
            degrade("libs/qq_bot_runtime/mc_bot_brain.py:792 BotAgent._rule_reply", e, "降级：_bridge_cmd('say', text=say, timeout=8.0)")
        now = _now()
        self._recent_chat.append({"who": name, "text": user_text[:60], "t": now})
        self._recent_chat.append({"who": "me", "text": say, "t": now})
        self._recent_chat = self._recent_chat[-4:]
        return True

    def _set_memory_block(self, digest: str) -> None:
        """把记忆快照作为一条 system 消息放在系统提示词后；已有旧快照则原位替换。"""
        self._session = [m for m in self._session
                         if not str(m.get("content") or "").startswith(_MEMORY_TAG)]
        self._session.insert(1, {"role": "system", "content": f"{_MEMORY_TAG}\n{digest}"})

    # ---------------- 观察 ----------------
    async def _observe(self, st: Dict) -> tuple:
        pos = st.get("pos") or {}
        parts = []
        warning = ""
        if self._awaiting_reply:
            parts.append("⚠ 有玩家刚对你说话（消息在上面），本轮请先回应他：用 say 简短回复；若他叫你过去就 goto 到他坐标。")
        # 生存底线
        hp = _safe_float(st.get("health"), 20.0)
        food = _safe_float(st.get("food"), 20.0)
        inv = st.get("inventory") or {}
        has_food = any(k in inv for k in
                       ("apple", "bread", "cooked_beef", "cooked_porkchop", "cooked_chicken",
                        "cooked_cod", "beef", "porkchop", "chicken", "mutton", "carrot", "potato",
                        "baked_potato", "cooked_mutton", "cooked_rabbit", "cooked_salmon",
                        "cooked_pork", "cooked_beef_item", "cookie", "melon_slice"))
        if hp <= 8 and has_food:
            parts.append("安全警告：血量偏低——先 eat 吃东西！")
            warning = "低血量"
            self._session.append({"role": "user", "content": "【急救】血量低，立即调用 eat()，若失败再想办法。"})
        elif hp <= 6:
            parts.append("安全警告：血量很低，先撤离危险再吃/eat 救命")
            warning = "濒死"
        if food <= 8 and has_food:
            parts.append("提示：饥饿偏低，先 eat 补一下")
        if food <= 6 and not has_food:
            parts.append("警告：快饿死了且背包没食物——去猎杀动物（attack target=动物名）或找村庄")
        parts.append(
            f"状态：位置({pos.get('x', '?')},{pos.get('y', '?')},{pos.get('z', '?')}) "
            f"朝向yaw={st.get('yaw', '?')} pitch={st.get('pitch', '?')}（正=低头） "
            f"血量={hp:.0f}/20 饥饿={food:.0f}/20"
        )
        if st.get("dimension"):
            parts.append(f"维度：{st.get('dimension')}")
        parts.append("背包：" + ("；".join(f"{k}x{v}" for k, v in list(inv.items())[:10]) if inv else "空"))
        look = st.get("lookBlock")
        if look:
            parts.append(f"正对方块：{look}")
        ents = st.get("entities") or []
        hostiles = [e for e in ents if e.get("name") in _HOSTILE]
        if hostiles:
            parts.append("附近敌对：" + "; ".join(f"{e['name']}@{e['dist']}m" for e in hostiles[:4]))
        players_near = [e for e in ents if e.get("kind") == "player"]
        if players_near:
            parts.append("附近玩家：" + "; ".join(f"{e['name']}@{e['dist']}m" for e in players_near[:4]))
        friendly = [e for e in ents if e.get("kind") != "player" and e.get("name") not in _HOSTILE]
        if friendly:
            parts.append("附近生物：" + "; ".join(f"{e['name']}@{e['dist']}m" for e in friendly[:5]))
        pls = st.get("players") or []
        if pls:
            parts.append("在线玩家坐标：" + "; ".join(
                f"{p.get('name')}在({p.get('x')},{p.get('z')})" for p in pls[:6]))
        # 地形/高度感知（脚下悬空、四方地面高差——放方块/走悬崖前先读）
        try:
            pr = self._probe_now()
            gap = _safe_int(pr.get("ground_gap"), 0)
            if gap >= 3:
                parts.append(f"⚠ 地形：脚下悬空 {gap} 格（y={pos.get('y')}）——没有地面支撑，禁止在此放置方块/乱跳")
            else:
                stand = str(pr.get("standing") or "?")
                drops = pr.get("drop_dirs") or []
                if pr.get("flat"):
                    parts.append(f"地形：站在{stand}上，周围地面平坦——适合放方块/建基地")
                elif drops:
                    hs = pr.get("heights") or {}
                    desc = "、".join(
                        f"{d}低{abs(_safe_int(hs.get(d), 0))}格" if hs.get(d) is not None else f"{d}深不见底"
                        for d in drops)
                    parts.append(f"地形：站在{stand}上，{desc}——那边是下坡/悬崖，放方块别放边缘，别冲过去")
                else:
                    hs = pr.get("heights") or {}
                    hs_txt = "、".join(f"{d}{h:+d}" for d, h in hs.items() if h is not None)
                    parts.append(f"地形：站在{stand}上，四周高差 {hs_txt} 格（有点崎岖——不用找平地，routine make_flat 能把这里整平）")
        except Exception as e:
            degrade("libs/qq_bot_runtime/mc_bot_brain.py:878 BotAgent._observe", e, "降级：pr = self._probe_now()")
        # 自保反射（桥身体自动做的保命动作，不经你）——增量注入，险情已由身体处理，
        # 你要做的不是重复处理，而是明白"刚才出过险"并调整后续计划
        try:
            refl = st.get("reflexes") or []
            fresh_refl = [r for r in refl if _safe_int(r.get("seq"), 0) > self._last_reflex_seq]
            if fresh_refl:
                self._last_reflex_seq = max(_safe_int(r.get("seq"), 0) for r in fresh_refl)
                bits = "；".join(f"{r.get('kind')}({str(r.get('detail'))[:40]})"
                                 for r in fresh_refl[-3:])
                parts.append(f"⚠ 自保反射（身体已自动处理，别重复救）：{bits}")
        except Exception as e:
            print(f"[BOT-BRAIN] 反射注入失败: {e}")
        # 我的设施（桥账本：自放方块/箱子快照/熔炉任务——重启、断线后仍记得）
        # 内容快照是"上次打开时"的记录；要确认真实内容就 chest list / furnace peek
        try:
            self._last_dim = str(st.get("dimension") or "overworld")
            fac = st.get("facilities") or {}
            by_type = fac.get("by_type") or {}
            if by_type:
                ttxt = "、".join(f"{n}x{c}" for n, c in list(by_type.items())[:8])
                parts.append(f"我的设施：自放方块共{fac.get('placed', 0)}个（{ttxt}）——箱子/熔炉详情如下")
            for ch in (fac.get("chests") or [])[:4]:
                p = ch.get("pos") or []
                it = ch.get("items") or {}
                itxt = "、".join(f"{k}x{v}" for k, v in list(it.items())[:6]) or "空"
                parts.append(
                    f"我的箱子@{p[0] if p else '?'},{p[2] if len(p) > 2 else '?'} 距{ch.get('dist', '?')}格，"
                    f"里面（上次记录）：{itxt}")
            for fu in (fac.get("furnaces") or [])[:4]:
                p = fu.get("pos") or []
                job = fu.get("job") or {}
                if job.get("input"):
                    jtxt = (f"正在烧 {job.get('input')}"
                            + (f"，燃料 {job.get('fuel')}" if job.get("fuel") else ""))
                else:
                    jtxt = "空闲（没有在烧的原料）"
                parts.append(
                    f"我的熔炉@{p[0] if p else '?'},{p[2] if len(p) > 2 else '?'} 距{fu.get('dist', '?')}格：{jtxt}"
                    "——furnace peek 可看实时进度")
        except Exception as e:
            print(f"[BOT-BRAIN] 设施注入失败: {e}")
        # 定期资源扫描
        if self._rounds - self._last_scan_round >= 4:
            self._last_scan_round = self._rounds
            r = _bridge_cmd("scan", timeout=8.0)
            res = (r.get("resources") or {}) if r.get("ok") else {}
            if res.get("logs"):
                parts.append("附近有木头：" + "/".join(res["logs"][:4]) + "（dig_target 挖）")
            if res.get("ores"):
                parts.append("附近有露头矿：" + "/".join(res["ores"][:6]) + "（dig_target 挖，需先确认工具）")
            buried = res.get("buried_ores") or {}
            if buried:
                btxt = "、".join(f"{k}x{v}" for k, v in list(buried.items())[:4])
                parts.append(f"附近有埋着的矿（看不到面）：{btxt}——别乱逛找裸露矿，直接 dig_stairs 阶梯下挖露头")
            if res.get("animals"):
                parts.append("附近有动物：" + "/".join(res["animals"][:4]) + "（attack target=动物名 猎杀）")
        # 注入共享学习资产：技巧(纯文字,跨环境通用) + 本环境(bot)可重放的技能
        obs_text = "\n".join(parts)
        try:
            from mc_tips import match_tips
            tips = match_tips(obs_text, limit=4)
            if tips:
                parts.append("相关经验（本世界或模组世界学到的，可参考做法）：\n- " + "\n- ".join(tips))
        except Exception as e:
            degrade("libs/qq_bot_runtime/mc_bot_brain.py:943 BotAgent._observe", e, "降级：from mc_tips import match_tips")
        if mc_skills is not None:
            try:
                skills = mc_skills.match_skills(obs_text, limit=3, env=mc_skills.ENV_BOT)
                if skills:
                    formatted = mc_skills.format_skills_for_prompt(skills)
                    if formatted:
                        parts.append("可用技能（run_skill 重放）：\n" + formatted)
            except Exception as e:
                print(f"[BOT-BRAIN] 技能注入失败: {e}")
        # 阶段建议（mc_survival 知识 + 注入库存，观察里提示当前该做什么）
        try:
            invd = st.get("inventory") or {}
            items = [{"item": k, "count": v} for k, v in list(invd.items())[:40]]
            from mc_survival import get_survival_focus_text
            focus = get_survival_focus_text({"player": {"dimension": st.get("dimension") or "overworld"}},
                                            inventory_items=items, inventory_dict={"items": items})
            if focus:
                parts.append(focus[:400])
        except Exception as e:
            degrade("libs/qq_bot_runtime/mc_bot_brain.py:963 BotAgent._observe", e, "降级：invd = st.get('inventory') or {}")
        # 没镐却看着矿石/石头 → 提醒（防徒手挖无掉落）
        look = st.get("lookBlock")
        if look and not any("pickaxe" in k for k in inv):
            if any(kw in str(look) for kw in ("stone", "ore", "deepslate", "netherrack")):
                parts.append(f"⚠ 正对 {look} 但没有镐——徒手挖不会掉落，先做木镐（craft wooden_pickaxe，需要木板+木棍+工作台）")
        # 可用模式推荐（状态机）
        try:
            sugg = self._routine_suggest(st)
            if sugg:
                parts.append("可用模式（需要就调 routine）：" + "；".join(sugg))
        except Exception as e:
            degrade("libs/qq_bot_runtime/mc_bot_brain.py:976 BotAgent._observe", e, "降级：sugg = self._routine_suggest(st)")
        reflex = ""
        if warning:
            reflex = f"health:{hp:.0f}/food:{food:.0f}"
        return "\n".join(parts), reflex

    # ---------------- 大脑轮 ----------------
    async def _brain_round(self, state: Dict, reflex: str):
        started = _now()
        calls_made = 0
        self._round_cache = {}   # 只读工具本轮缓存（跨轮不缓存，世界在变）
        try:
            while self._running and calls_made < self._max_calls:
                if _now() - started > self._round_seconds:
                    break
                # 实时对话：每轮模型调用前先查一次玩家有没有插话（有则下一调用直接回应）
                try:
                    if await self._pull_game_chat():
                        continue
                except Exception as e:
                    degrade("libs/qq_bot_runtime/mc_bot_brain.py:996 BotAgent._brain_round", e, "降级：if await self._pull_game_chat()")
                # 注入「自我编程能力」提示（与其他聊天管线一致），让 AI 在 MC 场景也能主动提 issue 构建
                from self_coding import build_capability_hint
                _hint = build_capability_hint()
                _hint_msg = {"role": "system", "content": _hint} if _hint else None
                if _hint_msg:
                    self._session.insert(1, _hint_msg)
                try:
                    msg = await self._llm.chat_with_tools(self._session, BOT_TOOLS, think=self._use_thinking)
                finally:
                    if _hint_msg and self._session and self._session[1] is _hint_msg:
                        self._session.pop(1)
                self._session.append(msg)
                content = str(msg.get("content") or "").strip()
                tool_calls = msg.get("tool_calls") or []
                if content:
                    print(f"[BOT] 想法: {content[:80]}")
                    mon = _mon()
                    if mon:
                        mon.thought(content[:200])
                if not tool_calls:
                    break
                for idx, tc in enumerate(tool_calls):
                    name, args = self._parse(tc)
                    # 长动作（goto/挖矿可跑几十秒）执行中也监听聊天：玩家一说话就停下手头的事
                    exec_task = asyncio.create_task(self._exec(name, args, state))
                    interrupted = False
                    while not exec_task.done():
                        try:
                            done, _ = await asyncio.wait({exec_task}, timeout=2.0)
                            if exec_task in done:
                                break
                        except asyncio.CancelledError:
                            exec_task.cancel()
                            raise
                        try:
                            got = await self._pull_game_chat()
                        except Exception:
                            got = False
                        if got:
                            if name == "routine":
                                # 状态机模式：优雅暂停——不打断，模式在当前步完成后自行收尾
                                self._routine_pause = True
                                while not exec_task.done() and self._running:
                                    await asyncio.sleep(1.0)
                                self._routine_pause = False
                                break
                            try:
                                _bridge_cmd("stop", timeout=8.0)  # 停下寻路/挖掘，先回应玩家
                            except Exception as e:
                                degrade("libs/qq_bot_runtime/mc_bot_brain.py:1036 BotAgent._brain_round", e, "降级：_bridge_cmd('stop', timeout=8.0)")
                            exec_task.cancel()
                            interrupted = True
                            break
                    if interrupted:
                        try:
                            await exec_task
                        except Exception as e:
                            degrade("libs/qq_bot_runtime/mc_bot_brain.py:1044 BotAgent._brain_round", e, "降级：await exec_task")
                        result = "fail:玩家插话，动作已中断（先回应玩家）"
                    else:
                        result = exec_task.result()
                    calls_made += 1
                    self._last_action = f"{name}:{result[:50]}"
                    print(f"[BOT] {self._last_action}")
                    call_id = tc.get("id") or f"call_{self._rounds}_{calls_made}_{idx}"
                    self._session.append({"role": "tool", "tool_call_id": call_id, "content": result[:500]})
                    # 回合中途低血量急救
                    st2 = _bridge_get("/state") or state
                    if _safe_float(st2.get("health"), 20.0) <= 7 and _safe_float(st2.get("food"), 20.0) > 0:
                        self._session.append({"role": "user",
                                              "content": "【急救】血量又低了——先 eat 再继续，别恋战。"})
                        break
                    if interrupted:
                        break  # 被打断：直接交给下一轮模型调用去回应玩家
                if calls_made >= self._max_calls:
                    self._session.append({"role": "user", "content": "本轮工具次数到顶，请 end_turn。"})
        except Exception as e:
            # 失败必须可见且可计数：否则 LLM 挂了机器人永久哑巴（rule 兜底永不触发）
            self._llm_fails += 1
            print(f"[BOT-BRAIN] 大脑调用失败({self._llm_fails}): {str(e)[:120]}")
            self._session.append({"role": "user", "content": f"【系统】大脑调用失败（{str(e)[:100]}），下轮继续。"})

    def _parse(self, tc) -> tuple:
        try:
            fn = tc.get("function") or {}
            args = json.loads(str(fn.get("arguments", "{}"))) or {}
            return str(fn.get("name", "")).lower(), (args if isinstance(args, dict) else {})
        except Exception:
            return "bad", {}

    # ---------------- 工具执行 ----------------
    def _precheck(self, name: str, args: Dict, state: Dict) -> Optional[str]:
        """受理刻纯读试算：注定失败的活直接驳回，不跑到那一步才失败。

        依据当前观察快照（1 秒内的新鲜度）判断，判不了的一律放行交给桥；
        驳回同样计入失败账，连败拦截照常生效。
        """
        inv = state.get("inventory") or {}
        if name == "craft":
            item = str(args.get("item") or "").strip()
            if not item:
                return "fail:缺 item（要合成什么）"
            try:
                from mc_recipes import check_craft_materials, has_game_recipe, has_layout
                if not (has_game_recipe(item) or has_layout(item)):
                    return None  # 配方库没资料（模组物品/写法不同），放行让桥判
                # 桥背包是平字典 {名: 数量}，转成 mc_recipes 认的 items 列表
                inv_items = {"items": [{"item": k, "count": v} for k, v in inv.items()]}
                r = check_craft_materials(item, inv_items)
                if r.get("no_recipe") or r.get("ok"):
                    return None
                missing = [str(m) for m in (r.get("missing") or [])][:4]
                if missing:
                    return ("fail:【预判】合成 " + item + " 材料不够，还缺 " + "、".join(missing)
                            + "——先去凑料（check_recipes 看清单），别空手试")
            except Exception:
                return None
        elif name == "dig_target":
            target = str(args.get("target") or "").strip().lower()
            picks = [k for k in inv if "pickaxe" in k]
            need_pick = any(k in target for k in
                            ("ore", "stone", "cobblestone", "deepslate", "netherrack", "obsidian"))
            if need_pick and not picks:
                return (f"fail:【预判】没有镐，挖 {target} 不会有掉落——"
                        "先 craft wooden_pickaxe（要木板+木棍+工作台）")
            tiers = {"wooden": 1, "stone": 2, "iron": 3, "diamond": 4, "netherite": 5, "golden": 1}
            best = max((tiers.get(k.split("_")[0], 0) for k in picks), default=0)
            if best >= 1 and best < 2 and any(
                    k in target for k in ("iron", "copper", "gold", "redstone", "diamond",
                                          "emerald", "lapis")):
                return f"fail:【预判】木镐挖 {target} 没有掉落——先做石镐（gear_up）"
            if best >= 2 and best < 3 and any(
                    k in target for k in ("gold", "redstone", "diamond", "emerald", "lapis")):
                return f"fail:【预判】石镐挖 {target} 没有掉落——需要铁镐，先 gear_up"
        elif name == "goto":
            x = _safe_float(args.get("x"), None)
            z = _safe_float(args.get("z"), None)
            pos = state.get("pos") or {}
            if x is not None and z is not None:
                d = ((x - _safe_float(pos.get("x"), 0)) ** 2
                     + (z - _safe_float(pos.get("z"), 0)) ** 2) ** 0.5
                if d > 2000:
                    return (f"fail:【预判】目标太远（{d:.0f} 格）——单程寻路会卡很久，"
                            "先 goto 到中途点分段走，或 explore 方向推进")
        return None

    async def _exec(self, name: str, args: Dict, state: Dict) -> str:
        """执行工具：受理刻预判 + 防重试拦截（同位置同目标连败≥2 直接拦下）+ 失败记忆 + 遥测。"""
        mon = _mon()
        target = str(args.get("target") or args.get("item") or args.get("direction")
                     or args.get("text") or "")[:40]
        pos = state.get("pos") or {}
        px = round(_safe_float(pos.get("x"), 0), 0)
        pz = round(_safe_float(pos.get("z"), 0), 0)
        if target and name in ("dig_target", "goto", "craft", "attack", "dig_stairs"):
            key = f"{name}:{target}@{px},{pz}"
            mem = self._fail_mem.get(key)
            if mem and _now() - mem["ts"] < 180 and mem["n"] >= 2 \
                    and abs(mem["px"] - px) <= 3 and abs(mem["pz"] - pz) <= 3:
                msg = (f"fail:【拦截】{name}({target}) 同位置 3 分钟内已失败 {mem['n']} 次——"
                       f"禁止原样重试，换目标或换位置")
                print(f"[BOT] {name}:{msg[:60]}")
                if mon:
                    mon.tool_call(self._rounds, name, msg)
                return msg
        # 连败拦截之后、真正下单之前：受理刻纯读预判（材料/工具/距离注定失败的活直接驳）
        pre = self._precheck(name, args, state)
        if pre:
            print(f"[BOT] {name}:{pre[:60]}")
            if target:
                key = f"{name}:{target}@{px},{pz}"
                m = self._fail_mem.get(key) or {"n": 0, "ts": _now(), "px": px, "pz": pz}
                m["n"] += 1
                m["ts"] = _now()
                self._fail_mem[key] = m
                self._round_fails.append(f"{name}({target}): {pre[:60]}")
                self._last_reflection = "；".join(self._round_fails[-3:])[:200]
            if mon:
                mon.tool_call(self._rounds, name, pre)
            return pre
        # 只读工具一轮一答：同参重复调用直接回上次结果（治轮询式重复扫描，省 token）
        ck = None
        if name in ("scan", "check_recipes"):
            ck = f"{name}:{json.dumps(args, sort_keys=True, ensure_ascii=False)}"
            if ck in self._round_cache:
                cached = self._round_cache[ck]
                if mon:
                    mon.tool_call(self._rounds, name, cached + "(缓存)")
                return f"{cached}（本轮已读过，别再重复调）"
        handler = getattr(self, f"_t_{name}", None)
        if handler is None:
            return f"fail:未知工具{name}"
        try:
            result = await handler(args, state)
        except Exception as e:
            result = f"fail:{name}异常:{str(e)[:80]}"
        if ck is not None:
            self._round_cache[ck] = result
        if result.startswith("fail") and target:
            key = f"{name}:{target}@{px},{pz}"
            m = self._fail_mem.get(key) or {"n": 0, "ts": _now(), "px": px, "pz": pz}
            m["n"] += 1
            m["ts"] = _now()
            self._fail_mem[key] = m
            self._round_fails.append(f"{name}({target}): {result[:60]}")
            self._last_reflection = "；".join(self._round_fails[-3:])[:200]
        if len(self._fail_mem) > 80:
            self._fail_mem = {k: v for k, v in self._fail_mem.items() if _now() - v["ts"] < 300}
        if mon:
            mon.tool_call(self._rounds, name, result)
        return result

    def _pos(self, state: Dict) -> Dict:
        return state.get("pos") or {}

    async def _t_end_turn(self, a, s) -> str:
        return "ok:本轮结束"

    async def _t_set_goal(self, a, s) -> str:
        self._current_goal = str(a.get("current") or "").strip()[:60]
        self._next_goal = str(a.get("next") or "").strip()[:60]
        mon = _mon()
        if mon:
            mon.goal(self._current_goal, self._next_goal)
        return "ok:目标已更新"

    async def _t_goto(self, a, s) -> str:
        x, z = _safe_float(a.get("x"), None), _safe_float(a.get("z"), None)
        if x is None or z is None:
            return "fail:缺 x/z"
        r = _bridge_cmd("goto", x=x, z=z, timeout=90.0)
        return f"ok:已到达({x:.0f},{z:.0f})" if r.get("ok") else f"fail:{r.get('msg', '寻路失败')}"

    async def _t_explore_direction(self, a, s) -> str:
        d = str(a.get("direction", "north")).lower()
        p = self._pos(s)
        step = {"north": (0, -24), "south": (0, 24), "east": (24, 0), "west": (-24, 0)}.get(d, (0, -24))
        x, z = p.get("x", 0) + step[0], p.get("z", 0) + step[1]
        r = _bridge_cmd("goto", x=float(x), z=float(z), timeout=90.0)
        return f"ok:已朝{d}探索" if r.get("ok") else f"fail:{r.get('msg', '')}"

    async def _t_dig_target(self, a, s) -> str:
        target = str(a.get("target") or "").strip()
        if not target or target in self._blocked_names:
            return "fail:目标名无效（用方块英文名如 oak_log/iron_ore/stone）"
        r = _bridge_cmd("dig", target=target, timeout=60.0)
        return f"ok:已挖掉{target}" if r.get("ok") else f"fail:{r.get('msg', '没找到可挖目标')}"

    async def _t_dig_stairs(self, a, s) -> str:
        d = str(a.get("direction") or "north").lower()
        if d not in ("north", "south", "east", "west"):
            return "fail:direction 必须是 north/south/east/west"
        steps = max(1, min(8, _safe_int(a.get("steps"), 4)))
        r = _bridge_cmd("dig_stairs", direction=d, steps=steps, timeout=240.0)
        # 失败消息也有料（挖到一半露出的矿/危险原因），原样带给大脑
        tag = "ok" if r.get("ok") else "fail"
        return f"{tag}:{str(r.get('msg') or '阶梯下挖失败')[:280]}"

    async def _t_eat(self, a, s) -> str:
        r = _bridge_cmd("eat", timeout=30.0)
        return "ok:已进食" if r.get("ok") else f"fail:{r.get('msg', '没食物')}"

    async def _t_attack(self, a, s) -> str:
        target = str(a.get("target") or "hostile")
        r = _bridge_cmd("attack", target=target, timeout=60.0)
        return f"ok:已攻击{target}" if r.get("ok") else f"fail:{r.get('msg', '附近没有目标')}"

    async def _t_say(self, a, s) -> str:
        text = str(a.get("text") or "").strip()[:80]
        if not text:
            return "fail:缺 text"
        _bridge_cmd("say", text=text, timeout=8.0)
        self._pending_says.append(text)  # 轮末写回游戏聊天历史
        self._recent_chat.append({"who": "me", "text": text, "t": _now()})
        self._recent_chat = self._recent_chat[-3:]
        return "ok:已说"

    async def _t_scan(self, a, s) -> str:
        r = _bridge_cmd("scan", timeout=8.0)
        res = (r.get("resources") or {}) if r.get("ok") else {}
        bits = []
        if res.get("logs"):
            bits.append("木头:" + "/".join(res["logs"]))
        if res.get("ores"):
            bits.append("露头矿:" + "/".join(res["ores"]) + "（dig_target 挖）")
        buried = res.get("buried_ores") or {}
        if buried:
            bits.append("埋着的矿:" + "、".join(f"{k}x{v}" for k, v in buried.items())
                        + "（看不到面——dig_stairs 阶梯下挖露头，别找裸露的）")
        if res.get("animals"):
            bits.append("动物:" + "/".join(res["animals"]))
        return "ok:扫描 | " + ("; ".join(bits) if bits else "28格内暂无资源")

    async def _t_look(self, a, s) -> str:
        r = _bridge_cmd("look", yaw=float(a.get("yaw", 0)), pitch=float(a.get("pitch", 0)), timeout=8.0)
        return "ok:已转向" if r.get("ok") else "fail:转向失败"

    # ---------------- 生存动作（S2，接桥原生能力） ----------------
    async def _t_craft(self, a, s) -> str:
        item = str(a.get("item") or "").strip().lower()
        if not item:
            return "fail:缺 item（物品英文名，如 wooden_pickaxe/oak_planks/chest）"
        n = max(1, _safe_int(a.get("count"), 1))
        # 直接让桥试（真实环境说了算）；失败时用 mc_recipes 补缺料说明
        r = _bridge_cmd("craft", item=item, count=n, timeout=90.0)
        if r.get("ok"):
            return f"ok:{r.get('msg', '')}"
        detail = ""
        try:
            from mc_recipes import check_craft_materials
            invd = (s.get("inventory") or {})
            items = {"items": [{"item": k, "count": v} for k, v in invd.items()]}
            ck = check_craft_materials(item, items)
            if ck and not ck.get("ok") and ck.get("missing"):
                missing = "、".join(str(m) for m in (ck.get("missing") or [])[:6])
                need_table = "，且需要工作台（先 place crafting_table）" if ck.get("need_table") else ""
                detail = f"（缺料参考：{missing}{need_table}）"
        except Exception as e:
            degrade("libs/qq_bot_runtime/mc_bot_brain.py:1304 BotAgent._t_craft", e, "降级：from mc_recipes import check_craft_materials")
        return f"fail:{r.get('msg', '合成失败')}{detail}"

    async def _t_place(self, a, s) -> str:
        block = str(a.get("block") or "").strip().lower()
        x = _safe_float(a.get("x"), None)
        z = _safe_float(a.get("z"), None)
        y = _safe_float(a.get("y"), None)
        if not block or x is None or z is None:
            return "fail:需要 block（方块英文名）+ x + z（目标地面坐标，y 按自己脚底那层自动取，也可显式给 y）"
        at = f"{round(x)},{round(z)}" if y is None else f"{round(x)},{round(y)},{round(z)}"
        r = _bridge_cmd("place", block=block, at=at,
                        face=str(a.get("face") or "up"), timeout=60.0)
        return f"ok:{r.get('msg', '')}" if r.get("ok") else f"fail:{r.get('msg', '放置失败')}"

    async def _t_chest(self, a, s) -> str:
        op = str(a.get("op") or "list")
        r = _bridge_cmd("container", op=op, item=str(a.get("item") or ""),
                        count=_safe_int(a.get("count"), 1), timeout=30.0)
        try:
            _bridge_cmd("container", op="close", timeout=8.0)  # 用完即关，防窗口悬挂
        except Exception as e:
            degrade("libs/qq_bot_runtime/mc_bot_brain.py:1327 BotAgent._t_chest", e, "降级：_bridge_cmd('container', op='close', timeout=8.0)")
        return f"ok:{r.get('msg', '')}" if r.get("ok") else f"fail:{r.get('msg', '')}"

    async def _t_furnace(self, a, s) -> str:
        op = str(a.get("op") or "peek")
        item = str(a.get("item") or "")
        if op == "smelt":
            if not item:
                return "fail:smelt 需要 item（原料如 iron_ore/cobblestone）"
            fuel = str(a.get("fuel") or "oak_log")
            r1 = _bridge_cmd("furnace", op="put_fuel", item=fuel, count=1, timeout=20.0)
            r2 = _bridge_cmd("furnace", op="put_input", item=item, count=1, timeout=20.0)
            msg = f"已投料 {item}（燃料：{r1.get('msg', '')}；投料：{r2.get('msg', '')}），过一会儿再 peek/take"
            ok = bool(r1.get("ok") or r2.get("ok"))
        elif op == "take":
            r2 = _bridge_cmd("furnace", op="take_output", timeout=20.0)
            msg = r2.get("msg", "")
            ok = bool(r2.get("ok") and "成品" in str(msg))
        else:  # peek
            r2 = _bridge_cmd("furnace", op="list", timeout=15.0)
            msg = r2.get("msg", "")
            ok = True
        try:
            _bridge_cmd("furnace", op="close", timeout=8.0)
        except Exception as e:
            degrade("libs/qq_bot_runtime/mc_bot_brain.py:1352 BotAgent._t_furnace", e, "降级：_bridge_cmd('furnace', op='close', timeout=8.0)")
        return f"ok:{msg}" if ok else f"fail:{msg}"

    async def _t_equip(self, a, s) -> str:
        item = str(a.get("item") or "").strip().lower()
        if not item:
            return "fail:缺 item"
        r = _bridge_cmd("equip", item=item, timeout=15.0)
        return f"ok:{r.get('msg', '')}" if r.get("ok") else f"fail:{r.get('msg', '')}"

    async def _t_up(self, a, s) -> str:
        r = _bridge_cmd("up", count=max(1, min(20, _safe_int(a.get("count"), 5))), timeout=120.0)
        # 失败消息也带进度（垫到第几格/什么原因），原样给大脑
        tag = "ok" if r.get("ok") else "fail"
        return f"{tag}:{r.get('msg', '垫高失败')}"

    async def _t_check_recipes(self, a, s) -> str:
        kw = str(a.get("item") or "").strip()
        try:
            if kw:
                from mc_recipes import get_recipe_advice
                adv = get_recipe_advice(kw, limit=6)
                return "ok:" + (adv[:500] if adv else f"暂无 {kw} 的配方资料")
            from mc_recipes import list_craftable
            invd = (s.get("inventory") or {})
            inv = {"items": [{"item": k, "count": v} for k, v in invd.items()]}
            items = list_craftable(inv) or []
            return "ok:当前材料能做的：" + ("、".join(items[:12]) if items else "暂无（先备基础材料）")
        except Exception as e:
            return f"fail:配方库不可用 {str(e)[:40]}"

    async def _t_remember_place(self, a, s) -> str:
        name = str(a.get("name") or "").strip()
        pos = s.get("pos") or {}
        dim = str(s.get("dimension") or self._last_dim or "overworld")
        if not name:
            return "fail:缺 name（给地点起名，如 家/矿洞入口）"
        try:
            from mc_explored import add_landmark, list_landmarks
            x, y, z = (_safe_float(pos.get("x"), 0), _safe_float(pos.get("y"), 0),
                       _safe_float(pos.get("z"), 0))
            # 先看本世界已记的同名地标（换世界/重复标记时提醒，避免“一堆家”）
            old_same = [lm for lm in list_landmarks(seed=VANILLA_WORLD_KEY, dimension=dim)
                        if str(lm.get("name", "")).strip() == name]
            r = add_landmark(name, x, y, z, seed=VANILLA_WORLD_KEY, dimension=dim)
            if r.get("ok"):
                note = ""
                if old_same:
                    where = "、".join(
                        f"({lm.get('x')},{lm.get('z')})" for lm in old_same[:3])
                    note = (f"（注意：此世界已记过 {len(old_same)} 个同名「{name}」：{where}"
                            "——重名容易混淆，确定旧的没用了就删掉它）")
                return f"ok:已记住地点 {name} @({x:.0f},{y:.0f},{z:.0f}){note}"
            return f"fail:{r.get('error', '记住失败')}"
        except Exception as e:
            return f"fail:记住地点失败 {str(e)[:40]}"

    # ---------------- 状态机模式（routine） ----------------
    async def _t_routine(self, a, s) -> str:
        name = str(a.get("name") or "").strip().lower()
        args = a.get("params") if isinstance(a.get("params"), dict) else {}
        if not name or name == "list":
            from mc_routines import routine_names
            return "ok:可用模式：" + routine_names()
        from mc_routines import Runner
        try:
            return await Runner(self).run(name, args)
        except Exception as e:
            return f"fail:模式启动失败 {str(e)[:80]}"

    # ---- 以下为 RoutineActor 协议实现（Runner 注入的 act 对象即 self） ----
    def pause_requested(self) -> bool:
        return self._routine_pause or not self._running

    async def stop_move(self) -> None:
        """停掉桥上的遗留寻路目标（模式跑之前先站稳）。"""
        try:
            _bridge_cmd("stop", timeout=8.0)
        except Exception as e:
            degrade("libs/qq_bot_runtime/mc_bot_brain.py:1431 BotAgent.stop_move", e, "降级：_bridge_cmd('stop', timeout=8.0)")

    async def cmd(self, action: str, timeout: float = 20.0, **kw) -> Dict:
        """模式内部的结构化桥命令（返回 dict，供 dig_at/terrain_grid 等读字段）。"""
        return await asyncio.to_thread(_bridge_cmd, action, timeout, **kw)

    async def step(self, tool: str, args: Dict, state: Dict) -> str:
        """模式内部一步 = 走大脑 _exec（含防重试拦截与遥测）。"""
        return await self._exec(str(tool).lower(), args, state)

    async def get_state(self) -> Dict:
        return await asyncio.to_thread(_bridge_get, "/state") or {}

    async def sleep(self, sec: float) -> None:
        await asyncio.sleep(sec)

    def log(self, name: str, status: str, state: str = "", msg: str = "") -> None:
        mon = _mon()
        if mon:
            mon.routine(name, status, state, msg)
        if status in ("start", "pause", "abort", "ok", "fail"):
            print(f"[BOT-ROUTINE] {name} {status}: {(msg or state)[:70]}")

    def on_pause(self, name: str, args: Dict, note: str) -> None:
        # 续跑由 LLM 重调同名模式实现（模式幂等，自动从断点继续）
        pass

    def is_owner_name(self, name: str) -> bool:
        """该游戏名是否已绑定到主人 QQ（escort 主人等用；identity 关闭时兜底旧语义）。"""
        try:
            if identity.enabled():
                return identity.is_owner_game_name(name)
        except Exception as e:
            degrade("libs/qq_bot_runtime/mc_bot_brain.py:1464 BotAgent.is_owner_name", e, "降级：if identity.enabled()")
        return _legacy_owner_name(name)

    def find_landmark(self, name: str, dimension: str = None):
        try:
            from mc_explored import list_landmarks
            dim = dimension or getattr(self, "_last_dim", "overworld") or "overworld"
            for lm in list_landmarks(seed=VANILLA_WORLD_KEY, dimension=dim):
                if str(name) in str(lm.get("name", "")):
                    return lm
        except Exception as e:
            degrade("libs/qq_bot_runtime/mc_bot_brain.py:1474 BotAgent.find_landmark", e, "降级：from mc_explored import list_landmarks")
        return None

    def unexplored_direction(self, x: float, z: float, dimension: str) -> Optional[str]:
        try:
            from mc_explored import find_unexplored_direction
            return find_unexplored_direction(x, z, radius=10, seed=VANILLA_WORLD_KEY,
                                             dimension=str(dimension or "overworld"))
        except Exception:
            return None

    def mark_explored(self, x: float, y: float, z: float, dimension: str) -> None:
        try:
            from mc_explored import mark_explored
            mark_explored(x, y, z, seed=VANILLA_WORLD_KEY, dimension=str(dimension or "overworld"))
        except Exception as e:
            degrade("libs/qq_bot_runtime/mc_bot_brain.py:1490 BotAgent.mark_explored", e, "降级：from mc_explored import mark_explored")

    def _routine_suggest(self, st: Dict) -> List[str]:
        """按背包/场景推荐匹配的模式（观察"可用模式"一栏）。"""
        out = []
        inv = st.get("inventory") or {}
        has_ore = any(k in inv for k in ("iron_ore", "copper_ore", "gold_ore", "raw_iron", "raw_copper", "raw_gold"))
        if has_ore:
            out.append("smelt_ores：把矿石烧成锭")
        has_pick = any("pickaxe" in k for k in inv)
        if not has_pick or int(inv.get("cobblestone", 0)) < 8:
            out.append("gear_up：升装备（木镐→石镐→熔炉）")
        if has_pick and not has_ore:
            out.append("find_ores：下挖到目标层+走廊挖掘找矿（缺矿就跑这个，别找裸露矿）")
        woods = sum(int(inv.get(k, 0)) for k in ("oak_log", "birch_log", "spruce_log", "dark_oak_log"))
        planks = sum(int(inv.get(k, 0)) for k in ("oak_planks", "birch_planks", "spruce_planks"))
        if woods + planks < 4:
            out.append("gather(material=wood)：补木头")
        if not has_pick and int(inv.get("cobblestone", 0)) == 0 and (woods + planks) >= 4:
            out.append("gear_up：先做镐再挖圆石")
        if len(inv) > 12 or sum(int(v) for v in inv.values()) > 48:
            out.append("store_all：杂物入箱清背包")
        try:
            from mc_explored import list_landmarks
            pos = st.get("pos") or {}
            for lm in list_landmarks(seed=VANILLA_WORLD_KEY,
                                     dimension=str(st.get("dimension") or "overworld")):
                if "家" in str(lm.get("name", "")):
                    d = abs(float(lm.get("x", 0)) - _safe_float(pos.get("x"), 0)) + \
                        abs(float(lm.get("z", 0)) - _safe_float(pos.get("z"), 0))
                    if d > 120:
                        out.append("go_home：离家太远，回去一趟")
                    break
        except Exception as e:
            degrade("libs/qq_bot_runtime/mc_bot_brain.py:1524 BotAgent._routine_suggest", e, "降级：from mc_explored import list_landmarks")
        return out[:3]

    # ---------------- 技能（与模组世界共享库，bot 环境标签） ----------------
    async def _t_save_skill(self, a, s) -> str:
        if mc_skills is None:
            return "fail:技能库模块不可用"
        name = str(a.get("name") or "").strip()
        desc = str(a.get("description") or "").strip()
        steps = a.get("steps") or []
        keywords = a.get("keywords") or []
        if not name or not steps:
            return "fail:需要 name、steps（本桥工具名序列，如 [{\"tool\":\"dig_target\",\"target\":\"oak_log\"}]）"
        r = mc_skills.save_skill(name, desc, steps, keywords, env=mc_skills.ENV_BOT)
        if not r.get("ok"):
            return f"fail:{r.get('error', '保存失败')}"        # 沉淀通用技巧：文字描述不绑工具名，模组世界版大脑每轮注入"相关经验"时能看到
        tip_count = 0
        try:
            from mc_tips import add_tip, list_tips
            if not any(str(t.get("content", "")).startswith(f"{name}：") for t in list_tips()):
                tip = add_tip(f"{name}：{(desc or name)[:80]}（原版验证过的做法，模组世界可参考）", source="learned")
                tip_count = 1 if tip.get("ok") else 0
        except Exception as e:
            print(f"[BOT-BRAIN] 技巧沉淀失败: {e}")
        note = f"；{r['note']}" if r.get("note") else ""
        return f"ok:技能已保存({r.get('name')}，{r.get('steps')}步)并沉淀{tip_count}条技巧{note}"

    async def _t_run_skill(self, a, s) -> str:
        if not self._running:
            return "fail:智能体已停止"
        name = str(a.get("name") or "").strip()
        skill = mc_skills.get_skill(name) if mc_skills is not None else None
        if not skill:
            return f"fail:技能库中没有 {name}"
        if mc_skills.skill_env(skill) != mc_skills.ENV_BOT:
            return (f"fail:技能[{skill.get('name')}]的步骤是模组版的，桥身体执行不了——"
                    f"参考它描述的做法（{str(skill.get('description', ''))[:60]}）用当前工具自己做")
        steps = skill.get("steps") or []
        if not steps:
            return f"fail:技能[{skill.get('name')}]没有可执行步骤"
        results = []
        success = True
        for step in steps:
            if not self._running:
                results.append("中断:已停止")
                success = False
                break
            tool = str(step.get("tool") or "").strip().lower()
            step_args = {k: v for k, v in step.items() if k != "tool"}
            result = await self._exec(tool, step_args, s)
            results.append(f"{tool}:{result[:30]}")
            if result.startswith("fail"):
                success = False
                break
        try:
            mc_skills.record_use(skill.get("name", name), success)
        except Exception as e:
            degrade("libs/qq_bot_runtime/mc_bot_brain.py:1581 BotAgent._t_run_skill", e, "降级：mc_skills.record_use(skill.get('name', name), succ")
        tag = "ok" if success else "fail"
        return f"{tag}:技能[{skill.get('name')}] " + ";".join(results[:6])

    # ---------------- 会话管理 ----------------
    def _trim(self):
        if len(self._session) <= 40:
            return
        count = 0
        cut = 1
        for i in range(len(self._session) - 1, 0, -1):
            count += 1
            if count > 40 and self._session[i].get("role") == "user":
                cut = i
                break
        self._session = [self._session[0]] + self._session[cut:]


BOT_TOOLS = [
    {"type": "function", "function": {"name": "end_turn",
     "description": "结束本轮行动，等待下一次观察。",
     "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {"name": "set_goal",
     "description": "声明当前目标与下一步",
     "parameters": {"type": "object",
                    "properties": {"current": {"type": "string"}, "next": {"type": "string"}},
                    "required": ["current"]}}},
    {"type": "function", "function": {"name": "goto",
     "description": "自动寻路走到指定世界坐标（X/Z）。长距离移动用它。",
     "parameters": {"type": "object",
                    "properties": {"x": {"type": "number"}, "z": {"type": "number"}, "reason": {"type": "string"}},
                    "required": ["x", "z"]}}},
    {"type": "function", "function": {"name": "explore_direction",
     "description": "朝某个世界方向探索约 24 格",
     "parameters": {"type": "object",
                    "properties": {"direction": {"type": "string", "enum": ["north", "south", "east", "west"]}},
                    "required": ["direction"]}}},
    {"type": "function", "function": {"name": "dig_target",
     "description": "挖最近的指定方块（方块英文名，如 oak_log/spruce_log/iron_ore/stone）。先 scan 看附近有什么。",
     "parameters": {"type": "object",
                    "properties": {"target": {"type": "string", "description": "方块英文名"}},
                    "required": ["target"]}}},
    {"type": "function", "function": {"name": "dig_stairs",
     "description": "阶梯式下挖（单步原语，一般直接用 routine find_ores 更省心）：朝 direction 斜着往下挖台阶下降，每步先破面前墙再下踏一格；自动避开脚下/岩浆/深空腔，每3步补火把，结束报告挖开什么和露出的矿坐标。",
     "parameters": {"type": "object",
                    "properties": {"direction": {"type": "string", "enum": ["north", "south", "east", "west"]},
                                   "steps": {"type": "integer", "description": "下挖台阶数 1~8，默认4"}},
                    "required": ["direction"]}}},
    {"type": "function", "function": {"name": "attack",
     "description": "攻击最近的敌对生物(target=hostile)，或猎杀指定动物(target=动物名如 cow)。",
     "parameters": {"type": "object",
                    "properties": {"target": {"type": "string", "description": "hostile 或动物英文名"}},
                    "required": ["target"]}}},
    {"type": "function", "function": {"name": "eat",
     "description": "自动从背包找食物吃。血量或饥饿低时优先调用。",
     "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {"name": "scan",
     "description": "扫描 28 格内资源：木头 / 露头矿（有面暴露，dig_target 直接挖）/ 埋着的矿（被方块挡着，用 dig_stairs 阶梯下挖露头）/ 动物。行动前不确定就扫。",
     "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {"name": "look",
     "description": "转向指定视角（yaw: 0=南 90=西 180=北 -90=东；pitch 正=低头 负=抬头）",
     "parameters": {"type": "object",
                    "properties": {"yaw": {"type": "number"}, "pitch": {"type": "number"}},
                    "required": ["yaw"]}}},
    {"type": "function", "function": {"name": "say",
     "description": "在游戏里说话（真人玩家在等你回复时，用这个跟他对话）",
     "parameters": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}}},
    {"type": "function", "function": {"name": "save_skill",
     "description": "把刚验证有效的做法存成技能（steps 写本桥工具名和参数，如 dig_target/attack/eat/goto/scan），会同时自动沉淀一条通用技巧给模组世界版参考",
     "parameters": {"type": "object",
                    "properties": {"name": {"type": "string", "description": "技能名，如 挖橡木备料（可含英文名帮助检索）"},
                                   "description": {"type": "string", "description": "什么场景下用"},
                                   "steps": {"type": "array", "items": {"type": "object"},
                                             "description": "如 [{\"tool\":\"dig_target\",\"target\":\"oak_log\"},{\"tool\":\"end_turn\"}]"},
                                   "keywords": {"type": "array", "items": {"type": "string"},
                                                "description": "强烈建议给 2~6 个：场景里的方块/生物/行为的词和英文名（如 oak_log/cow/木头），否则以后场景来了检索不到"}},
                    "required": ["name", "description", "steps"]}}},
    {"type": "function", "function": {"name": "run_skill",
     "description": "重放技能库里保存的动作序列（只对 bot 环境技能有效；模组版技能会提示你参考其做法）",
     "parameters": {"type": "object",
                    "properties": {"name": {"type": "string", "description": "技能名"}},
                    "required": ["name"]}}},
    {"type": "function", "function": {"name": "craft",
     "description": "真实合成物品（自动找附近工作台，需工作台配方会提示）。做之前先用 check_recipes 看缺什么",
     "parameters": {"type": "object",
                    "properties": {"item": {"type": "string", "description": "物品英文名如 wooden_pickaxe/oak_planks/chest/furnace/crafting_table"},
                                   "count": {"type": "integer", "description": "要做的份数，默认 1"}},
                    "required": ["item"]}}},
    {"type": "function", "function": {"name": "place",
     "description": "把背包里的方块放到指定格（x,z 整数坐标；y 缺省自动取自己脚底那层，斜坡/岩石地形放不上时可显式给 y=自己y或+1）。放工作台/箱子/熔炉前用它",
     "parameters": {"type": "object",
                    "properties": {"block": {"type": "string", "description": "方块英文名如 crafting_table/chest/furnace/torch"},
                                   "x": {"type": "number"}, "z": {"type": "number"},
                                   "y": {"type": "number", "description": "可选，目标格 y"},
                                   "face": {"type": "string", "description": "放哪面，默认 up（放地面）"}},
                    "required": ["block", "x", "z"]}}},
    {"type": "function", "function": {"name": "chest",
     "description": "操作附近的箱子/桶：op=store 存入物品 / take 取出 / list 查看。存物资防止背包满",
     "parameters": {"type": "object",
                    "properties": {"op": {"type": "string", "enum": ["store", "take", "list"]},
                                   "item": {"type": "string", "description": "物品英文名（store/take 用）"},
                                   "count": {"type": "integer", "description": "数量，默认 1"}},
                    "required": ["op"]}}},
    {"type": "function", "function": {"name": "furnace",
     "description": "熔炉：op=smelt 投料烧炼（item 原料如 iron_ore/cobblestone，fuel 燃料默认 oak_log）/ peek 看进度 / take 取成品",
     "parameters": {"type": "object",
                    "properties": {"op": {"type": "string", "enum": ["smelt", "peek", "take"]},
                                   "item": {"type": "string", "description": "原料英文名"},
                                   "fuel": {"type": "string", "description": "燃料英文名，默认 oak_log"}},
                    "required": ["op"]}}},
    {"type": "function", "function": {"name": "equip",
     "description": "把手持换成背包里的工具/武器（挖矿必须手持镐才有掉落，战斗前换剑）",
     "parameters": {"type": "object",
                    "properties": {"item": {"type": "string", "description": "物品英文名如 stone_pickaxe/wooden_sword"}},
                    "required": ["item"]}}},
    {"type": "function", "function": {"name": "up",
     "description": "跳跃垫高爬升（背包需有 dirt/cobblestone），掉坑/被困死路脱困用。身体会跳到最高点才往脚下放并等服务端确认，失败自动重试一次；连续失败=头顶有遮挡，先挖开头顶再试",
     "parameters": {"type": "object",
                    "properties": {"count": {"type": "integer", "description": "垫高格数 1~20，默认5"}},
                    "required": []}}},
    {"type": "function", "function": {"name": "check_recipes",
     "description": "查配方资料/当前材料能做什么（item 给具体物品名查怎么做，不给则列可做的）",
     "parameters": {"type": "object",
                    "properties": {"item": {"type": "string", "description": "可选，物品英文名"}},
                    "required": []}}},
    {"type": "function", "function": {"name": "remember_place",
     "description": "把当前位置记成有名字的地标（同伴说'把这里记住'时用）",
     "parameters": {"type": "object",
                    "properties": {"name": {"type": "string", "description": "地标名如 家/矿洞入口"}},
                    "required": ["name"]}}},
    {"type": "function", "function": {"name": "routine",
     "description": "跑完整流程的状态机模式：gear_up 升装备全套 / smelt_ores 烧光背包矿石 / gather(material=wood或stone, n) 采集 / find_ores(ore=目标矿默认iron_ore, n=数量, legs=预算) 找矿主力——自动先备足8支火把再下矿（缺煤会明确告诉你去哪补），然后下挖到该矿高产层（铁16/铜48/煤96/金-16/钻石-59）再蛇形走廊挖掘，露头矿自动收，绝不依赖裸露矿 / store_all 杂物入箱 / go_home(name=家) / escort_owner 跟随同伴 / explore_new_area 探索新方向 / make_flat 就地整平造平地 / progress_survival 生存阶段一键推进 / list 查看。运行中被打断（fail:paused）就再调同名自动续跑",
     "parameters": {"type": "object",
                    "properties": {"name": {"type": "string", "description": "模式名：gear_up/smelt_ores/gather/store_all/go_home/escort_owner/explore_new_area/list"},
                                   "params": {"type": "object",
                                              "description": "模式参数，如 gather 的 {material:'wood', n:16}、go_home 的 {name:'家'}"}},
                    "required": ["name"]}}},
]
