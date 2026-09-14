# -*- coding: utf-8 -*-
"""跨平台人物身份绑定（QQ ↔ MC 游戏名 ↔ 未来更多平台）。

背景：记忆/档案/笔记按 user_id 键隔离，QQ 号与 MC 游戏名是两套互不知晓的
命名空间（chat_history/<qq号>.jsonl 与 chat_history/<游戏名>.jsonl 并存）。
本模块提供「身份注册表」：双向确认后把一个 QQ 号与一个或多个 MC 游戏名
绑定为同一个人。canonical（记忆合并键）= 该 QQ 号（QQ 侧旧数据零迁移），
绑定生效后：
  - 游戏侧说话者若已绑定 → 按 QQ 号读写全文历史/档案/笔记（与 QQ 侧同源）
  - 两侧对话互相注入对方的身份提示（identity hints）

确认模型（防冒认，防止把某人的 QQ 档案错接给陌生人）：
  QQ 侧发起 /绑定 mc:X   → pending_side="mc"，等游戏里 X 本人回复「确认绑定」
  游戏里 X 声明「QQ号 Y 是我」→ pending_side="qq"，等 QQ 侧 Y 回复「同意绑定」
  主人可用 /绑定批准 直接激活（信任场景）；主人预置绑定开机即 active。

存储：identity_bindings.json（项目根，与 user_profiles.json 同约定）
结构：
{
  "version": 1,
  "persons": [
    {
      "id": "p-...",
      "qq": "2190720017",       # 本人 QQ（始终存在；即 canonical 记忆键）
      "mc": [                    # 该 QQ 绑定过的游戏名（小写比较）
        {"name": "insomnic", "ok": true, "pending_side": "", "asked_ts": 0, "time": "..."}
      ],
      "created": 0, "updated": 0
    }
  ]
}
mc[i].ok = 双向确认完成（记忆打通生效）；pending_side =
  "mc" = 等游戏里本人确认 / "qq" = 等 QQ 侧本人确认 / "" = 已生效。
asked_ts = 游戏侧最近一次向该玩家询问确认的时间戳（防每句话都问）。

并发：QQ 核心进程与 MC bot 进程都会读写本文件（低频），load-modify-save
全程在 file_lock 保护下进行。
"""
import json
import os
import random
import re
import time

import config
import file_lock

_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "identity_bindings.json")
_VERSION = 1

# 游戏侧确认/否认的说法（与 QQ 侧命令提示保持同文案）
MC_CONFIRM_WORDS = {"确认绑定", "同意绑定", "绑定确认", "确认", "同意", "是", "是的"}
MC_DENY_WORDS = {"不是", "不是我", "拒绝", "不同意", "不是的"}
QQ_CONFIRM_WORDS = {"同意绑定", "确认绑定", "绑定同意", "同意", "确认", "是", "是的"}
QQ_DENY_WORDS = {"不是", "不是我", "拒绝", "不同意", "不是的"}

# 游戏里声称「QQ号 xxx 是我」的识别（内容里还须有 绑定/是我/我的 等身份动词，
# 避免误吞日常数字）。兼容：绑定QQ 123 / QQ123456是我 / 我的QQ是123456
_QQ_CLAIM_RE = re.compile(r"(?:绑定|我的)?\s*(?:QQ|qq|QQ号|qq号)\s*[:：是]?\s*(\d{5,15})")

_ASK_INTERVAL = 300.0  # 游戏侧同一个人询问确认的最小间隔（秒）


def enabled() -> bool:
    return bool(getattr(config, "ENABLE_IDENTITY_LINK", True))


def owner_qq() -> str:
    """主人 QQ 号（身份语义上的主人 = 预置绑定里的 QQ）。"""
    return str(getattr(config, "PROACTIVE_PRIVATE_USER_ID", "") or "").strip()


def _owner_mc_names() -> list:
    names = getattr(config, "IDENTITY_OWNER_MC_NAMES", []) or []
    return [str(n).strip().lower() for n in names if str(n).strip()]


# ---------------- 存取 ----------------
_seed_attempted = False  # 防止 load_bindings → seed_owner → load_bindings 循环


def load_bindings() -> dict:
    global _seed_attempted
    # 首次运行（文件不存在）时自动预置主人绑定（幂等）
    if enabled() and not _seed_attempted and not os.path.exists(_FILE):
        _seed_attempted = True
        try:
            seed_owner()
        except Exception as e:
            print(f"[IDENTITY] 预置绑定失败: {e}")
    if os.path.exists(_FILE):
        try:
            with open(_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("persons"), list):
                return data
        except (json.JSONDecodeError, OSError):
            pass
    return {"version": _VERSION, "persons": []}


def _save(data: dict):
    with open(_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _norm_mc(name) -> str:
    return str(name or "").strip().lower()


def _norm_qq(qq) -> str:
    return str(qq or "").strip()


def _new_person_id(data: dict) -> str:
    for _ in range(20):
        pid = f"p-{int(time.time() * 1000)}-{random.randint(0, 9999)}"
        if not any(p.get("id") == pid for p in data["persons"]):
            return pid
    return f"p-{time.time()}-{random.randint(0, 99999)}"


def _now_str() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def seed_owner() -> bool:
    """预置主人绑定（开机首次调用时自动生成 active），替代旧硬编码。

    Returns:
        True 表示本次执行了预置（文件此前不存在/为空）。
    """
    if not enabled():
        return False
    oqq = owner_qq()
    names = _owner_mc_names()
    if not oqq or not names:
        return False
    with file_lock.file_lock(_FILE):
        data = load_bindings()
        if data["persons"]:
            return False
        person = {
            "id": _new_person_id(data),
            "qq": oqq,
            "mc": [{"name": n, "ok": True, "pending_side": "", "asked_ts": 0,
                    "time": _now_str()} for n in names],
            "created": time.time(),
            "updated": time.time(),
        }
        data["persons"].append(person)
        _save(data)
    # 预置激活：把主人游戏名的历史并入 QQ 档案文件（幂等）
    for n in names:
        _merge_history_safely(n, oqq)
    print(f"[IDENTITY] 已预置主人绑定: QQ {oqq} ↔ {names}")
    return True


def _merge_history_safely(mc_name: str, qq: str):
    """把某个游戏名的旧全文历史并入 canonical（QQ 号），失败不阻塞。"""
    try:
        from long_term_memory import merge_alias_history
        merge_alias_history(mc_name, qq)
    except Exception as e:
        print(f"[IDENTITY] 历史合并失败({mc_name}→{qq}): {e}")


def _find_person_by_qq(data: dict, qq: str):
    qq = _norm_qq(qq)
    for p in data["persons"]:
        if _norm_qq(p.get("qq")) == qq:
            return p
    return None


def _find_person_by_mc(data: dict, mc_name: str):
    name = _norm_mc(mc_name)
    for p in data["persons"]:
        if any(_norm_mc(a.get("name")) == name for a in p.get("mc", [])):
            return p
    return None


def _mc_alias(person: dict, mc_name: str):
    name = _norm_mc(mc_name)
    for a in person.get("mc", []):
        if _norm_mc(a.get("name")) == name:
            return a
    return None


def _touch(data: dict, person: dict):
    person["updated"] = time.time()
    _save(data)


# ---------------- 解析 ----------------
def canonical_for(platform: str, raw_id: str) -> str:
    """把某平台身份解析为记忆键：已生效绑定的人 → 该人 QQ 号；否则原样返回。"""
    raw = str(raw_id or "")
    if not enabled() or not raw:
        return raw
    plat = str(platform or "").strip().lower()
    try:
        if plat == "mc":
            data = load_bindings()
            person = _find_person_by_mc(data, raw)
            if person:
                a = _mc_alias(person, raw)
                if a and a.get("ok"):
                    return _norm_qq(person.get("qq")) or raw
        # 其他平台（qq/console/...）尚无映射语义；有需要时在此扩展
    except Exception as e:
        print(f"[IDENTITY] canonical 解析失败: {e}")
    return raw


def qq_of_mc(mc_name: str):
    """该游戏名已生效绑定到的 QQ 号；未绑定/未生效返回 None。"""
    if not enabled():
        return None
    try:
        data = load_bindings()
        person = _find_person_by_mc(data, mc_name)
        if person:
            a = _mc_alias(person, mc_name)
            if a and a.get("ok"):
                return _norm_qq(person.get("qq")) or None
    except Exception as e:
        print(f"[IDENTITY] qq_of_mc 查询失败: {e}")
    return None


def is_owner_game_name(mc_name: str) -> bool:
    """该游戏名是否已绑定到主人 QQ（原 MC_OWNER_GAME_NAMES 语义）。"""
    oqq = owner_qq()
    if not oqq:
        return False
    qq = qq_of_mc(mc_name)
    return bool(qq) and qq == oqq


def is_owner_qq(qq: str) -> bool:
    oqq = owner_qq()
    return bool(oqq) and _norm_qq(qq) == oqq


def ok_mc_names_of_qq(qq: str) -> list:
    """某 QQ 已生效绑定的游戏名列表。"""
    if not enabled():
        return []
    try:
        data = load_bindings()
        person = _find_person_by_qq(data, qq)
        if not person:
            return []
        return [a["name"] for a in person.get("mc", []) if a.get("ok")]
    except Exception:
        return []


def has_bound_mc(qq: str) -> bool:
    """该 QQ 是否有任何生效的游戏名绑定（QQ 侧会话提示用）。"""
    return bool(ok_mc_names_of_qq(qq))


def pending_qq_names(qq: str) -> list:
    """该 QQ 待其本人同意的游戏名（游戏侧声明「QQ号是我」，pending_side=qq）。"""
    if not enabled():
        return []
    qq = _norm_qq(qq)
    try:
        data = load_bindings()
        person = _find_person_by_qq(data, qq)
        if not person:
            return []
        return [a["name"] for a in person.get("mc", [])
                if not a.get("ok") and a.get("pending_side") == "qq"]
    except Exception:
        return []


# ---------------- 绑定操作（返回 (ok, 给用户/机器人的文案)） ----------------
def request_bind(qq: str, mc_name: str):
    """QQ 侧发起：把自己的 QQ 与游戏名 X 绑定。等游戏里 X 本人确认。"""
    if not enabled():
        return False, "身份绑定功能未开启（config.ENABLE_IDENTITY_LINK）"
    qq = _norm_qq(qq)
    name = _norm_mc(mc_name)
    if not qq or not name:
        return False, "格式：/绑定 mc:游戏名"
    if not re.fullmatch(r"[a-zA-Z0-9_]{2,16}", name):
        return False, "游戏名格式不太对哦，一般是字母数字下划线，2~16 位。"
    with file_lock.file_lock(_FILE):
        data = load_bindings()
        other = _find_person_by_mc(data, name)
        if other and _norm_qq(other.get("qq")) != qq:
            return False, f"这个游戏名已经和另一个 QQ 绑定或待绑定了，先请对方解除再说~"
        person = _find_person_by_qq(data, qq)
        if person is None:
            person = {"id": _new_person_id(data), "qq": qq, "mc": [],
                      "created": time.time(), "updated": time.time()}
            data["persons"].append(person)
        alias = _mc_alias(person, name)
        if alias is None:
            alias = {"name": name, "ok": False, "pending_side": "mc",
                     "asked_ts": 0, "time": _now_str()}
            person["mc"].append(alias)
            _touch(data, person)
            return True, f"好，已向游戏里的「{mc_name}」发起绑定请求。等 TA 在游戏里对机器人回复「确认绑定」后生效；届时两边记忆就打通啦。取消用 /解除绑定 mc:{mc_name}"
        if alias.get("ok"):
            return False, f"这个游戏名本来就已经绑定了~（/绑定列表 可看）"
        if alias.get("pending_side") == "mc":
            return False, f"之前已经发起过与「{mc_name}」的绑定啦，还在等 TA 在游戏里确认（/绑定列表 可看进度）"
        if alias.get("pending_side") == "qq":
            # 对方正在游戏里声称是你；QQ 侧再发一次绑定 = 直接同意
            alias["ok"] = True
            alias["pending_side"] = ""
            _touch(data, person)
            _merge_history_safely(alias["name"], qq)
            return True, f"游戏里的「{mc_name}」正好声称这个 QQ 号是 TA，既然你也确认，绑定就生效啦~两边记忆已打通。"
    return False, "绑定失败，稍后再试试？"


def claim_by_game(mc_name: str, qq: str):
    """游戏侧发起：玩家 X 声称「QQ号 qq 是我」。等 QQ 侧本人同意。"""
    if not enabled():
        return False, "身份绑定功能未开启"
    name = _norm_mc(mc_name)
    qq = _norm_qq(qq)
    with file_lock.file_lock(_FILE):
        data = load_bindings()
        holder = _find_person_by_mc(data, name)
        if holder and _norm_qq(holder.get("qq")) != qq:
            return False, f"这个游戏名已经和另一个 QQ（{holder.get('qq')}）绑定或待绑定啦,让 TA 解除之后再说吧。"
        person = _find_person_by_qq(data, qq)
        if person is None:
            person = {"id": _new_person_id(data), "qq": qq, "mc": [],
                      "created": time.time(), "updated": time.time()}
            data["persons"].append(person)
        alias = _mc_alias(person, name)
        if alias is None:
            alias = {"name": name, "ok": False, "pending_side": "qq",
                     "asked_ts": 0, "time": _now_str()}
            person["mc"].append(alias)
            _touch(data, person)
            return True, f"好，已向 QQ 号 {qq} 发起绑定请求。等 TA 在 QQ 上回复「同意绑定」就生效,到时候我们互相都认得你啦。"
        if alias.get("ok"):
            return False, "这个游戏名之前就已经绑定好啦。"
        if alias.get("pending_side") == "qq":
            return False, f"之前就向 QQ 号 {qq} 发起过绑定啦,还在等 TA 同意。"
        if alias.get("pending_side") == "mc":
            # QQ 侧正在等游戏里确认；玩家亲口确认 = 双向完成
            alias["ok"] = True
            alias["pending_side"] = ""
            _touch(data, person)
            _merge_history_safely(alias["name"], person.get("qq"))
            return True, "好，绑定生效啦！你的 QQ 档案已经和这边打通,以后我两边都认识你。"
    return False, "绑定失败,稍后再试试？"


def confirm_in_game(mc_name: str):
    """游戏侧确认（pending_side=mc 时玩家回「确认绑定」）。"""
    if not enabled():
        return False, "身份绑定功能未开启"
    name = _norm_mc(mc_name)
    with file_lock.file_lock(_FILE):
        data = load_bindings()
        person = _find_person_by_mc(data, name)
        if not person:
            return False, "没有找到关于你的绑定请求。想发起绑定的话,在 QQ 上跟机器人说 /绑定 mc:你的游戏名 试试？"
        alias = _mc_alias(person, name)
        if not alias:
            return False, "没有找到关于你的绑定请求。"
        if alias.get("ok"):
            return True, "你早就绑定好啦,两边记忆是打通的。"
        if alias.get("pending_side") == "qq":
            return False, f"是你先声称自己是 QQ 号 {person.get('qq')} 的,还在等 TA 在 QQ 上回复「同意绑定」呢。"
        alias["ok"] = True
        alias["pending_side"] = ""
        _touch(data, person)
        _merge_history_safely(alias["name"], person.get("qq"))
        return True, "确认收到,绑定生效！QQ 那边关于你的记忆（怎么称呼你、你的喜好等）已经和这边打通啦。"


def deny_in_game(mc_name: str):
    """游戏侧否认（玩家回「不是我」）。"""
    if not enabled():
        return False, "身份绑定功能未开启"
    name = _norm_mc(mc_name)
    with file_lock.file_lock(_FILE):
        data = load_bindings()
        person = _find_person_by_mc(data, name)
        if person:
            alias = _mc_alias(person, name)
            if alias and not alias.get("ok") and alias.get("pending_side") == "mc":
                person["mc"] = [a for a in person["mc"] if a is not alias]
                _prune_person(data, person)
                _touch(data, person)
                return True, "明白啦,我已经回绝了那个绑定请求,你的记忆不会被别人接走。"
        return False, "没有待你确认的绑定请求呀。"


def confirm_on_qq(qq: str):
    """QQ 侧同意（pending_side=qq 时本人回「同意绑定」）→ 全部生效。"""
    if not enabled():
        return False, "身份绑定功能未开启"
    qq = _norm_qq(qq)
    with file_lock.file_lock(_FILE):
        data = load_bindings()
        person = _find_person_by_qq(data, qq)
        if not person:
            return False, "没有找到要你确认的绑定请求。"
        targets = [a for a in person.get("mc", [])
                   if not a.get("ok") and a.get("pending_side") == "qq"]
        if not targets:
            return False, "没有找到要你确认的绑定请求。"
        names = [a["name"] for a in targets]
        for a in targets:
            a["ok"] = True
            a["pending_side"] = ""
        _touch(data, person)
    for n in names:
        _merge_history_safely(n, qq)
    return True, f"好,已同意游戏里的「{'、'.join(names)}」与你的 QQ 绑定,两边记忆打通啦。之后在游戏里 TA 说话时我会认得 TA。"


def deny_on_qq(qq: str, mc_name: str = None):
    """QQ 侧否认（回复「不是我」）。指定 mc_name 则只回绝那一个。"""
    if not enabled():
        return False, "身份绑定功能未开启"
    qq = _norm_qq(qq)
    with file_lock.file_lock(_FILE):
        data = load_bindings()
        person = _find_person_by_qq(data, qq)
        if not person:
            return False, "没有找到要你确认的绑定请求。"
        if mc_name:
            alias = _mc_alias(person, mc_name)
            if alias and not alias.get("ok") and alias.get("pending_side") == "qq":
                person["mc"] = [a for a in person["mc"] if a is not alias]
                _prune_person(data, person)
                _touch(data, person)
                return True, "明白啦,已经回绝,不会把那个游戏名当成你。"
            return False, "没有找到对应的待确认绑定。"
        targets = [a for a in person.get("mc", [])
                   if not a.get("ok") and a.get("pending_side") == "qq"]
        if not targets:
            return False, "没有找到要你确认的绑定请求。"
        person["mc"] = [a for a in person["mc"] if a not in targets]
        _prune_person(data, person)
        _touch(data, person)
        return True, "明白啦,已经全部回绝。"


def force_activate(qq: str, mc_name: str):
    """主人直接批准某条待确认绑定（信任场景,跳过另一侧确认）。"""
    if not enabled():
        return False, "身份绑定功能未开启"
    if not is_owner_qq(qq):
        return False, "只有主人能直接批准绑定哦。"
    name = _norm_mc(mc_name)
    with file_lock.file_lock(_FILE):
        data = load_bindings()
        person = _find_person_by_mc(data, name)
        if not person:
            return False, f"还没有「{mc_name}」的绑定请求。先让 TA 在 QQ 发起 /绑定 或在游戏里声明,再来批准。"
        alias = _mc_alias(person, name)
        if not alias:
            return False, f"没有找到「{mc_name}」的绑定请求。"
        if alias.get("ok"):
            return True, "这条绑定早就生效啦。"
        alias["ok"] = True
        alias["pending_side"] = ""
        target_qq = _norm_qq(person.get("qq"))
        _touch(data, person)
    _merge_history_safely(alias["name"], target_qq)
    return True, f"好,已直接批准：游戏名「{mc_name}」→ QQ {target_qq} 绑定生效。"


def unbind_mc(actor_qq: str, mc_name: str, is_owner: bool = False):
    """解除某游戏名与 QQ 的绑定（本人或主人）。数据不拆分,只是不再融合新内容。"""
    if not enabled():
        return False, "身份绑定功能未开启"
    actor = _norm_qq(actor_qq)
    name = _norm_mc(mc_name)
    with file_lock.file_lock(_FILE):
        data = load_bindings()
        person = _find_person_by_mc(data, name)
        if not person or not _mc_alias(person, name):
            return False, f"「{mc_name}」没有绑定,不用解除。"
        if not is_owner and not is_owner_qq(actor) and _norm_qq(person.get("qq")) != actor:
            return False, "只能解除你自己 QQ 的绑定哦。"
        alias = _mc_alias(person, name)
        person["mc"] = [a for a in person["mc"] if a is not alias]
        was_ok = bool(alias.get("ok"))
        _prune_person(data, person)
        _touch(data, person)
    if was_ok:
        return True, f"好,已解除「{mc_name}」的绑定。以后 TA 在游戏里说的话会重新记到 TA 自己的名下;以前合并的历史不会拆开。"
    return True, f"好,已取消「{mc_name}」的绑定请求。"


def _prune_person(data: dict, person: dict):
    """mc 别名清空后该人记录没有存在意义,删除。"""
    if not person.get("mc"):
        data["persons"] = [p for p in data["persons"] if p.get("id") != person.get("id")]


# ---------------- 询问去重（游戏侧问确认） ----------------
def mc_ask_due(mc_name: str) -> bool:
    """该玩家是否到了再次询问绑定确认的时间（未问过 / 距上次超 5 分钟）。"""
    if not enabled():
        return False
    name = _norm_mc(mc_name)
    try:
        with file_lock.file_lock(_FILE):
            data = load_bindings()
            person = _find_person_by_mc(data, name)
            if not person:
                return False
            alias = _mc_alias(person, name)
            if not alias or alias.get("ok") or alias.get("pending_side") != "mc":
                return False
            return (time.time() - float(alias.get("asked_ts") or 0)) > _ASK_INTERVAL
    except Exception:
        return False


def mark_mc_asked(mc_name: str):
    """记录游戏侧刚问过该玩家确认。"""
    if not enabled():
        return
    name = _norm_mc(mc_name)
    try:
        with file_lock.file_lock(_FILE):
            data = load_bindings()
            person = _find_person_by_mc(data, name)
            if not person:
                return
            alias = _mc_alias(person, name)
            if alias and not alias.get("ok"):
                alias["asked_ts"] = time.time()
                _touch(data, person)
    except Exception:
        pass


def pending_mc_qq_of(mc_name: str):
    """该玩家名下待游戏侧确认的绑定对应的 QQ 号（用于游戏里询问）。"""
    if not enabled():
        return None
    name = _norm_mc(mc_name)
    try:
        data = load_bindings()
        person = _find_person_by_mc(data, name)
        if not person:
            return None
        alias = _mc_alias(person, name)
        if alias and not alias.get("ok") and alias.get("pending_side") == "mc":
            return _norm_qq(person.get("qq"))
    except Exception:
        pass
    return None


# ---------------- 注入提示 ----------------
def qq_hints(qq: str) -> list:
    """QQ 侧会话注入提示：该 QQ 在游戏里的身份 / 待其同意的绑定请求。"""
    if not enabled():
        return []
    qq = _norm_qq(qq)
    if not qq:
        return []
    try:
        data = load_bindings()
        person = _find_person_by_qq(data, qq)
        if not person:
            return []
        hints = []
        ok_names = [a["name"] for a in person.get("mc", []) if a.get("ok")]
        if ok_names:
            hints.append(
                f"【身份提示】这位用户在 MC 游戏世界里叫「{'、'.join(ok_names)}」,"
                f"两边是同一个人（已双向确认绑定）,QQ 上和游戏里学到的记忆已打通,"
                f"可以直接当同一个人对待。")
        pending = [a for a in person.get("mc", [])
                   if not a.get("ok") and a.get("pending_side") == "qq"]
        if pending:
            hints.append(
                f"【待确认绑定】游戏里的玩家「{'、'.join(a['name'] for a in pending)}」说这个 QQ 号是 TA,"
                f"想跟你互通记忆。如果你确认 TA 就是你本人,回复「同意绑定」;如果不是,回复「不是」。")
        return hints
    except Exception as e:
        print(f"[IDENTITY] QQ 提示注入失败: {e}")
        return []


def mc_identity_hint(mc_name: str) -> str:
    """游戏侧会话注入提示（仅已生效绑定的玩家,带私密性约束）。"""
    if not enabled():
        return ""
    name = _norm_mc(mc_name)
    try:
        data = load_bindings()
        person = _find_person_by_mc(data, name)
        if not person:
            return ""
        alias = _mc_alias(person, name)
        if not alias or not alias.get("ok"):
            return ""
        qq = _norm_qq(person.get("qq"))
        others = [a["name"] for a in person.get("mc", [])
                  if a.get("ok") and a.get("name") != name]
        other_txt = f"（TA 的游戏名还有：{'、'.join(others)}）" if others else ""
        return (f"（身份提示：正在游戏里说话的是 {name} —— 他就是 QQ 号 {qq} 的那个人,"
                f"绑定已双向确认{other_txt}。下方档案、笔记、QQ 近况都是关于 TA 的真实信息,"
                f"直接用;但游戏聊天是公开频道,不要把 TA 在 QQ 上告诉你的私密信息说给其他玩家听。）")
    except Exception as e:
        print(f"[IDENTITY] 游戏侧身份提示失败: {e}")
        return ""


# ---------------- 列表 ----------------
def list_for_qq(qq: str) -> str:
    if not enabled():
        return "身份绑定功能未开启。"
    qq = _norm_qq(qq)
    lines = ["【我的跨平台身份绑定】"]
    data = load_bindings()
    person = _find_person_by_qq(data, qq)
    if not person or not person.get("mc"):
        return "你还没有绑定任何游戏名。游戏里想让我认得出你（两边记忆互通）,发 /绑定 mc:你的游戏名 就行~"
    for a in person.get("mc", []):
        if a.get("ok"):
            lines.append(f"· 游戏名 {a['name']} —— 已打通 ✓")
        elif a.get("pending_side") == "mc":
            lines.append(f"· 游戏名 {a['name']} —— 等待游戏里确认（TA 回复「确认绑定」后生效）")
        else:
            lines.append(f"· 游戏名 {a['name']} —— 等待你在 QQ 上确认（回复「同意绑定」）")
    lines.append("解除绑定用 /解除绑定 mc:游戏名")
    return "\n".join(lines)


def list_all() -> str:
    if not enabled():
        return "身份绑定功能未开启。"
    data = load_bindings()
    if not data["persons"]:
        return "目前还没有任何跨平台绑定。"
    lines = ["【全部身份绑定】"]
    for p in data["persons"]:
        bits = [f"QQ {p.get('qq')}"]
        for a in p.get("mc", []):
            st = "已打通" if a.get("ok") else ("待游戏确认" if a.get("pending_side") == "mc" else "待QQ确认")
            bits.append(f"{a['name']}({st})")
        lines.append("· " + "；".join(bits))
    return "\n".join(lines)
