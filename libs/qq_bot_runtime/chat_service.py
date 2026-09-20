# -*- coding: utf-8 -*-
"""聊天大脑：平台无关的消息理解与回复流水线。

这是智能体的核心业务逻辑，从原 bot.py 中抽取而来，不再依赖 QQ / OneBot / NapCat：
- 任何平台插件把消息转成 InboundMessage + 实现 ReplyTarget，
  就能获得完整的聊天能力（命令、记忆、图片/语音/视频、意图识别、Minecraft 操控...）。
- 平台差异（表情码、语音格式、媒体抓取）全部留在平台插件里。

流程：
  InboundMessage -> 命令分发 -> 触发判断 -> 记忆/上下文构建 -> DeepSeek -> 回复动作
"""
import asyncio
import os
import re
import time

import config
import emotion
import agent_ctx
import emoji_store
import identity
import long_term_memory
import ai_profile
import important_notes
import knowledge_service
from deepseek_client import DeepSeekClient
from glm_client import detect_image_type
from session_manager import SessionManagerAdapter
from quiet import degrade
try:  # 视觉捕获依赖 cv2/mss，未安装时聊天优雅降级（仍可用文本/记忆等功能）
    from vision_capture import vision_capture
except Exception:
    vision_capture = None
try:  # 视频理解依赖 cv2，未安装时降级
    from video_processor import describe_video
except Exception:
    describe_video = None
try:
    from mc_watcher import mc_watcher
    from mc_agent import get_agent
except Exception:  # MC 大脑已随插件分发（brain_mc_mod），未安装时聊天优雅降级
    mc_watcher = None
    get_agent = None

try:
    import mc_mods
except Exception:
    mc_mods = None
from web_tools import search_web, extract_urls, fetch_webpage
from message_bus import InboundMessage, ReplyTarget
from memory_client import vector_memory  # 重负载记忆模块代理（走 sidecar，可降级）
from ai_provider import get_llm, get_vision  # 统一多供应商抽象（能力路由 + 故障转移）


class _McAsync:
    """mc_watcher 的异步代理：把同步 HTTP 调用转进线程池执行。

    ChatService 跑在平台插件的事件循环里，而 mc_watcher 是同步 httpx
    （超时最长 3 秒），直接调用会把整个事件循环（含所有平台的消息处理）卡住。
    属性访问返回一个转线程池的异步包装，调用方式：await _mcw.fetch_state()
    """

    def __getattr__(self, name):
        if mc_watcher is None:
            raise AttributeError(
                "MC 插件未安装（brain_mc_mod）：MC 状态感知不可用，"
                "可在插件市场安装后重启")
        attr = getattr(mc_watcher, name)
        if not callable(attr):
            return attr

        async def _call(*args, **kwargs):
            return await asyncio.to_thread(attr, *args, **kwargs)

        return _call


_mcw = _McAsync()


def _brain_detail_line(brain, st) -> str:
    """把大脑 status() 转成一行摘要（/大脑 总览命令展示用）。"""
    if brain.name == "chat":
        plats = st.get("platforms") or []
        return f"接入平台: {'、'.join(plats) if plats else '（暂无）'}"
    if brain.name == "mc_mod":
        if not st.get("running"):
            return "想让我自己去玩就发 /mc自动"
        goal = str(st.get("current_goal") or "")[:40]
        act = str(st.get("last_action") or "-")[:60]
        return f"已进行 {st.get('rounds', '?')} 轮 · 目标: {goal} · 最近: {act}"
    if brain.name == "mc_bot":
        d = st.get("detail")
        if d:
            return d
        pos = st.get("pos", "?")
        hp = st.get("health")
        act = str(st.get("action") or "-")[:60]
        return f"位置{pos} 血量{hp if hp is not None else '?'} · 最近: {act}"
    return str(st.get("description") or "")


class ChatService:
    """聊天大脑。通过 handle_message() 接收任何平台的消息。"""

    def __init__(self, agent_id: str = "feiyu"):
        self.agent_id = agent_id
        self.llm = get_llm()  # 统一多供应商入口（chat/reasoning/tools/vision）
        self.vision = get_vision()  # 统一视觉层（GLM/Gemini 驱动，按任务路由 + 故障转移）
        # 使用 SessionManagerAdapter 替代 Memory，支持会话热切换
        self.memory = SessionManagerAdapter()

    # ==================================================================
    # 主入口
    # ==================================================================
    async def handle_message(self, msg: InboundMessage, reply: ReplyTarget):
        """处理一条平台无关的消息。按所属智能体设置隔离上下文，保证记忆命名空间正确。"""
        # 多智能体隔离：本次处理全程处于该智能体的记忆命名空间
        token = agent_ctx.set_agent(self.agent_id)
        try:
            return await self._handle_message(msg, reply)
        finally:
            agent_ctx.reset_agent(token)

    async def _handle_message(self, msg: InboundMessage, reply: ReplyTarget):
        user_id = msg.user_id
        text = msg.text or ""
        # 标记当前 world 为 chat：AI 调用失败时经统一入口报障（self_coding.report_ai_error）。
        # 默认关闭（BOT_SELF_CODING_ENABLED=False）时不触发，故不影响日常。
        try:
            from self_coding import set_world
            set_world("chat")
        except Exception:
            pass

        # 目标用户（私聊对象）主动发消息，视为"已回复"，重置主动说话计数
        if msg.channel_type == "private" and getattr(config, "ENABLE_PROACTIVE_SPEAKER", False):
            priv_target = str(getattr(config, "PROACTIVE_PRIVATE_USER_ID", ""))
            if str(user_id) == priv_target:
                try:
                    from proactive_speaker import get_speaker
                    get_speaker().notify_reply()
                except Exception as e:
                    degrade("libs/qq_bot_runtime/chat_service.py:135 ChatService._handle_message", e, "降级：from proactive_speaker import get_speaker")

        has_voice = bool(msg.audio_wav)
        has_quote = bool(msg.quoted_text or msg.quoted_image_refs or msg.quoted_sender)

        # 既没有文字、图片、语音、视频，也没有引用消息，忽略
        if not text.strip() and not msg.image_refs and not has_voice and not msg.has_video and not has_quote:
            return

        # ---------------- 核心大脑总览（所有已注册大脑的统一状态入口） ----------------
        if text.strip().startswith("/大脑") or text.strip().startswith("/核心大脑"):
            try:
                from agent_core import get_core
                core = get_core()
                lines = ["【核心大脑】"]
                for b in core.brains.all():
                    st = b.status()
                    state = "运行中" if st.get("running") else "未运行"
                    lines.append(f"· {b.title}（{b.name}/{b.kind}）{state}")
                    detail = _brain_detail_line(b, st)
                    if detail:
                        lines.append(f"    {detail}")
                await reply.reply("\n".join(lines))
            except Exception as e:
                await reply.reply(f"大脑总览查询失败: {e}")
            return

        # ---------------- 插件注册表总览（统一清单 + 版本 + 依赖校验） ----------------
        if text.strip().startswith("/插件"):
            try:
                import plugin_registry as reg
                sub = text.strip()[len("/插件"):].strip()
                if sub.startswith("校验") or sub == "check":
                    rep = reg.validate()
                    lines = [f"【插件注册表校验】{'通过' if rep['ok'] else '有问题'}"]
                    if rep["errors"]:
                        lines.append("错误:")
                        lines += ["  " + e for e in rep["errors"]]
                    if rep["warnings"]:
                        lines.append("告警:")
                        lines += ["  " + w for w in rep["warnings"]]
                    if not rep["errors"] and not rep["warnings"]:
                        lines.append("清单与已注册插件一致，无版本/依赖问题～")
                    await reply.reply("\n".join(lines))
                    return
                # 默认：精简清单（避免消息过长，只列名字/版本/开关态）
                lines = [f"【插件注册表】v{reg.CORE_VERSION}"
                         f" 共{len(reg.SPECS)}项 启用{len(reg.enabled_specs())}项"]
                kind_title = {"platform": "平台", "feature": "功能",
                              "brain": "大脑", "sidecar": "sidecar"}
                for kind in ("platform", "feature", "brain", "sidecar"):
                    items = reg.by_kind(kind)
                    if not items:
                        continue
                    lines.append(f"· {kind_title[kind]}:")
                    for s in sorted(items, key=lambda x: x.order):
                        mark = "开" if s.enabled() else "关"
                        dep = ("  ←" + ",".join(s.requires)) if s.requires else ""
                        lines.append(f"    [{mark}] {s.name} v{s.version}{dep}")
                lines.append("（发「/插件 校验」看依赖与版本问题）")
                await reply.reply("\n".join(lines))
            except Exception as e:
                await reply.reply(f"插件注册表查询失败: {e}")
            return

        # ---------------- B站直播控制（streamer 模式，bilibili_plugin） ----------------
        if text.strip().startswith("/直播"):
            try:
                from agent_core import get_core as _get_core
                bili = _get_core().plugins.get("bilibili")
            except Exception:
                bili = None
            sub = text.strip()[len("/直播"):].strip()
            if bili is None:
                await reply.reply("B站直播插件没装哦（config 开 ENABLE_BILIBILI_PLUGIN）。")
                return
            if sub.startswith("状态"):
                st = bili.status()
                lines = [f"模式: {st.get('mode')}",
                         "状态: " + ("直播中" if st.get("on_air") else "待命（/直播 开播 上线）"),
                         f"弹幕连接: {'已连接' if st.get('connected') else '未连接'}",
                         f"在线人气: {st.get('online')}"]
                s = st.get("streamer")
                if s:
                    lines.append("声音: " + ("进行中，已播 %d 分钟" % (s.get("uptime_seconds", 0) // 60)
                                             if s.get("pushing") else "未启用"))
                await reply.reply("\n".join(lines))
                return
            if not identity.is_owner_qq(user_id):
                await reply.reply("只有主人能控制我的直播哦。")
                return
            if sub.startswith("开播"):
                try:
                    await reply.reply(await bili.ensure_streaming())
                except Exception as e:
                    await reply.reply(f"开播失败: {e}")
                return
            if sub.startswith("关播"):
                await reply.reply(await bili.shutdown_streaming())
                return
            if sub.startswith("说话"):
                say = sub[len("说话"):].strip()
                if not say:
                    await reply.reply("要我说什么呀？格式：/直播 说话 内容")
                    return
                if not bili.status().get("on_air"):
                    await reply.reply("现在没在直播哦，先 /直播 开播。")
                    return
                await bili.streamer.speak(say)
                await reply.reply("说啦～")
                return
            await reply.reply("用法：/直播 开播 | /直播 关播 | /直播 状态 | /直播 说话 内容")
            return

        # ---------------- B站视频学习（移植 bilibili_learning_bot：视频理解 + 知识库） ----------------
        if text.strip().startswith("/b站看") or text.strip().startswith("/b站学"):
            sub = text.strip()[3:].strip()
            if not sub:
                await reply.reply("格式：/b站看 <BV号或视频链接>，例如 /b站看 BV1xx411c7mD")
                return
            try:
                from bili_learn import parse_bvid, summarize
                bvid, _ = parse_bvid(sub)
                if not bvid:
                    await reply.reply("没看出 BV 号或链接哦，格式：/b站看 BV1xx411c7mD")
                    return
                await reply.reply("小鱼正在看这个视频，稍等一下下～")
                res = await summarize(bvid)
                out = f"【看了《{res['title']}》】\n{res['summary']}"
                await reply.reply(out)
                try:
                    knowledge_service.get_knowledge().learn_async(res["title"], res["summary"])
                    await reply.reply("（已经把知识点记进我的小本本啦，以后聊天能想起来～）")
                except Exception as e:
                    print(f"[BILI_LEARN] 知识沉淀失败（不影响本次回复）: {e}")
            except Exception as e:
                await reply.reply(f"看视频失败: {e}")
            return

        # ---------------- 实时视觉控制命令（高优先级） ----------------
        _vision_cmds = ("/看屏幕", "/看窗口", "/看摄像头", "/看游戏", "/别看")
        if text.strip().startswith(_vision_cmds) and vision_capture is None:
            await reply.reply("视觉捕获组件未安装（需要 cv2/mss），这些功能暂时用不了哦~")
            return
        if text.strip().startswith("/看屏幕"):
            if not config.ENABLE_LIVE_VISION or not config.GEMINI_API_KEY:
                await reply.reply("实时视觉没开或者没配置 GEMINI_API_KEY 哦，先去 config.py 填一下吧。")
                return
            await vision_capture.start_screen()
            await reply.reply("好的，我开始盯着屏幕看啦，有什么变化会告诉你的~")
            return

        if text.strip().startswith("/看窗口"):
            if not config.ENABLE_LIVE_VISION or not config.GEMINI_API_KEY:
                await reply.reply("实时视觉没开或者没配置 GEMINI_API_KEY 哦，先去 config.py 填一下吧。")
                return
            title = re.sub(r'^/?看窗口[:：\s]*', '', text.strip())
            if not title:
                await reply.reply("要我看哪个窗口呀？格式：/看窗口 窗口标题")
                return
            await vision_capture.start_window(title)
            await reply.reply(f"好的，我开始盯着「{title}」这个窗口看啦~")
            return

        if text.strip().startswith("/看摄像头"):
            if not config.ENABLE_LIVE_VISION or not config.GEMINI_API_KEY:
                await reply.reply("实时视觉没开或者没配置 GEMINI_API_KEY 哦，先去 config.py 填一下吧。")
                return
            index = 0
            m = re.search(r'(\d+)$', text.strip())
            if m:
                index = int(m.group(1))
            try:
                await vision_capture.start_camera(index)
                await reply.reply(f"好的，我打开摄像头 {index} 开始看啦~")
            except Exception as e:
                await reply.reply(f"摄像头打开失败：{e}")
            return

        if text.strip().startswith("/看游戏"):
            if not config.ENABLE_LIVE_VISION or not config.GEMINI_API_KEY:
                await reply.reply("实时视觉没开或者没配置 GEMINI_API_KEY 哦，先去 config.py 填一下吧。")
                return
            output_idx = 0
            m = re.search(r'(\d+)$', text.strip())
            if m:
                output_idx = int(m.group(1))
            try:
                await vision_capture.start_dxgi(output_idx)
                await reply.reply(f"好的，我开始看显示器 {output_idx} 啦~")
            except Exception as e:
                await reply.reply(f"捕获启动失败：{e}")
            return

        if text.strip().startswith("/别看"):
            await vision_capture.stop()
            await reply.reply("唔...那我闭上眼睛休息会儿。")
            return

        # ---------------- 电脑操控命令（主人专属，pc_agent/pc_control） ----------------
        if text.strip().startswith("/电脑") or text.strip().startswith("/操控电脑"):
            await self._handle_pc_command(msg, reply)
            return

        # ---------------- PVZ 游戏命令（主人专属，pvz_agent） ----------------
        if text.strip().startswith("/pvz") or text.strip().startswith("/PVZ") or text.strip().startswith("/植物大战僵尸"):
            await self._handle_pvz_command(msg, reply)
            return

        # ---------------- Minecraft 非视觉监听命令 ----------------
        if text.strip().startswith("/看mc") or text.strip().startswith("/看MC") or text.strip().startswith("/看我的世界"):
            if not getattr(config, "ENABLE_MC_WATCH", False):
                await reply.reply("Minecraft 监听没开哦，去 config.py 把 ENABLE_MC_WATCH 设成 True 吧~")
                return
            ok = await _mcw.start()
            if ok:
                log_path = await _mcw.get_log_path()
                await reply.reply(f"好哒，我开始盯着 Minecraft 的日志看啦！日志位置：\n{log_path}\n有玩家进出、聊天、死亡、成就这些事我都会知道的~")
            else:
                await reply.reply("唔...没找到 Minecraft 的日志文件哦。可以告诉我你的 .minecraft 目录（或 logs/latest.log 路径），我去 config.py 里填上~")
            return

        if text.strip().startswith("/mc状态"):
            if not await _mcw.is_running():
                await reply.reply("我现在没在监听 Minecraft 呢，先发 /看mc 让我开始吧~")
                return
            msgs = []
            state = await _mcw.fetch_state()
            state_text = await _mcw.format_state_text(state)
            if state_text:
                msgs.append("【当前玩家状态】\n" + state_text)
            else:
                msgs.append("【玩家状态】暂时读不到 FeiyuAPI 接口，可能是游戏没开或 mod 没加载。日志监听仍然生效。")
            env_text = await _mcw.format_environment_text(state)
            if env_text:
                msgs.append("【附近环境】\n" + env_text)
            events = await _mcw.get_events(clear=True)
            if events:
                lines = [e["summary"] for e in events]
                msgs.append("【最近的游戏动态】\n" + "\n".join(lines))
            await reply.reply("\n\n".join(msgs))
            return

        if text.strip().startswith("/mc周围") or text.strip().startswith("/mc附近") or text.strip().startswith("/mc看"):
            if not await _mcw.is_running():
                await reply.reply("我现在没在监听 Minecraft 呢，先发 /看mc 让我开始吧~")
                return
            env_text = await _mcw.format_environment_text()
            if env_text:
                await reply.reply("我感知到的附近环境：\n" + env_text)
            else:
                await reply.reply("暂时读不到环境数据，可能是游戏没开或 FeiyuAPI mod 没加载哦~")
            return

        # ---------------- Minecraft 操控命令 ----------------
        if text.strip().startswith("/mc急停") or text.strip().startswith("/mc停"):
            result = await _mcw.control_stop()
            if result.get("ok"):
                await reply.reply("好，我已经停下来啦！所有操控都停止了~")
            else:
                await reply.reply(f"停止失败：{result.get('error','未知错误')}")
            return

        if text.strip().startswith("/mc走"):
            if not await _mcw.is_running():
                await reply.reply("我现在没在监听 Minecraft 呢，先发 /看mc 让我开始吧~")
                return
            raw = re.sub(r'^/?mc走[:：\s]*', '', text.strip())
            duration = 1.0
            m = re.search(r'(\d+(?:\.\d+)?)\s*$', raw)
            if m:
                duration = float(m.group(1))
                raw = raw[:m.start()].strip()
            keys = {}
            if "前" in raw: keys["forward"] = True
            if "后" in raw: keys["back"] = True
            if "左" in raw: keys["left"] = True
            if "右" in raw: keys["right"] = True
            if "跳" in raw: keys["jump"] = True
            if "蹲" in raw: keys["sneak"] = True
            if not keys:
                await reply.reply("想让我怎么走呀？格式：/mc走 前进2 或 /mc走 前左1.5（支持 前后左右 跳 蹲）")
                return
            result = await _mcw.control_move(keys, duration)
            if result.get("ok"):
                dirs = "、".join(k for k, v in keys.items() if v)
                await reply.reply(f"好，我让玩家{dirs}走 {duration} 秒啦~")
            else:
                await reply.reply(f"操控失败：{result.get('error','未知错误')}")
            return

        if text.strip().startswith("/mc转向") or text.strip().startswith("/mc转身"):
            if not await _mcw.is_running():
                await reply.reply("我现在没在监听 Minecraft 呢，先发 /看mc 让我开始吧~")
                return
            raw = re.sub(r'^/?mc[转向转身]+[:：\s]*', '', text.strip())
            dir_map = {"北": 180.0, "南": 0.0, "东": -90.0, "西": 90.0,
                       "东北": -135.0, "西北": 135.0, "东南": -45.0, "西南": 45.0}
            if raw in dir_map:
                yaw = dir_map[raw]
            else:
                try:
                    yaw = float(raw)
                except ValueError:
                    await reply.reply("格式：/mc转向 北 或 /mc转向 90（角度，0=南 90=西 180=北 -90=东）")
                    return
            result = await _mcw.control_look(yaw, 0.0, True)
            if result.get("ok"):
                await reply.reply(f"好，我转向 {raw if raw in dir_map else yaw}° 啦~")
            else:
                await reply.reply(f"转向失败：{result.get('error','未知错误')}")
            return

        if text.strip().startswith("/mc执行") or text.strip().startswith("/mc命令"):
            if not await _mcw.is_running():
                await reply.reply("我现在没在监听 Minecraft 呢，先发 /看mc 让我开始吧~")
                return
            raw = re.sub(r'^/?mc[执行命令]+[:：\s]*', '', text.strip())
            if not raw:
                await reply.reply("格式：/mc执行 give @p diamond 1（需要游戏开启作弊）")
                return
            result = await _mcw.control_cmd(raw)
            if result.get("ok"):
                await reply.reply(f"好，命令已发送：/{raw}")
            else:
                await reply.reply(f"命令执行失败：{result.get('error','未知错误')}")
            return

        if text.strip().startswith("/mc使用") or text.strip().startswith("/mc吃") or text.strip().startswith("/mc用"):
            if not await _mcw.is_running():
                await reply.reply("我现在没在监听 Minecraft 呢，先发 /看mc 让我开始吧~")
                return
            result = await _mcw.use_hand_item()
            if result.get("ok"):
                await reply.reply(f"好，使用了「{result.get('used_item','?')}」~")
            else:
                await reply.reply(f"使用失败：{result.get('error','未知错误')}")
            return

        if text.strip().startswith("/mc交互") or text.strip().startswith("/mc开门") or text.strip().startswith("/mc开箱"):
            if not await _mcw.is_running():
                await reply.reply("我现在没在监听 Minecraft 呢，先发 /看mc 让我开始吧~")
                return
            result = await _mcw.interact()
            if result.get("ok"):
                await reply.reply(f"好，我交互了前方的「{result.get('target','方块')}」~")
            else:
                await reply.reply(f"交互失败：{result.get('error','未知错误')}")
            return

        if text.strip().startswith("/mc容器") or text.strip().startswith("/mc箱子"):
            if not await _mcw.is_running():
                await reply.reply("我现在没在监听 Minecraft 呢，先发 /看mc 让我开始吧~")
                return
            result = await _mcw.container_info()
            if not result.get("ok"):
                await reply.reply(f"读不到容器：{result.get('error','未知错误')}（需要先打开箱子/背包界面）")
                return
            cont = result.get("container", {})
            items = cont.get("items", [])
            if not items:
                await reply.reply(f"当前容器是「{cont.get('title','?')}」，但里面是空的~")
                return
            lines = [f"当前容器「{cont.get('title','?')}」（{len(items)}种物品）："]
            for it in items[:20]:
                lines.append(f"  槽位{it.get('slot')}: {it.get('item','?')}x{it.get('count')}")
            await reply.reply("\n".join(lines))
            return

        if text.strip().startswith("/mc转移"):
            if not await _mcw.is_running():
                await reply.reply("我现在没在监听 Minecraft 呢，先发 /看mc 让我开始吧~")
                return
            parts = re.sub(r'^/?mc转移[:：\s]*', '', text.strip()).split()
            if len(parts) < 2:
                await reply.reply("格式：/mc转移 <源槽位> <目标槽位>（先用 /mc容器 查看槽位号）")
                return
            try:
                from_slot = int(parts[0])
                to_slot = int(parts[1])
            except ValueError:
                await reply.reply("槽位号必须是数字哦~")
                return
            result = await _mcw.move_item(from_slot, to_slot)
            if result.get("ok"):
                await reply.reply(f"好，把物品从槽位{from_slot}移到了{to_slot}~")
            else:
                await reply.reply(f"转移失败：{result.get('error','未知错误')}")
            return

        if text.strip().startswith("/mc合成") or text.strip().startswith("/mc造"):
            if not await _mcw.is_running():
                await reply.reply("我现在没在监听 Minecraft 呢，先发 /看mc 让我开始吧~")
                return
            raw = re.sub(r'^/?mc[合成造]+[:：\s]*', '', text.strip()).split()
            if not raw:
                await reply.reply("格式：/mc合成 钻石剑 或 /mc合成 diamond_sword 2")
                return
            item = raw[0]
            count = 1
            if len(raw) > 1:
                try:
                    count = int(raw[1])
                except ValueError as e:
                    degrade("libs/qq_bot_runtime/chat_service.py:540 ChatService._handle_message", e, "降级：count = int(raw[1])")
            result = await _mcw.craft(item, count)
            if result.get("ok"):
                await reply.reply(f"合成了 {count} 个 {item}~（注意：需要游戏开启作弊）")
            else:
                await reply.reply(f"合成失败：{result.get('error','未知错误')}")
            return

        if text.strip().startswith("/mc自动") or text.strip().startswith("/mc自己玩") or text.strip().startswith("/mc托管"):
            if not getattr(config, "ENABLE_MC_AGENT", False):
                await reply.reply("自主游戏代理没开哦，去 config.py 把 ENABLE_MC_AGENT 设成 True 吧~")
                return
            if not await _mcw.is_running():
                if not await _mcw.is_http_api_up():
                    await reply.reply("Minecraft 还没连上，我先看不到游戏状态；先把游戏和 FeiyuAPI 模组打开吧~")
                    return
            if not await _mcw.is_http_api_up():
                await reply.reply("FeiyuAPI 接口不在线，游戏没开或者没重启加载新版 mod 哦~")
                return
            agent = get_agent()
            if agent.is_running():
                await reply.reply("我已经在自主玩啦，先发 /mc停止自动 让我停手吧~")
                return
            ok = await asyncio.to_thread(agent.start)
            if ok:
                await reply.reply("好哒，我开始自己玩 Minecraft 啦！我会自主探索、挖矿、打怪、盖房子，想让我停下就说 /mc停止自动~")
            else:
                await reply.reply("自主游戏启动失败，可能是 FeiyuAPI 接口不可用哦~")
            return

        if text.strip().startswith("/mc停止自动") or text.strip().startswith("/mc停下") or text.strip().startswith("/mc别玩了"):
            agent = get_agent()
            if agent.is_running():
                await asyncio.to_thread(agent.stop)
                await reply.reply("好，我停下自主玩啦！要看情况发 /mc状态 或 /mc周围 都行~")
            else:
                await reply.reply("我现在没有在自主玩哦~")
            return

        if text.strip().startswith("/mc代理状态") or text.strip().startswith("/mc自动状态"):
            agent = get_agent()
            stats = await asyncio.to_thread(agent.get_stats)
            if stats["running"]:
                lines = [f"自主游戏运行中：已进行 {stats['rounds']} 轮，运行 {stats['uptime']} 秒"]
                recent = agent.get_recent_actions()
                if recent:
                    lines.append("最近动作：")
                    lines.extend(f"- {a}" for a in recent)
                await reply.reply("\n".join(lines))
            else:
                await reply.reply("自主游戏当前没在运行，发 /mc自动 让我开始吧~")
            return

        if text.strip().startswith("/目标") or text.strip().startswith("/mc目标") or text.strip().startswith("/当前目标"):
            agent = get_agent()
            goals = agent.get_goals()
            lines = [f"【我现在的目标】{goals.get('current', '未知')}"]
            if goals.get("next"):
                lines.append(f"【完成后的下一步】{goals.get('next')}")
            else:
                lines.append("【完成后的下一步】（暂时还没定）")
            stats = await asyncio.to_thread(agent.get_stats)
            if stats["running"]:
                lines.append(f"（自主运行中，已进行{stats['rounds']}轮）")
            else:
                lines.append("（自主游戏当前没在运行，发 /mc自动 让我开始吧）")
            await reply.reply("\n".join(lines))
            return

        if text.strip().startswith("/主动说话") or text.strip().startswith("/主动闲聊") or text.strip().startswith("/mc闲聊"):
            if not getattr(config, "ENABLE_PROACTIVE_SPEAKER", False):
                await reply.reply("主动说话没开哦，去 config.py 把 ENABLE_PROACTIVE_SPEAKER 设成 True 吧~")
                return
            from proactive_speaker import get_speaker
            from message_bus import get_sender
            sp = get_speaker()
            sp.set_sender(get_sender())
            msg_text = await sp.trigger_casual()
            if msg_text:
                await reply.reply(f"好，我这就说：\n{msg_text}")
            else:
                await reply.reply("唔...我现在想不到什么有趣的话题呢，让我酝酿一下~")
            return

        # ---------------- Minecraft 游戏技巧知识库 ----------------
        if text.strip().startswith("/教肥鱼娘") or text.strip().startswith("/教肥鱼") or text.strip().startswith("/教会肥鱼娘"):
            raw = re.sub(r'^/?[教教会]+肥鱼(娘)?[:：\s]*', '', text.strip())
            if not raw:
                await reply.reply("想教我什么呀？格式：/教肥鱼娘 遇到苦力怕要后退躲避")
                return
            from mc_tips import add_tip
            result = add_tip(raw, source="user")
            if result.get("ok"):
                await reply.reply(f"记住啦！我学会了：「{raw}」~")
            else:
                await reply.reply(result.get("error", "保存失败"))
            return

        if text.strip().startswith("/技巧") or text.strip().startswith("/我学会的") or text.strip().startswith("/游戏技巧"):
            from mc_tips import list_tips, count_tips
            tips = list_tips()
            if not tips:
                await reply.reply("我还没学会任何游戏技巧呢，你可以 /教肥鱼娘 <技巧> 教我，或者让我自己玩一阵子学习~")
                return
            lines = [f"我目前会 {count_tips()} 条游戏技巧："]
            for i, t in enumerate(tips, 1):
                src = "你教的" if t.get("source") == "user" else "我自己学的"
                lines.append(f"{i}. {t.get('content','')}（{src}）")
            await reply.reply("\n".join(lines))
            return

        if text.strip().startswith("/忘记技巧") or text.strip().startswith("/删技巧") or text.strip().startswith("/不要这个技巧"):
            raw = re.sub(r'^/?[忘记删不要这个]+技巧[:：\s]*', '', text.strip())
            if not raw:
                await reply.reply("想忘掉哪条技巧呀？格式：/忘记技巧 苦力怕")
                return
            from mc_tips import delete_tip
            result = delete_tip(raw)
            if result.get("ok"):
                deleted = [t.get("content", "") for t in result.get("deleted", [])]
                await reply.reply(f"忘掉了 {len(deleted)} 条技巧：\n" + "\n".join(f"- {d}" for d in deleted))
            else:
                await reply.reply(result.get("error", "删除失败"))
            return

        if text.strip().startswith("/探索状态") or text.strip().startswith("/探索"):
            from mc_explored import explored_count, get_explored_text, list_landmarks
            explored = explored_count()
            lm = list_landmarks()
            lines = [f"我探索过 {explored} 个区块"]
            et = get_explored_text()
            if et:
                lines.append(et)
            if lm:
                lines.append(f"记了 {len(lm)} 个地标")
            else:
                lines.append("还没有记录地标")
            await reply.reply("\n".join(lines))
            return

        if text.strip().startswith("/地标") or text.strip().startswith("/去过的地方"):
            from mc_explored import list_landmarks, count_landmarks
            lms = list_landmarks()
            if not lms:
                await reply.reply("我还没记下任何地标呢。自主探索时发现村庄/矿洞会记住，你也可以用「记地标 名字」让我记~")
                return
            lines = [f"我记得 {count_landmarks()} 个地标："]
            for lm in lms:
                lines.append(f"- {lm.get('name')}（{lm.get('type')}）@X={lm.get('x')} Y={lm.get('y')} Z={lm.get('z')}")
            await reply.reply("\n".join(lines))
            return

        if text.strip().startswith("/资源") or text.strip().startswith("/资源地图"):
            from mc_explored import list_resources, count_resources
            resources = list_resources()
            if not resources:
                await reply.reply("我还没记下任何资源点呢。自主探索时发现钻石矿/村庄/矿洞会自动记住~")
                return
            lines = [f"我记住了 {count_resources()} 个资源点："]
            for r in resources:
                extra = f"（{r.get('extra')}）" if r.get("extra") else ""
                lines.append(f"- {r.get('name')} @X={r.get('x')} Z={r.get('z')}{extra}")
            await reply.reply("\n".join(lines))
            return

        if text.strip().startswith("/记地标") or text.strip().startswith("/记地点"):
            raw = re.sub(r'^/?[记记录]+[地地点]+[:：\s]*', '', text.strip())
            if not raw:
                await reply.reply("想让我记住这个位置叫什么呀？格式：/记地标 我家")
                return
            state = await _mcw.fetch_state()
            p = state.get("player") if state else None
            if not p or p.get("x") is None:
                await reply.reply("读不到当前坐标，游戏没开或接口不在线哦~")
                return
            from mc_explored import add_landmark
            result = add_landmark(raw, p.get("x"), p.get("y", 0), p.get("z"), "landmark")
            if result.get("ok"):
                lm = result.get("landmark", {})
                await reply.reply(f"记住啦！「{raw}」在 X={lm.get('x')} Y={lm.get('y')} Z={lm.get('z')}~")
            else:
                await reply.reply(result.get("error", "记录失败"))
            return

        # ---------------- BOT Self Coding（智能体自编程）命令 ----------------
        if text.strip().startswith("/开启自我编程"):
            from self_coding import set_enabled, set_perm
            raw = re.sub(r'^/?开启自我编程[:：\s]*', '', text.strip()).strip()
            perm = raw if raw else None
            set_enabled(True)
            msg = "已开启 BOT Self Coding ✅ 我现在可以用内置构建助手改进自己啦~"
            if perm:
                r = set_perm(perm)
                if r.get("ok"):
                    msg += f"\n权限档已设为：{perm}"
                else:
                    msg += f"\n（权限档设置失败：{r.get('error')}）"
            await reply.reply(msg)
            return

        if text.strip().startswith("/关闭自我编程"):
            from self_coding import set_enabled
            set_enabled(False)
            await reply.reply("已关闭 BOT Self Coding。我不再自行改代码，需要时再 /开启自我编程 即可。")
            return

        if text.strip().startswith("/自我编程权限"):
            from self_coding import set_perm, get_perm, is_enabled
            raw = re.sub(r'^/?自我编程权限[:：\s]*', '', text.strip()).strip()
            if not raw:
                await reply.reply(f"当前权限档：{get_perm()}（开启中={is_enabled()}）。"
                                  f"可设：plan / default / acceptEdits / full / bypassPermissions")
                return
            r = set_perm(raw)
            if r.get("ok"):
                await reply.reply(f"权限档已设为：{raw}")
            else:
                await reply.reply(f"设置失败：{r.get('error')}")
            return

        if text.strip().startswith("/自我编程设置"):
            from self_coding import set_issue_auto, set_auto_load, is_enabled, get_perm
            body = re.sub(r'^/?自我编程设置[:：\s]*', '', text.strip()).strip().lower()
            auto_issue = None
            auto_load = None
            if "issue自动" in body or "自动执行" in body:
                auto_issue = "关" not in body and "否" not in body and "off" not in body
                set_issue_auto(auto_issue)
            if "自动装载" in body or "自动启动" in body:
                auto_load = "关" not in body and "否" not in body and "off" not in body
                set_auto_load(auto_load)
            await reply.reply(
                f"已更新（开启中={is_enabled()}，权限={get_perm()}）：\n"
                f"  Issue 自动执行 = {'开' if auto_issue is None else ('开' if auto_issue else '关')}\n"
                f"  产物自动装载 = {'开' if auto_load is None else ('开' if auto_load else '关')}")
            return

        if text.strip().startswith("/提issue") or text.strip().startswith("/提需求"):
            from self_coding import file_issue, is_enabled
            if not is_enabled():
                await reply.reply("BOT Self Coding 还没开哦，先 /开启自我编程 我才好提需求给自己做~")
                return
            raw = re.sub(r'^/?(提issue|提需求)[:：\s]*', '', text.strip()).strip()
            if not raw:
                await reply.reply("格式：/提issue 想要一个能定时总结聊天的大脑")
                return
            r = file_issue(title=raw, kind="feature")
            if r.get("ok"):
                if r.get("auto"):
                    await reply.reply(f"已提 Issue {r['issue_id']} 并自动派给构建助手执行中（默认自动执行）~")
                else:
                    await reply.reply(f"已提 Issue {r['issue_id']}（待你同意才执行，/同意issue {r['issue_id']}）")
            else:
                await reply.reply(f"提 Issue 失败：{r.get('error')}")
            return

        if text.strip().startswith("/待确认issue") or text.strip().startswith("/issue列表"):
            from self_coding import list_issues
            pend = list_issues("pending")
            open_ = list_issues("open")
            done = list_issues("done")
            if not (pend or open_ or done):
                await reply.reply("暂时没有自我编程 Issue~")
                return
            lines = ["自我编程 Issue："]
            for s, lst in (("待你同意", pend), ("执行中", open_), ("已完成", done)):
                for it in lst:
                    lines.append(f"[{it['id']}] ({s}) {it.get('title','')}")
            await reply.reply("\n".join(lines))
            return

        if text.strip().startswith("/同意issue"):
            from self_coding import approve_issue
            rid = re.sub(r'^/?同意issue[:：\s]*', '', text.strip()).strip()
            if not rid:
                await reply.reply("格式：/同意issue <IssueID>")
                return
            r = approve_issue(rid)
            await reply.reply("已派给构建助手执行~" if r.get("ok") else f"失败：{r.get('error')}")
            return

        if text.strip().startswith("/拒绝issue"):
            from self_coding import reject_issue
            rid = re.sub(r'^/?拒绝issue[:：\s]*', '', text.strip()).strip()
            if not rid:
                await reply.reply("格式：/拒绝issue <IssueID>")
                return
            r = reject_issue(rid)
            await reply.reply("已拒绝该 Issue~" if r.get("ok") else f"失败：{r.get('error')}")
            return

        if text.strip().startswith("/别看mc") or text.strip().startswith("/别看MC"):
            await _mcw.stop()
            await reply.reply("好哒，我不看 Minecraft 的日志啦~")
            return

        # ---------------- 重要信息存储命令 ----------------
        if text.strip().startswith("/记住") or text.strip().startswith("/记下"):
            raw = re.sub(r'^/?[记住记下]+[:：\s]*', '', text.strip())
            if not raw:
                await reply.reply("想让我记住什么呀？格式：/记住 内容 或 /记住 分类:内容")
                return
            category = ""
            note_text = raw
            if ":" in raw or "：" in raw:
                cat, _, rest = re.split(r'[:：]', raw, maxsplit=1)
                if cat.strip() and rest.strip():
                    category = cat.strip()
                    note_text = rest.strip()
            added = important_notes.add_note(user_id, note_text, category)
            if added:
                await reply.reply(f"好哒，我记下啦：{note_text}")
            else:
                await reply.reply("这个我之前已经记住啦，不用重复哦~")
            return

        if text.strip().startswith("/重要信息") or text.strip().startswith("/我的笔记"):
            notes = important_notes.get_user_notes(user_id)
            if not notes:
                await reply.reply("目前还没有记住任何重要信息呢，可以用 /记住 内容 让我记下来~")
                return
            lines = [f"{i+1}. {n.get('text', '')}" for i, n in enumerate(notes)]
            await reply.reply("我记得的这些重要信息：\n" + "\n".join(lines))
            return

        if text.strip().startswith("/忘记"):
            raw = re.sub(r'^/?忘记[:：\s]*', '', text.strip())
            if not raw:
                await reply.reply("想让我忘记哪个？格式：/忘记 序号 或 /忘记 关键词")
                return
            try:
                idx = int(raw)
                removed = important_notes.delete_note(user_id, index=idx)
                if removed:
                    await reply.reply(f"好哒，我忘了第 {idx} 条~")
                else:
                    await reply.reply(f"没有第 {idx} 条哦，看看 /重要信息 里的序号~")
                return
            except ValueError as e:
                degrade("libs/qq_bot_runtime/chat_service.py:812 ChatService._handle_message", e, "降级：idx = int(raw)")
            removed = important_notes.delete_note(user_id, keyword=raw)
            if removed:
                await reply.reply(f"好哒，我忘了 {removed} 条相关的重要信息~")
            else:
                await reply.reply(f"没找到和「{raw}」相关的重要信息~")
            return

        if text.strip().startswith("/清空笔记") or text.strip().startswith("/全部忘记"):
            removed = important_notes.clear_notes(user_id)
            await reply.reply(f"好哒，我把 {removed} 条重要信息都清空啦~")
            return

        # ---------------- 跨平台身份绑定（QQ ↔ MC 同一个人记忆打通） ----------------
        # 纯文本确认：游戏里有人声明「QQ号是我」后，本 QQ 侧回复「同意绑定」/「不是」
        pend = identity.pending_qq_names(user_id) if identity.enabled() else []
        if pend:
            direct = (msg.channel_type == "private"
                      or getattr(msg, "mentioned", False) or getattr(msg, "quoted_self", False))
            if direct and text.strip() in identity.QQ_CONFIRM_WORDS:
                ok, txt = identity.confirm_on_qq(user_id)
                await reply.reply(txt)
                return
            if direct and text.strip() in identity.QQ_DENY_WORDS:
                ok, txt = identity.deny_on_qq(user_id)
                await reply.reply(txt)
                return

        if text.strip().startswith("/绑定"):
            cmd = text.strip()
            if cmd.startswith("/绑定列表") or cmd.startswith("/绑定状态"):
                await reply.reply(identity.list_for_qq(user_id))
                return
            if cmd.startswith("/绑定总览") or cmd.startswith("/绑定全部"):
                if not identity.is_owner_qq(user_id):
                    await reply.reply("只有主人能看全部绑定哦。")
                    return
                await reply.reply(identity.list_all())
                return
            if cmd.startswith("/绑定批准"):
                if not identity.is_owner_qq(user_id):
                    await reply.reply("只有主人能直接批准绑定哦。")
                    return
                raw = re.sub(r'^/?绑定批准\s*(?:mc|MC|游戏名)?\s*[:：]?\s*', '', cmd)
                if not raw:
                    await reply.reply("格式：/绑定批准 mc:游戏名")
                    return
                ok, txt = identity.force_activate(user_id, raw.strip())
                await reply.reply(txt)
                return
            if cmd == "/绑定":
                await reply.reply(
                    "想让我在 QQ 和游戏里认出同一个人？发 /绑定 mc:你的游戏名（如 /绑定 mc:insomnic）。\n"
                    "绑定后两边聊到你都会用同一份记忆（档案/笔记/历史互通）。\n"
                    "其它：/绑定列表 看进度；/绑定总览（主人）；/解除绑定 mc:名字 解除。")
                return
            raw = re.sub(r'^/?绑定\s*', '', cmd)
            name = raw.strip()
            plat = ""
            # 找第一个 ':' 或 '：' 分隔平台与名字（两种分隔符都可能出现）
            idx = -1
            for c in (":", "："):
                j = name.find(c)
                if j != -1 and (idx == -1 or j < idx):
                    idx = j
            if idx != -1:
                plat = name[:idx].strip().lower()
                name = name[idx + 1:].strip()
            if plat and plat != "mc":
                await reply.reply("目前只支持绑定 MC 游戏名：/绑定 mc:游戏名")
                return
            if not name or not re.fullmatch(r"[a-zA-Z0-9_]{2,16}", name.strip()):
                await reply.reply("格式：/绑定 mc:游戏名（字母数字下划线，2~16 位，如 /绑定 mc:insomnic）")
                return
            ok, txt = identity.request_bind(user_id, name.strip())
            await reply.reply(txt)
            return

        if text.strip().startswith("/解除绑定"):
            raw = re.sub(r'^/?解除绑定\s*(?:mc|MC|游戏名)?\s*[:：]?\s*', '', text.strip())
            if not raw:
                await reply.reply("格式：/解除绑定 mc:游戏名")
                return
            ok, txt = identity.unbind_mc(user_id, raw.strip(), is_owner=identity.is_owner_qq(user_id))
            await reply.reply(txt)
            return

        # ---------------- AI 自我认知档案命令 ----------------
        if text.strip().startswith("/我的档案") or text.strip().startswith("/查看我的档案"):
            profile = ai_profile.list_profile_fields()
            await reply.reply("这是我的自我认知档案：\n" + profile)
            return

        if text.strip().startswith("/修改档案"):
            raw = re.sub(r'^/?修改档案[:：\s]*', '', text.strip())
            if not raw or (":" not in raw and "：" not in raw):
                await reply.reply("格式：/修改档案 身份:我是xxx 或 性格:我很温柔")
                return
            field, _, value = re.split(r'[:：]', raw, maxsplit=1)
            field_map = {
                "身份": "identity", "性格": "personality", "能力": "abilities",
                "工作习惯": "habits", "习惯": "habits", "其他": "extra", "自我认知": "extra",
            }
            key = field_map.get(field.strip())
            if not key:
                await reply.reply("可修改的字段：身份、性格、能力、工作习惯、其他")
                return
            ok = ai_profile.update_field(key, value.strip())
            if ok:
                await reply.reply(f"好哒，我把{field.strip()}改成：{value.strip()}")
            else:
                await reply.reply("修改失败，字段名不对哦~")
            return

        if text.strip().startswith("/重置档案"):
            ai_profile.reset_profile()
            await reply.reply("好哒，我把自己的档案恢复成默认啦~")
            return

        # ---------------- AI 心情档案（emotion.py 情绪模块，全局一份心情）----------------
        if text.strip().startswith("/心情"):
            if not getattr(config, "EMOTION_ENABLED", False):
                await reply.reply("情绪模块没开哦，去 config.py 把 EMOTION_ENABLED 设成 True 吧~")
                return
            raw = re.sub(r'^/?心情[:：\s]*', '', text.strip())
            if raw in ("重置", "清零", "和好"):
                emotion.reset_mood()
                await reply.reply("唔…既然你主动提了，那本鱼就当什么都没发生过好啦，心情恢复平静~")
                return
            if raw:
                await reply.reply("格式：/心情（查看我现在的心情） 或 /心情 重置（清零重来）"
                                  "\n其实直接问我「你心情怎么样」也可以哦~")
                return
            await reply.reply(emotion.describe_mood())
            return

        # ---------------- 反思记忆（reflection_memory.py）----------------
        if text.strip().startswith("/反思"):
            if not getattr(config, "ENABLE_REFLECTION", True):
                await reply.reply("反思记忆没开哦，去 config.py 把 ENABLE_REFLECTION 设成 True 吧~")
                return
            raw = re.sub(r'^/?反思[:：\s]*', '', text.strip())
            if raw in ("清", "清空", "清除"):
                import reflection_memory
                reflection_memory.clear_reflections(user_id)
                await reply.reply("反思记录已清空，我会重新学习的~")
                return
            if raw in ("统计", "状态"):
                import reflection_memory
                stats = reflection_memory.get_reflection_stats()
                await reply.reply(
                    f"【反思记忆统计】\n"
                    f"对话轮数: {stats['total_conversations']}\n"
                    f"反思次数: {stats['total_reflections']}\n"
                    f"交互规则: {stats['interaction_rules_count']} 条"
                )
                return
            if not raw:
                import reflection_memory
                stats = reflection_memory.get_reflection_stats()
                hint = reflection_memory.build_reflection_hint(user_id)
                if hint:
                    await reply.reply(f"【我的反思记忆】\n{hint}")
                else:
                    await reply.reply("还没有关于你的反思记录呢，我们多聊聊就会有啦~")
                return
            await reply.reply("格式：/反思（查看） | /反思 统计 | /反思 清")
            return

        # ---------------- 人格记忆（persona_memory.py）----------------
        if text.strip().startswith("/人格"):
            if not getattr(config, "ENABLE_PERSONA", True):
                await reply.reply("人格记忆没开哦，去 config.py 把 ENABLE_PERSONA 设成 True 吧~")
                return
            raw = re.sub(r'^/?人格[:：\s]*', '', text.strip())
            if raw in ("清", "清空", "清除"):
                import persona_memory
                persona_memory.clear_persona(user_id)
                await reply.reply("人格记忆已清空，我会重新了解你的~")
                return
            if raw in ("列表", "全部", "统计"):
                import persona_memory
                await reply.reply(persona_memory.list_personas())
                return
            if not raw:
                import persona_memory
                hint = persona_memory.build_persona_hint(user_id)
                if hint:
                    await reply.reply(f"【我与你相处的方式】\n{hint}")
                else:
                    await reply.reply("还没有关于你的相处记忆呢，我们多聊聊就会有的~")
                return
            await reply.reply("格式：/人格（查看） | /人格 列表 | /人格 清")
            return

        # ---------------- 向量记忆（vector_memory.py）----------------
        if text.strip().startswith("/向量"):
            if not getattr(config, "ENABLE_VECTOR_MEMORY", False):
                await reply.reply("向量记忆没开哦，去 config.py 把 ENABLE_VECTOR_MEMORY 设成 True 吧~")
                return
            raw = re.sub(r'^/?向量[:：\s]*', '', text.strip())
            if raw in ("清", "清空", "清除"):
                await vector_memory.clear_vectors(user_id)
                await reply.reply("向量记忆已清空，我会重新索引的~")
                return
            if raw in ("统计", "状态"):
                stats = await vector_memory.get_vector_stats()
                await reply.reply(
                    f"【向量记忆统计】\n"
                    f"用户数: {stats['user_count']}\n"
                    f"总向量数: {stats['total_vectors']}\n"
                    f"模型: {stats['model_name']}"
                )
                return
            await reply.reply("格式：/向量 统计 | /向量 清")
            return

        # ---------------- 实时语音对讲（本机免提全双工，voice_room.py）----------------
        if config.ENABLE_REALTIME_VOICE:
            cmd = text.strip()
            # 只允许本机控制台或主人私聊操作（语音对讲会占用本机麦克风）
            is_console = msg.platform == "console"
            owner = config.REALTIME_OWNER_QQ or config.BALANCE_ALERT_USER_ID
            is_owner = msg.channel_type == "private" and (not owner or msg.user_id == owner)
            if cmd in ("/语音对讲", "/开语音") or cmd == "/语音对讲关" or cmd == "/语音对讲状态":
                if not (is_console or is_owner):
                    await reply.reply("语音对讲是主人专属的功能哦，不许乱动麦克风~")
                    return
                from voice_room import get_voice_room
                room = get_voice_room()
                if cmd in ("/语音对讲", "/开语音"):
                    result = await room.start()
                elif cmd == "/语音对讲关":
                    result = await room.stop(reason="QQ/控制台命令")
                else:
                    result = room.status()
                await reply.reply(result)
                return

        # ---------------- 电脑操控意图识别（自然语言触发 pc_agent）----------------
        if getattr(config, "ENABLE_PC_INTENT", False):
            try:
                from pc_intent import should_trigger_pc_agent
                should_trigger, task = should_trigger_pc_agent(text, user_id)
                if should_trigger and task:
                    # 检查 pc_agent 是否已在运行
                    from pc_agent import get_agent
                    agent = get_agent()
                    if agent.is_running():
                        await reply.reply(f"我正在操控电脑呢（任务：{agent._task_text[:40]}），等会儿再说~")
                        return
                    # 启动任务
                    ok, msg_ = agent.start_task(
                        task, 
                        requester=str(msg.user_id) if msg.platform == "qq" else "",
                        notify=reply.reply
                    )
                    await reply.reply(msg_)
                    return
            except Exception as e:
                print(f"[PC-INTENT] 意图识别失败: {e}")

        # ---------------- 触发判断 ----------------
        # 群聊接话已关闭：未被 @（should_reply 为 False）时，只有"引用了机器人自己发的
        # 消息"才允许触发（QQ 上引用=对着机器人说话）；语音/视频/引用别人的消息都不再接话。
        if not self.should_reply(msg):
            if not (has_quote and getattr(msg, "quoted_self", False)):
                return

        # ---------------- 语音消息：先转文字再走正常流程 ----------------
        if has_voice and config.ENABLE_VOICE:
            try:
                record_text = await self._transcribe_voice(msg.audio_wav)
                if record_text:
                    text = (text.strip() + " " + record_text).strip() if text.strip() else record_text
                else:
                    await reply.reply("唔...我没听清你说的什么，能再说一遍或用文字吗？")
                    return
            except Exception as e:
                print(f"[ERROR] 语音识别失败: {e}")
                await reply.reply("语音识别出错了，能发文字吗？")
                return

        # ---------------- 心情自然语言询问（emotion.py：免指令，问「你还在生气吗」直接答）----------------
        if (getattr(config, "EMOTION_ENABLED", False) and text.strip()
                and not msg.image_refs and not msg.has_video and not has_quote
                and emotion.is_mood_query(text)):
            await reply.reply(emotion.describe_mood())
            return

        # ---------------- 构建上下文并调用模型 ----------------
        try:
            await self._chat_pipeline(msg, reply, text, has_quote, has_voice)
        except Exception as e:
            # AI 调用失败已在 UnifiedLLM.chat 统一报障（self_coding.report_ai_error）。
            # 此处仅负责把错误回给用户，不再重复提 Issue。
            print(f"[ERROR] 处理消息失败: {e}")
            await reply.reply(f"抱歉，出错了：{e}")

    # ==================================================================
    # 电脑操控命令（主人专属，pc_agent.py / pc_control.py）
    # ==================================================================
    async def _handle_pc_command(self, msg: InboundMessage, reply: ReplyTarget):
        """/电脑做 /电脑停 /电脑状态 /电脑截图 —— 让智能体操控这台电脑。"""
        text = (msg.text or "").strip()
        is_console = msg.platform == "console"
        if not (is_console or identity.is_owner_qq(msg.user_id)):
            await reply.reply("操控电脑是主人专属能力哦，普通人不许碰我的鼠标键盘~")
            return
        if not getattr(config, "ENABLE_PC_CONTROL", False):
            await reply.reply("电脑操控没开哦，去 config.py 把 ENABLE_PC_CONTROL 设成 True~")
            return

        raw = re.sub(r"^/操控电脑(\s+|：|:)?", "/电脑做 ", text)   # /操控电脑 xxx ≡ /电脑做 xxx

        if raw.startswith("/电脑做"):
            task = raw[len("/电脑做"):].strip()
            if not task:
                await reply.reply("要我做什么呀？格式：/电脑做 打开记事本写一句你好\n"
                                  "其它：/电脑状态 看进度 · /电脑截图 看屏幕 · /电脑停 急停")
                return
            from pc_agent import get_agent
            ok, msg_ = get_agent().start_task(
                task, requester=str(msg.user_id) if msg.platform == "qq" else "",
                notify=reply.reply)
            await reply.reply(msg_)
            return

        if raw.startswith("/电脑停") or raw.startswith("/电脑急停"):
            from pc_agent import get_agent
            ok = get_agent().request_stop()
            await reply.reply("收到收到，我马上停手！" if ok else "我现在没有在操控电脑哦~")
            return

        if raw.startswith("/电脑状态"):
            from pc_agent import get_agent
            await reply.reply(get_agent().status_text())
            return

        if raw.startswith("/电脑截图"):
            from pc_agent import get_agent
            agent = get_agent()
            if not agent.vision_ready():
                await reply.reply("没配置视觉模型（VISION_MODEL 或 GEMINI_API_KEY），我看不见屏幕哦~")
                return
            question = raw[len("/电脑截图"):].strip()
            try:
                import pc_control
                jpeg, (iw, ih) = await asyncio.to_thread(pc_control.take_screenshot)
                if question:
                    prompt = (f"这是 Windows 电脑屏幕截图（{iw}x{ih} 像素，原点左上）。"
                              f"请回答关于屏幕的问题：{question}")
                else:
                    prompt = ("这是 Windows 电脑屏幕截图。请简要描述：当前活动窗口/应用、"
                              "界面上主要元素（带坐标）、关键文字。150 字以内。")
                desc = await agent.describe_screen(jpeg, prompt)
                await reply.reply(f"【屏幕 {iw}x{ih}】\n{desc}")
            except Exception as e:
                await reply.reply(f"截屏看画失败：{e}")
            return

        # 裸 /电脑 或未知子命令 → 帮助
        await reply.reply(
            "【电脑操控】主人专属～\n"
            "/电脑做 任务描述 — 我来帮你操作电脑（例：/电脑做 打开计算器算 123*456）\n"
            "/电脑状态 — 看当前执行进度\n"
            "/电脑截图 [问题] — 看一眼屏幕并描述/回答\n"
            "/电脑停 — 紧急停止（也可以把鼠标甩到屏幕左上角）")

    # ==================================================================
    # PVZ 游戏命令（主人专属，pvz_agent.py / pvz_vision.py / pvz_strategy.py）
    # ==================================================================
    async def _handle_pvz_command(self, msg: InboundMessage, reply: ReplyTarget):
        """/pvz玩 /pvz停 /pvz状态 /pvz看盘 —— 让智能体自己玩植物大战僵尸。"""
        text = (msg.text or "").strip()
        is_console = msg.platform == "console"
        if not (is_console or identity.is_owner_qq(msg.user_id)):
            await reply.reply(" PvZ 是我自己的小游戏时间，主人专属哦~")
            return
        if not getattr(config, "ENABLE_PVZ_BRAIN", False):
            await reply.reply("PVZ 大脑没开哦，去 config.py 把 ENABLE_PVZ_BRAIN 设成 True~")
            return

        raw = re.sub(r"^/植物大战僵尸(\s+|：|:)?", "/pvz ", text)
        raw = re.sub(r"^/pvz\s*", "/pvz ", raw, count=1)   # 归一空格

        if raw.startswith("/pvz玩"):
            hint = raw[len("/pvz玩"):].strip()
            from pvz_agent import get_agent
            ok, msg_ = get_agent().start_game(
                hint or "冒险模式",
                requester=str(msg.user_id) if msg.platform == "qq" else "",
                notify=reply.reply)
            await reply.reply(msg_)
            return

        if raw.startswith("/pvz停") or raw.startswith("/pvz急停"):
            from pvz_agent import get_agent
            ok = get_agent().request_stop()
            await reply.reply("呜…收手！" if ok else "我现在没在玩 PvZ 哦~")
            return

        if raw.startswith("/pvz状态"):
            from pvz_agent import get_agent
            await reply.reply(get_agent().status_text())
            return

        if raw.startswith("/pvz看盘"):
            from pvz_agent import get_agent
            agent = get_agent()
            try:
                state = await agent.read_board_once()
            except Exception as e:
                await reply.reply(f"看盘失败：{e}")
                return
            await reply.reply(f"【PvZ 读盘】\n{state}")
            return

        if raw.startswith("/pvz教"):
            tip = raw[len("/pvz教"):].strip()
            import pvz_tips
            res = pvz_tips.add_tip(tip, source="user")
            await reply.reply("学到啦！" if res.get("ok") else f"嗯…{res.get('error','')}")
            return

        # 裸 /pvz 或未知子命令 → 帮助
        await reply.reply(
            "【PVZ 大脑】主人专属～我可以自己玩植物大战僵尸！\n"
            "/pvz玩 [关卡说明] — 开一局（例：/pvz玩 冒险模式1-1）\n"
            "/pvz状态 — 看她现在打到哪了\n"
            "/pvz看盘 — 截一张游戏画面读局面（调试用）\n"
            "/pvz教 一句话 — 教她一条 PvZ 技巧\n"
            "/pvz停 — 叫停这局")

    # ==================================================================
    # 聊天流水线（命令之后的主路径）
    # ==================================================================
    async def _chat_pipeline(self, msg: InboundMessage, reply: ReplyTarget, text: str,
                             has_quote: bool, has_voice: bool):
        user_id = msg.user_id
        channel_type = msg.channel_type
        channel_id = msg.channel_id

        # ===== 记忆预处理：话题切换检测 + 摘要压缩 =====
        if config.ENABLE_MEMORY:
            if config.ENABLE_TOPIC_CHECK and not msg.image_refs and text.strip():
                old_topic = self.memory.get_topic(channel_type, channel_id, user_id)
                if old_topic:
                    same = await is_same_topic(old_topic, text)
                    if not same:
                        print(f"[INFO] 检测到话题切换，压缩旧对话为摘要")
                        old_items = self.memory.overflow_items_force(channel_type, channel_id, user_id)
                        if old_items:
                            summary = await summarize_history(old_items)
                            if summary:
                                old_summary = self.memory.get_summary(channel_type, channel_id, user_id)
                                merged = (old_summary + " " + summary).strip() if old_summary else summary
                                self.memory.set_summary(channel_type, channel_id, user_id, merged)
                        self.memory.clear_short_term(channel_type, channel_id, user_id)
                        # 话题切换时触发会话热切换
                        if hasattr(self.memory, 'hot_swap'):
                            await self.memory.hot_swap(user_id)

            if config.ENABLE_SUMMARY and self.memory.is_full(channel_type, channel_id, user_id):
                overflow = self.memory.overflow_items(channel_type, channel_id, user_id)
                if overflow:
                    summary = await summarize_history(overflow)
                    if summary:
                        old_summary = self.memory.get_summary(channel_type, channel_id, user_id)
                        merged = (old_summary + " " + summary).strip() if old_summary else summary
                        self.memory.set_summary(channel_type, channel_id, user_id, merged)
                        print(f"[INFO] 旧对话已压缩成摘要: {merged[:50]}...")
                
                # 会话热切换：当会话满时，后台预热新会话
                if hasattr(self.memory, 'prepare_next_session'):
                    await self.memory.prepare_next_session(user_id)

        messages = self.memory.get(channel_type, channel_id, user_id)
        use_reasoner = False

        # 每 bot 人格覆盖：若该 bot 在 AgentCore 上设置了 _persona_override，
        # 用它替换基座人设（config.SYSTEM_PROMPT），实现多 bot 人格隔离。
        persona_override = getattr(getattr(self, "core", None), "_persona_override", None)
        if persona_override:
            if messages and messages[0].get("role") == "system":
                messages[0]["content"] = persona_override
            else:
                messages.insert(0, {"role": "system", "content": persona_override})

        # 注入被引用的消息内容
        if (msg.quoted_text or msg.quoted_image_refs) and has_quote:
            quote_parts = []
            if msg.quoted_text:
                quote_parts.append(f"内容：{msg.quoted_text}")
            if msg.quoted_image_refs:
                quote_parts.append(f"（包含 {len(msg.quoted_image_refs)} 张图片）")
            if msg.quoted_sender:
                quote_parts.append(f"发送者：{msg.quoted_sender}")
            quote_hint = "用户引用了上一条消息，请针对这条被引用的消息回应：\n" + "\n".join(quote_parts)
            messages.append({"role": "system", "content": quote_hint})

        # 说话人标识（防串台降级）：优先真实称呼，其次 user_name，再次 channel 兜底，最后 "用户"
        # 既用于下方记忆注入的身份锚点，也用于待会儿给每条 user 消息打标签。
        try:
            from emotion import resolve_display_name
            speaker_label = (resolve_display_name(user_id) or user_name or user_id
                             or (f"{channel_type}对话对象" if channel_type else "对话对象")
                             or "用户")
        except Exception:
            speaker_label = user_name or user_id or "用户"

        # 注入完整记忆库
        try:
            from memory_context import build_memory_messages
            memory_msgs = build_memory_messages(
                user_id, include={"history": False, "speaker_label": speaker_label})
            messages.extend(memory_msgs)
        except Exception as e:
            print(f"[WARN] 记忆注入失败: {e}")

        # 身份绑定提示（该 QQ 在游戏世界的身份 / 待其同意的绑定请求）
        try:
            for h in identity.qq_hints(user_id):
                messages.append({"role": "system", "content": h})
        except Exception as e:
            print(f"[IDENTITY] 身份提示注入失败: {e}")

        # 主动历史检索
        if config.ENABLE_HISTORY_RETRIEVAL and text.strip() and not msg.image_refs:
            try:
                related = await retrieve_relevant_history(user_id, text.strip())
                if related:
                    messages.append({
                        "role": "system",
                        "content": f"【从历史对话中检索到的相关内容，可能有助于回答】\n{related}"
                    })
            except Exception as e:
                print(f"[WARN] 历史检索失败: {e}")

        # 优先级 0：推理模式（/思考 或 /r 开头）
        if text.strip().startswith("/思考") or text.strip().startswith("/r"):
            query = re.sub(r'^/?(思考|r)[:：\s]*', '', text.strip())
            if not query:
                await reply.reply("请告诉我你想让我深度思考的问题，例如：/思考 如何证明勾股定理")
                return
            await reply.reply("这题有点绕，让我认真想一会儿...")
            try:
                think_messages = self.memory.get(channel_type, channel_id, user_id)
                think_messages.append({"role": "user", "content": query})
                reply_text = await self.llm.chat(think_messages, capability="reasoning",
                                                 model=config.DEEPSEEK_REASONER_MODEL, think=True)
                if config.ENABLE_MEMORY:
                    self.memory.add(channel_type, channel_id, user_id, "user", query)
                    self.memory.add(channel_type, channel_id, user_id, "assistant", reply_text)
                await reply.reply(reply_text)
            except Exception as e:
                print(f"[ERROR] 推理模式失败: {e}")
                await reply.reply(f"深度思考失败：{e}")
            return

        # 优先级 1：指定链接解析（消息里带 URL）
        urls = extract_urls(text)
        if urls:
            await reply.reply("我去看看这个链接里写了啥，稍等哦~")
            try:
                page_text = await fetch_webpage(urls[0])
                page_text = page_text[:8000]
                user_content = f"请阅读以下网页内容并总结要点。\n\n[网页地址] {urls[0]}\n[用户问题] {text}\n\n[网页内容]\n{page_text}"
                messages.append({"role": "user", "content": user_content})
            except Exception as e:
                print(f"[ERROR] 网页解析失败: {e}")
                messages.append({"role": "user", "content": f"用户发了一个链接但抓取失败：{urls[0]}，错误：{e}"})
        # 优先级 2：实时搜索
        elif text.strip().startswith("/搜索") or text.strip().startswith("搜索"):
            query = re.sub(r'^/?搜索[:：\s]*', '', text.strip())
            if not query:
                await reply.reply("请告诉我你想搜索什么，例如：/搜索 今天北京天气")
                return
            await reply.reply("让我搜搜看，马上回来~")
            try:
                search_result = await search_web(query)
                await reply.reply(search_result)
                # 后台沉淀：把这次搜到的通用知识记进知识库
                knowledge_service.get_knowledge().learn_async(query, search_result)
            except Exception as e:
                print(f"[ERROR] 联网搜索失败: {e}")
                await reply.reply(f"搜索失败：{e}")
            if config.ENABLE_MEMORY:
                self.memory.add(channel_type, channel_id, user_id, "user", text)
            return
        # 优先级 2.5：视频 -> 抽帧 + 多帧理解
        elif msg.has_video:
            await reply.reply("我看到视频啦，让我看看里面发生了什么~")
            try:
                video_path = await reply.fetch_video(msg.video_ref)
                desc = await describe_video(video_path, text.strip())
                user_content = f"[视频描述] {desc}" + (f"\n[用户文字] {text.strip()}" if text.strip() else "")
                messages.append({"role": "user", "content": user_content})
                if config.ENABLE_MEMORY:
                    self.memory.add(channel_type, channel_id, user_id, "user", f"[用户发了视频] {text}".strip())
            except Exception as e:
                print(f"[ERROR] 视频识别失败: {e}")
                if "无法获取视频文件" in str(e):
                    await reply.reply(
                        "唔...我拿到视频文件失败了，可能是平台没有把视频缓存到本地，"
                        "或者视频链接过期了。你可以重新发一次试试~"
                    )
                else:
                    await reply.reply(f"视频识别失败了：{e}")
                return
        # 优先级 3：图片 -> 识别 + 收集表情包
        elif msg.image_refs:
            user_content = text.strip()
            for ref in msg.image_refs:
                try:
                    img_bytes = await reply.fetch_image(ref)
                    if detect_image_type(img_bytes) == "gif":
                        desc = await self.vision.describe_gif_animation(img_bytes)
                    else:
                        desc = await self.vision.describe_image(img_bytes, user_content)
                    user_content = f"[图片描述] {desc}" + (f"\n[用户文字] {text.strip()}" if text.strip() else "")
                    # 收集表情包：保存图片 + 识别情绪 + 详细描述
                    # （局部变量叫 emoji_emotion，避免遮蔽 emotion 情绪模块）
                    try:
                        emoji_emotion = await self.vision.recognize_emotion(img_bytes)
                        emoji_desc = ""
                        try:
                            emoji_desc = await self.vision.describe_emoji(img_bytes)
                        except Exception as e:
                            print(f"[WARN] 表情包描述识别失败: {e}")
                        ext_map = {"jpeg": ".jpg", "png": ".png", "gif": ".gif", "webp": ".webp"}
                        ext = ext_map.get(detect_image_type(img_bytes), ".jpg")
                        name = emoji_store.add_emoji(img_bytes, emoji_emotion, emoji_desc, ext)
                        print(f"[INFO] 表情包已收集: {name} (情绪: {emoji_emotion})")
                    except Exception as e:
                        print(f"[WARN] 表情包收集失败: {e}")
                except Exception as e:
                    print(f"[ERROR] 图片识别失败: {e}")
                    user_content = text.strip() if text.strip() else "[图片识别失败]"
            messages.append({"role": "user", "content": user_content})
        # 优先级 4：普通文字对话
        else:
            # 自然语言游戏意图识别（保留斜杠命令）
            try:
                from intent_router import detect_intent_extended, execute_intent, intent_to_reply, is_enabled
                if is_enabled() and text.strip():
                    intent_hit = await detect_intent_extended(text.strip())
                    if intent_hit:
                        intent_type, intent_params = intent_hit
                        result = await execute_intent(intent_type, intent_params)
                        reply_text = intent_to_reply(result)
                        await reply.reply(reply_text)
                        if config.ENABLE_MEMORY:
                            self.memory.add(channel_type, channel_id, user_id, "user", text.strip())
                            self.memory.add(channel_type, channel_id, user_id, "assistant", reply_text)
                        return
            except Exception as e:
                print(f"[WARN] 意图识别异常: {e}")

            # 用户只引用了消息但没写文字时，直接用引用内容作为发言
            effective_text = text.strip()
            if not effective_text and (msg.quoted_text or has_quote):
                q = (msg.quoted_text or "").strip()
                if q:
                    effective_text = f"[用户引用了消息: {q}] 请回应这条消息"
                else:
                    effective_text = "[用户引用了上一条消息，但没有附文字] 请回应"

            # 知识库召回：以前搜索学到过的知识直接注入使用，省一次联网搜索。
            # 用户明确要「最新/现在/今天」等信息时不拦截，照常走联网判断
            kb_context = ""
            if getattr(config, "ENABLE_KNOWLEDGE_RECALL", False) and effective_text:
                force_fresh = bool(re.search(r"最新|现在|今天|最近|目前|当前|latest", effective_text, re.I))
                if not force_fresh:
                    kb_context = knowledge_service.get_knowledge().recall(effective_text)
                    if kb_context:
                        messages.append({"role": "system", "content": kb_context})
                        print("[KB] 知识库命中，跳过联网搜索")

            # 自动联网判断（知识库已命中时不重复联网）
            need_search = False
            if config.AUTO_WEB_SEARCH and effective_text and not kb_context:
                # 关键词预判：仅疑似实时话题才跑 LLM judge + 联网，闲聊直接跳过（省 ~1.7s 前置开销）
                if _looks_realtime(effective_text):
                    need_search = await should_web_search(effective_text)
                else:
                    print("[WEB] 非实时话题，跳过联网 judge")
                if need_search:
                    await reply.reply("唔...这个我得去查查最新的，等我翻翻资料~")
                    try:
                        search_result = await search_web(effective_text)
                        await reply.reply(search_result)
                        # 后台沉淀：把这次搜到的通用知识记进知识库
                        knowledge_service.get_knowledge().learn_async(effective_text, search_result)
                        if config.ENABLE_MEMORY:
                            self.memory.add(channel_type, channel_id, user_id, "user", effective_text)
                        return
                    except Exception as e:
                        print(f"[ERROR] 自动联网搜索失败: {e}")
                        need_search = False

            # 自动推理判断
            use_reasoner = False
            if config.AUTO_REASONING and effective_text:
                use_reasoner = await should_reason(effective_text)

            # 注入实时时间
            try:
                from realtime import get_time_context
                messages.append({"role": "system", "content": f"【当前实时时间】{get_time_context()}"})
            except Exception as e:
                degrade("libs/qq_bot_runtime/chat_service.py:1511 ChatService._chat_pipeline", e, "降级：from realtime import get_time_context")

            messages.append({"role": "user", "content": effective_text or text})

        # 语言跟随
        lang = detect_language(text)
        if lang and lang != "zh":
            lang_names = {"en": "英文", "ja": "日文", "ko": "韩文", "fr": "法文", "de": "德文"}
            lang_cn = lang_names.get(lang, lang)
            messages.append({"role": "system", "content": f"【重要】用户本次使用{lang_cn}交流，请务必用{lang_cn}回复，不要用中文。"})

        # 注入表情包清单
        emoji_hint = emoji_store.build_emoji_hint()
        if emoji_hint:
            messages.append({"role": "system", "content": emoji_hint})

        # 注入实时画面描述
        if config.ENABLE_LIVE_VISION and vision_capture is not None and vision_capture.is_running():
            scene_hint = await vision_capture.get_scene_hint()
            if scene_hint:
                messages.append({"role": "system", "content": scene_hint})

        # 注入 Minecraft 游戏事件（非视觉）；MC 插件未安装时整体跳过
        if (mc_watcher is not None and getattr(config, "ENABLE_MC_WATCH", False)
                and await _mcw.is_running()):
            mc_hint = await self._build_mc_hint()
            if mc_hint:
                messages.append({"role": "system", "content": mc_hint})

        # 直播场景（voice_only 平台，如 B 站）：注入 MC 大脑实时状态，
        # 让她回弹幕/主动闲聊时把游戏里正在做的事自然融合进去
        try:
            if (getattr(reply, "capabilities", {}) or {}).get("voice_only"):
                s_hint = await self._build_stream_mc_hint()
                if s_hint:
                    messages.append({"role": "system", "content": s_hint})
                    print("[MC-STREAM] 已注入直播 MC 状态提示")
        except Exception as e:
            print(f"[MC-STREAM] 注入失败: {e}")

        # 注入纯净世界试验场 bot 实时动态（主人 或 已绑定 mc 名的 QQ：私聊或群里本人）
        try:
            _uid = str(getattr(msg, "user_id", ""))
            if (getattr(config, "ENABLE_MC_LIVE_HINT", True)
                    and (_uid == str(getattr(config, "PROACTIVE_PRIVATE_USER_ID", ""))
                         or identity.has_bound_mc(_uid))):
                from mc_live import build_live_hint
                live_hint = build_live_hint(effective_text or text)
                if live_hint:
                    messages.append({"role": "system", "content": live_hint})
        except Exception as e:
            print(f"[MC-LIVE] 注入失败: {e}")

        # 模组感知独立于游戏是否在线：游戏没开时也能靠 mods 目录扫描回答"装了什么模组"
        if mc_mods and not any("已加载模组" in m.get("content", "") for m in messages):
            try:
                mods_text = mc_mods.get_mods_text(max_mods=40)
                if mods_text:
                    messages.append({
                        "role": "system",
                        "content": "【Minecraft 已加载模组】（玩家问装了什么模组/某物品哪来的时参考；"
                                   "物品 id 前缀=模组 id，minecraft 开头是原版）\n" + mods_text,
                    })
            except Exception as e:
                print(f"[MC-MODS] 注入失败: {e}")

        # 注入心情状态（emotion.py：此刻的心情 + 该情绪下的语气指引，让回复口吻一致）
        if getattr(config, "EMOTION_ENABLED", False) and user_id:
            try:
                mood_hint = emotion.build_mood_hint(user_id)
                if mood_hint:
                    messages.append({"role": "system", "content": mood_hint})
            except Exception as e:
                print(f"[EMOTION] 心情提示注入失败: {e}")

        # 防串台：给每条 user 消息打上「说话人」标签。
        # 上下文虽按 (channel, channel_id, user_id) 隔离，但当 user_id 为空/被多会话共用、
        # 或旧会话残留混入时，模型无法从单条 role:user 消息判断是谁在说话，从而串台。
        # 每次请求都把当前说话人标识注入到每条 user 消息，确保身份始终明确。
        # 注意：这里构建新的列表，不改动 self.memory 中已持久化的历史 dict。
        try:
            _spk = speaker_label  # 复用上方构造的说话人标识（已含降级兜底）
            _labeled = []
            for _m in messages:
                if _m.get("role") == "user":
                    _c = _m.get("content", "")
                    if not isinstance(_c, str):
                        _c = str(_c)
                    _labeled.append({"role": "user", "content": f"[{_spk}] {_c}"})
                else:
                    _labeled.append(_m)
            messages = _labeled
        except Exception as e:
            print(f"[WARN] 说话人标签注入失败（不影响主流程）: {e}")

        # 调用模型（统一供应商：推理走 reasoning，否则 chat）
        # 若当前 bot 在 AgentCore 上设置了 _model_override，则覆盖主聊天模型（不改变推理模型）
        _model_override = getattr(getattr(self, "core", None), "_model_override", None)
        reply_text = await self.llm.chat(
            messages,
            capability="reasoning" if use_reasoner else "chat",
            model=config.DEEPSEEK_REASONER_MODEL if use_reasoner else (_model_override or None),
        )

        # 记录对话（供多轮记忆）
        if config.ENABLE_MEMORY:
            user_content = text if text.strip() else "[图片]"
            self.memory.add(channel_type, channel_id, user_id, "user", user_content)
            self.memory.add(channel_type, channel_id, user_id, "assistant", reply_text)
            if not msg.image_refs and text.strip():
                self.memory.set_topic(channel_type, channel_id, user_id, text.strip()[:100])

            # 全文历史记录（带时间戳）
            long_term_memory.append_history(user_id, "user", user_content, channel_type)
            long_term_memory.append_history(user_id, "assistant", reply_text, channel_type)

            # 记忆提取：一次 AI 调用，同时提取人物档案事实 + 重要信息（+ 情绪模块感知 AI 心情）
            if text.strip() and (config.ENABLE_PROFILE or config.ENABLE_AUTO_IMPORTANT_NOTES):
                try:
                    memory_result = await extract_memory(
                        text, reply_text,
                        include_mood=bool(getattr(config, "EMOTION_ENABLED", False)),
                        user_id=user_id)
                    if config.ENABLE_PROFILE and memory_result.get("facts"):
                        merged = await merge_profile_facts(user_id, memory_result["facts"])
                        long_term_memory.replace_profile(user_id, merged)
                    if config.ENABLE_AUTO_IMPORTANT_NOTES and memory_result.get("notes"):
                        for note in memory_result["notes"]:
                            added = important_notes.add_note(user_id, note["text"], note.get("category", ""))
                            if added:
                                print(f"[INFO] AI 自动记住重要信息: [{note.get('category','')}] {note['text'][:40]}")
                    # 情绪感知：写入心情档案，影响下一轮回复的语气（/心情 可查看）
                    if memory_result.get("mood"):
                        try:
                            emotion.apply_mood(user_id, memory_result["mood"])
                        except Exception as e:
                            print(f"[WARN] 情绪感知写入失败: {e}")
                except Exception as e:
                    print(f"[WARN] 记忆提取失败: {e}")

            # 反思记忆：定期触发对话反思，提炼交互规则
            if getattr(config, "ENABLE_REFLECTION", True):
                try:
                    import reflection_memory
                    reflection_memory.increment_conversation_count(user_id)
                    # 检查是否达到反思触发条件
                    if reflection_memory.should_reflect_now():
                        # 获取最近对话用于反思
                        recent_history = long_term_memory.get_user_history(user_id, limit=20)
                        if recent_history:
                            recent_messages = [
                                {"role": "user" if h.get("role") == "user" else "assistant",
                                 "content": h.get("content", "")}
                                for h in recent_history
                            ]
                            # 异步触发反思（不阻塞当前回复）
                            async def _reflect_and_extract_persona():
                                reflections = await reflection_memory.reflect_on_conversation(user_id, recent_messages)
                                # 从反思中提取人格相关规则
                                if reflections and getattr(config, "ENABLE_PERSONA", True):
                                    try:
                                        import persona_memory
                                        await persona_memory.extract_persona_from_reflection(user_id, reflections)
                                    except Exception as e:
                                        print(f"[PERSONA] 人格提取失败: {e}")
                            asyncio.create_task(_reflect_and_extract_persona())
                            reflection_memory.mark_reflection_done()
                except Exception as e:
                    print(f"[REFLECT] 反思触发失败: {e}")

            # 向量记忆索引（如果启用）
            if getattr(config, "ENABLE_VECTOR_MEMORY", False):
                try:
                    # 为最近的对话建立向量索引
                    recent_history = long_term_memory.get_user_history(user_id, limit=10)
                    if recent_history:
                        texts = [h.get("content", "") for h in recent_history if h.get("content")]
                        asyncio.create_task(vector_memory.index_history(user_id, texts))
                except Exception as e:
                    print(f"[VECTOR] 向量索引失败: {e}")

        # 语音回复触发条件
        # 平台能力降级：capabilities.voice=False 的平台（如 B 站弹幕）直接走文字，
        # 跳过语音意图判断（省一次 LLM 调用）与语音合成；
        # capabilities.voice_only=True 的平台（如 B 站直播）只有语音通道，恒走语音。
        _cap = getattr(reply, "capabilities", None) or {}
        platform_voice_ok = bool(_cap.get("voice", True))
        voice_only = bool(_cap.get("voice_only", False))
        want_voice = has_voice or voice_only
        if config.ENABLE_VOICE and platform_voice_ok and not want_voice:
            if re.match(r'^/?(语音|voice)\b', text.strip()):
                want_voice = True
            elif not text.strip().startswith(("/", "搜索", "思考")):
                want_voice = await wants_voice_reply(text)

        if config.ENABLE_VOICE and platform_voice_ok and want_voice:
            voice_text = clean_voice_text(reply_text)
            if voice_text:
                # 字幕挂钩：直播模型（voice_only）说出口之前先把文字推给字幕服务
                try:
                    cap = getattr(reply, "caption", None)
                    if cap:
                        cap(reply_text)
                except Exception as e:
                    print(f"[WARN] 字幕推送失败: {e}")
                chunks = split_voice_text(voice_text, config.VOICE_TTS_MAX_CHARS)
                sent = 0
                try:
                    for i, chunk in enumerate(chunks):
                        wav_path = await self._text_to_voice(chunk)
                        await reply.reply_voice(wav_path)
                        sent = i + 1
                        if sent < len(chunks):
                            await asyncio.sleep(config.SEND_INTERVAL_SECONDS)
                    return
                except Exception as e:
                    print(f"[ERROR] 语音回复失败: {e}")
                    if sent == 0:
                        await reply.reply(reply_text)  # 一句都没说成，整体退回文字
                        return
                    rest = "".join(chunks[sent:]).strip()  # 说了一半：剩下的话改文字补发
                    if rest:
                        await reply.reply(rest)
                    return
            await reply.reply(reply_text)
        else:
            await reply.reply(reply_text)

    # ==================================================================
    # 触发判断
    # ==================================================================
    def should_reply(self, msg: InboundMessage) -> bool:
        """判断是否应回复该消息。"""
        if msg.channel_type == "private":
            return True
        if config.ONLY_MENTION_OR_PRIVATE:
            if msg.mentioned:
                return True
            if getattr(config, "ENABLE_PROACTIVE_SPEAKER", False) and getattr(config, "PROACTIVE_GROUP_REPLY", False):
                if _is_related_topic(msg.text):
                    return True
            return False
        return True

    # ==================================================================
    # 语音：ASR 与 TTS（编解码由平台插件负责，这里只调云端接口）
    # ==================================================================
    async def _transcribe_voice(self, wav_bytes: bytes) -> str:
        """语音转文字：先自动检测语言，失败再按日语重试一次。"""
        import voice_client
        text = ""
        try:
            text = await voice_client.speech_to_text(wav_bytes, "voice.wav")
        except Exception as e:
            print(f"[INFO] ASR 自动识别失败: {e}")
        if not text:
            try:
                text = await voice_client.speech_to_text(wav_bytes, "voice.wav", language="ja")
                if text:
                    print(f"[INFO] 日语指定识别成功: {text}")
            except Exception as e:
                print(f"[INFO] 日语重试失败: {e}")
        return text.strip()

    async def _text_to_voice(self, text: str) -> str:
        """文字转语音，生成 wav 文件，返回路径。"""
        return await text_to_voice_wav(text)

    # ==================================================================
    # Minecraft 实时事件注入
    # ==================================================================
    # ==================================================================
    # 直播 MC 状态注入：她玩 MC 时回复/闲聊都带上"自己正在做的事"
    # ==================================================================
    async def _build_stream_mc_hint(self) -> str:
        """把 mc 大脑（模组世界 mc_mod / 原版世界 mc_bot，谁在跑用谁）的
        实时状态压成一段第一人称提示：她回弹幕/主动闲聊时自然结合游戏内容说。"""
        try:
            from agent_core import get_core
            core = get_core()
        except Exception:
            return ""
        parts = []
        # 原版世界 bot（mc_bot：mineflayer 桥，状态在 mc_bot_live.json）
        try:
            st = core.brains.get("mc_bot").status()
            if st.get("running"):
                pos = st.get("pos") or "?"
                lines = [f"【你现在正在玩 Minecraft 原版世界】你是第一人称：位置 {pos}，"
                         f"血量 {st.get('health')}/20，饱食度 {st.get('food')}/20，"
                         f"附近玩家 {st.get('players', 0)} 人。"]
                action = str(st.get("action") or "")[:60]
                if action:
                    lines.append(f"你刚才在做：{action}")
                parts.append("".join(lines))
        except Exception as e:
            degrade("libs/qq_bot_runtime/chat_service.py:1805 ChatService._build_stream_mc_hint", e, "降级：st = core.brains.get('mc_bot').status()")
        # 模组世界自主大脑（mc_mod）
        try:
            brain = core.brains.get("mc_mod")
            st = brain.status()
            if st.get("running"):
                lines = [f"【模组世界大脑】你自主在玩模组版 Minecraft，当前目标："
                         f"{st.get('current_goal') or st.get('long_goal') or '自由活动'}。"]
                acts = st.get("last_actions") or []
                if acts:
                    lines.append("你最近的动作：" + "；".join(str(a)[:40] for a in acts[:3]))
                parts.append("".join(lines))
        except Exception as e:
            degrade("libs/qq_bot_runtime/chat_service.py:1818 ChatService._build_stream_mc_hint", e, "降级：brain = core.brains.get('mc_mod')")
        if not parts:
            return ""
        return ("\n".join(parts)
                + "\n【要求】用第一人称、自然口语把正在做的事或想法融入回应，比如："
                  "我正蹲在矿洞里挖石头呢、这个洞好像有铁矿石。可以套在回应弹幕的句子里"
                  "说出来，不要逐条念状态、不要报坐标数字。")

    async def _build_mc_hint(self) -> str:
        """构建 Minecraft 实时事件提示，让 AI 不看画面也能理解游戏。"""
        try:
            parts = []
            state = await _mcw.fetch_state()
            state_text = await _mcw.format_state_text(state)
            if state_text:
                parts.append("【Minecraft 玩家/世界实时状态】（来自 FeiyuAPI mod 的本地接口）\n" + state_text)
            env_text = await _mcw.format_environment_text(state)
            if env_text:
                parts.append("【Minecraft 附近环境】（渲染区块内感知）\n" + env_text)
            recent = await _mcw.get_recent(limit=getattr(config, "MINECRAFT_RECENT_MAX", 50))
            if recent:
                lines = "\n".join(f"- {x}" for x in recent)
                parts.append("【Minecraft 最近发生的游戏事件】（来自日志监听）\n" + lines)
            if not parts:
                return ""
            return "【Minecraft 实时动态，非画面识别】以下是游戏世界真实状态，回答时可参考：\n" + "\n\n".join(parts)
        except Exception as e:
            print(f"[MC-HINT] 构建提示失败: {e}")
            return ""


# ============================================================================
# 模块级工具函数（平台无关的判断/提取，供 ChatService 与其它模块复用）
# ============================================================================

def _is_related_topic(text: str) -> bool:
    """判断群聊消息是否与肥鱼娘相关（游戏/自己/被喊名字等），相关则主动接话。"""
    if not text or not text.strip():
        return False
    t = text.strip()
    self_names = ["肥鱼娘", "小鱼", "鱼娘", "大肥鱼", "肥鱼", "bot", "机器人"]
    if any(name in t for name in self_names):
        return True
    game_kw = ["minecraft", "mc", "我的世界", "游戏", "生存", "挖矿", "钻石", "村民",
               "僵尸", "末影", "下界", "末地", "红石", "刷怪", "开黑", "一起玩"]
    if any(k in t for k in game_kw):
        return True
    return False


def detect_language(text: str) -> str:
    """检测文本语言，返回语言代码：zh/en/ja/ko/等。"""
    if not text or not text.strip():
        return "zh"
    text = text.strip()
    has_cjk = has_kana = has_hangul = has_latin = False
    for ch in text:
        code = ord(ch)
        if 0x4E00 <= code <= 0x9FFF:
            has_cjk = True
        elif 0x3040 <= code <= 0x30FF:
            has_kana = True
        elif 0xAC00 <= code <= 0xD7AF:
            has_hangul = True
        elif ch.isascii() and ch.isalpha():
            has_latin = True
    if has_kana:
        return "ja"
    if has_hangul:
        return "ko"
    if has_cjk and not has_latin:
        return "zh"
    if has_latin and not has_cjk:
        return "en"
    return "zh"


def clean_voice_text(text: str) -> str:
    """清理语音文本：去掉表情代码、动作描写、颜文字、停顿标点等不适合朗读的内容。"""
    text = re.sub(r'\[[^\]]*\]', '', text)
    text = re.sub(r'（[^）]*）', '', text)
    text = re.sub(r'\([^)]*\)', '', text)
    text = re.sub(r'[｡•́︿•̀｡(｡ìωí)(≧∇≦)ﾉ(๑•̀ㅂ•́)و✧(๑•́ ₃ •̀๑)(｡•́︿•̀｡)]+', '', text)
    text = re.sub(r'[，,、；;：:…—\-～~]', '', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


def split_voice_text(text: str, max_chars: int = 140) -> list:
    """把要朗读的文本切成不超过 max_chars 字的片段（优先在句末标点处切）。

    QQ 语音消息有长度上限，超长回复拆成多条语音逐条发送。
    """
    text = text.strip()
    if len(text) <= max_chars:
        return [text]
    parts, buf = [], ""
    pieces = [p for p in re.split(r"(?<=[。！？!?；;\n])", text) if p]
    for piece in pieces:
        if len(piece) > max_chars:
            if buf:
                parts.append(buf)
                buf = ""
            # 单段本身超长（没有句末标点可切）：按字硬切
            for i in range(0, len(piece), max_chars):
                parts.append(piece[i:i + max_chars])
            continue
        if len(buf) + len(piece) > max_chars:
            parts.append(buf)
            buf = piece
        else:
            buf += piece
    if buf:
        parts.append(buf)
    return parts


def _vox_suspended() -> bool:
    """直播中挂起本地 VoxCPM：显存被游戏/直播采集占满时它必 OOM，直接走 GLM 云端，
    下播自动恢复。查不到直播插件状态时一律返回 False（不影响原有引擎选择）。"""
    try:
        from agent_core import get_core
        p = get_core().plugins.get("bilibili")
        s = p.streamer if p else None
        return bool(s and getattr(s, "pushing", False))
    except Exception:
        return False


async def text_to_voice_wav(text: str) -> str:
    """把一段文字合成为语音 wav 文件并返回路径。

    供语音回复与「主动语音发送」复用（主动说话模块用法：
    wav = await chat_service.text_to_voice_wav(text) 后经
    sender.send_voice_private(...) / send_voice_group(...) 发送）。
    引擎按 config.VOICE_TTS_ENGINE 分发：voxcpm（本地 VoxCPM2，失败自动回退 GLM）或 glm。
    直播期间自动挂起 VoxCPM：显存被游戏/采集占满时它必 OOM（CUDA out of memory），
    直接走 GLM 云端，省一次失败往返；下播自动恢复本地合成。
    """
    if getattr(config, "VOICE_TTS_ENGINE", "glm") == "voxcpm" and not _vox_suspended():
        try:
            from tts_vox import voxcpm_synthesize
            return await voxcpm_synthesize(text)
        except Exception as e:
            print(f"[WARN] VoxCPM 本地合成失败，回退 GLM 云端: {e}")
    import voice_client
    pcm = await voice_client.text_to_speech(text)
    base_dir = os.path.dirname(os.path.abspath(__file__))
    voice_dir = os.path.join(base_dir, config.VOICE_DIR)
    os.makedirs(voice_dir, exist_ok=True)
    wav_path = os.path.join(voice_dir, f"voice_{int(time.time() * 1000)}.wav")
    if not voice_client.pcm_to_wav(pcm, wav_path):
        raise RuntimeError("PCM 转 WAV 失败（可能未安装 ffmpeg）")
    return wav_path


# 疑似"需联网的实时话题"信号词：命中才跑 LLM judge，避免闲聊每次被前置 judge（~1.7s）拖慢。
_REALTIME_HINTS = re.compile(
    r"(天气|气温|下雨|下雪|温度|新闻|头条|热点|时事|直播|最新|最近|今天|现在|当前|"
    r"刚刚|刚才|汇率|股价|金价|油价|币价|比特币|比赛|比分|球赛|世界杯|奥运|亚运|"
    r"放假|节假日|休市|日期|几号|星期几|周几|哪天|什么时候|疫情|确诊|政策|新规|"
    r"涨价|降价|上市|官宣|公告|版本|更新|排名|榜单|票房|利率|通胀|gdp|GDP|"
    r"赛季|夺冠|夺冠|冠军|决赛|发布会|声明|快讯|通报)"
)


def _looks_realtime(text: str) -> bool:
    """本地快速预判：文本是否疑似需要联网查实时信息。命中才进一步跑 LLM judge。"""
    return bool(_REALTIME_HINTS.search(text))


async def should_web_search(text: str) -> bool:
    """让 DeepSeek 判断该问题是否需要联网搜索实时信息。"""
    judge_prompt = (
        "你是一个判断助手。请判断用户的问题是否需要联网搜索才能准确回答。\n"
        "【优先联网】凡是涉及以下情况，都回答「需要」：\n"
        "1. 实时信息：天气、新闻、股价汇率、时间、日期、最新事件、热点话题；\n"
        "2. 时效性内容：某产品最新版本、某人最新动态、最新政策、今年/最近发生的事；\n"
        "3. 事实性知识但你不确定或可能过时的：具体数据、排名、记录、名单、具体日期；\n"
        "4. 涉及现实世界的人物、公司、地点、事件的任何具体查询。\n"
        "【无需联网】仅以下情况回答「不需要」：纯常识、数学计算、编程逻辑、翻译、写作润色、"
        "纯观点讨论、闲聊、情感交流、脑筋急转弯、明确不涉及现实时效的抽象问题。\n"
        "当你不确定时，请回答「需要」（宁可信其需要联网）。\n"
        "请只回答一个词：需要 或 不需要。\n\n"
        f"用户问题：{text}"
    )
    messages = [{"role": "user", "content": judge_prompt}]
    try:
        result = await get_llm().chat(messages, capability="chat", role="judge")
        result = result.strip()
        return "需要" in result and "不需要" not in result
    except Exception as e:
        print(f"[WARN] 联网判断失败，默认联网: {e}")
        return True


async def should_reason(text: str) -> bool:
    """让 DeepSeek 判断该问题是否需要深度推理。"""
    judge_prompt = (
        "你是一个判断助手。请判断用户的问题是否需要深度推理（用推理模型）才能回答好。\n"
        "【优先推理】凡是用户提出「问题」、需要你「思考后给出答案」的，都回答「需要」，包括：\n"
        "1. 数学计算、证明、推导、逻辑推理题、脑筋急转弯；\n"
        "2. 算法设计、代码调试、复杂度分析、技术原理讲解；\n"
        "3. 需要分析、比较、权衡、判断的开放性问题；\n"
        "4. 「为什么」「怎么办」「如何」「是什么原因」等需要解释的提问；\n"
        "5. 任何需要多步思考才能回答清楚的问题。\n"
        "【无需推理】仅以下情况回答「不需要」：纯闲聊、简单寒暄、单纯表达情感、"
        "直接陈述事实而不要求分析、简单打招呼、简单的翻译或复述。\n"
        "当你不确定时，请回答「需要」（宁可信其需要推理）。\n"
        "请只回答一个词：需要 或 不需要。\n\n"
        f"用户问题：{text}"
    )
    messages = [{"role": "user", "content": judge_prompt}]
    try:
        result = await get_llm().chat(messages, capability="chat", role="judge")
        result = result.strip()
        return "需要" in result and "不需要" not in result
    except Exception as e:
        print(f"[WARN] 推理判断失败，默认推理: {e}")
        return True


async def wants_voice_reply(text: str) -> bool:
    """判断用户是否希望用语音回复。"""
    judge_prompt = (
        "你是一个判断助手。请判断用户是否希望 AI 用「语音」方式回复（而不是文字）。\n"
        "表达想要语音交流的典型说法包括：和我说话、用语音和我聊天、说给我听、"
        "用语音回答我、语音交流、你说话呀 等。\n"
        "如果用户只是普通提问或聊天，没有明确要语音，就回答：不需要。\n"
        "请只回答一个词：需要 或 不需要。\n\n"
        f"用户消息：{text}"
    )
    messages = [{"role": "user", "content": judge_prompt}]
    try:
        result = await get_llm().chat(messages, capability="chat", role="judge")
        result = result.strip()
        return "需要" in result and "不需要" not in result
    except Exception as e:
        print(f"[WARN] 语音意图判断失败，默认不用语音: {e}")
        return False


async def extract_memory(user_text: str, reply: str, include_mood: bool = False, user_id: str = "") -> dict:
    """一次 AI 调用，同时提取「人物档案事实」「重要信息」，可选再加「AI 此刻的心情」。

    include_mood=True 时（情绪模块 emotion.py），让模型顺带分析用户这句话让 AI
    产生了什么情绪——不新增 LLM 调用，解析逻辑在 parse_memory_output()。
    user_id：当前对话用户的 ID，仅用于提示模型「正在为谁整理档案」，不参与事实内容。
    """
    subject_tip = (f"当前正在为「用户 {user_id}」整理长期记忆；下面所有「用户」都指这一位。\n\n"
                   if user_id else "")
    prompt = (
        "请分析下面的对话，同时提取两类信息，用于长期记忆。\n\n"
        + subject_tip +
        "主体说明（先读三遍，最关键）：本段对话里有两方——「用户」（人类，即下方『用户说』那一方）"
        "和「AI」（肥鱼娘，即下方『AI回复』那一方），二者绝不是同一人。\n\n"
        "第一类【人物档案】：只记录「用户」本人的长期稳定事实，如姓名/称呼、职业、兴趣爱好、"
        "喜欢的食物、居住城市、生日、宠物、性格等。\n"
        "★ 人物档案绝对不能写 AI（肥鱼娘）的任何特征或喜好；如果用户是在描述 AI"
        "（例如『你真可爱』『你总是很耐心』『你和别的 AI 不一样』），那是关于 AI 的，"
        "一律不要写进人物档案（该区块写：无）。\n"
        "★ 每条必须以「用户」为主语（如『用户喜欢猫』『用户叫小明』）；若一条事实分不清"
        "属于用户还是 AI，宁可丢弃，不要猜。\n"
        "第二类【重要信息】：「用户」值得长期记住的事，如明确要求记住的内容、重要日期/约定/待办、"
        "关键信息（手机号/地址）、重要偏好/决定/长期计划。\n\n"
        "输出格式（严格按下面格式）：\n"
        "【人物档案】\n"
        "用户叫小明\n"
        "用户喜欢猫\n"
        "【重要信息】\n"
        "生日|我的生日是8月15日\n"
        "约定|明天晚上开会\n\n"
        "注意：\n"
        "- 没有的内容区块就写【人物档案】或【重要信息】后紧跟：无\n"
        "- 重要信息每行用「分类|内容」格式，分类可以是生日、约定、偏好、待办、信息等\n"
        "- 不要保存临时闲聊、情绪表达、一次性话题、普通问候\n"
        "- 再次强调：人物档案只关于『用户』，绝不关于『AI（肥鱼娘）』\n\n"
    )
    if include_mood:
        prompt += (
            "第三类【心情】：分析用户这句话让 AI（肥鱼娘，傲娇萌娘人设）产生了什么情绪"
            "变化，供情绪模块用。\n"
            "情绪标签只能从以下词里选一个：" + "、".join(emotion.MOOD_LABELS) + "。\n"
            "只在有明显情绪起伏时写：被夸→开心/得意、被骂/被凶→生气/委屈、被关心→被暖到、"
            "被冷落/被敷衍→低落、被反复使唤→烦躁、被逗乐→被逗乐 等；普通闲聊、纯提问一律写：无\n"
            "输出格式一行：心情|标签|强度(1-3)|一句话原因。原因必须是 AI 第一人称的"
            "一句心里话，25 字以内，直接说发生了什么、自己什么感觉；禁止写成"
            "「用户说…让鱼娘觉得…」这种第三人称旁观描述，也不要出现“用户”“AI”“鱼娘”字样。\n"
            "例如：心情|开心|2|被夸是最聪明的鱼 / 心情|得意|2|他说好好好都听我的\n"
            "没有情绪变化时输出：\n【心情】\n无\n\n"
        )
    prompt += f"用户说：{user_text}\nAI回复：{reply}"
    messages = [{"role": "user", "content": prompt}]
    try:
        ai_output = await get_llm().chat(messages, capability="chat", role="memory_extract")
        if isinstance(ai_output, dict):
            ai_output = ai_output.get("content", "") or ai_output.get("text", "") or ""
        return parse_memory_output(str(ai_output).strip(), include_mood=include_mood)
    except Exception as e:
        print(f"[WARN] 记忆提取失败: {e}")
        return {"facts": [], "notes": [], "mood": None}


def parse_memory_output(ai_output: str, include_mood: bool = False) -> dict:
    """解析记忆提取的模型输出（纯文本处理，便于离线测试）。

    输出格式（模型遵循的约定）：
        【人物档案】  每行一条用户事实
        【重要信息】  每行「分类|内容」
        【心情】      一行「心情|标签|强度(1-3)|原因」（include_mood 时）
    返回 {"facts": [...], "notes": [...], "mood": None|{"emotion","strength","why"}}
    """
    result = {"facts": [], "notes": [], "mood": None}
    if not ai_output or ai_output == "无":
        return result
    section = None
    for line in ai_output.split("\n"):
        line = line.strip()
        if not line:
            continue
        if "人物档案" in line and "【" in line:
            section = "facts"
            continue
        if "重要信息" in line and "【" in line:
            section = "notes"
            continue
        if include_mood and "心情" in line and "【" in line:
            section = "mood"
            continue
        if line == "无":
            section = None
            continue
        line_clean = line.lstrip("-•·0123456789.、 ")
        if not line_clean:
            continue
        if section == "facts":
            if len(line_clean) > 2:
                result["facts"].append(line_clean)
        elif section == "notes":
            if "|" in line_clean:
                cat, _, note_text = line_clean.partition("|")
                if note_text.strip():
                    result["notes"].append({"category": cat.strip(), "text": note_text.strip()})
            elif len(line_clean) > 1:
                result["notes"].append({"category": "", "text": line_clean})
        elif section == "mood":
            mood = _parse_mood_line(line_clean)
            if mood:
                result["mood"] = mood
    result["facts"] = result["facts"][:10]
    result["notes"] = result["notes"][:8]
    return result


def _parse_mood_line(line_clean: str) -> dict:
    """解析单行情绪输出「心情|标签|强度|原因」（容错：兼容漏掉强度/原因）。"""
    parts = [p.strip() for p in line_clean.split("|") if p.strip()]
    if not parts:
        return None
    # 兼容模型把前缀"心情/情绪"也写进一行的情况
    if parts[0].startswith(("心情", "情绪")):
        parts = parts[1:]
    if not parts:
        return None
    label = parts[0]
    if label in ("无", "平静") or label not in emotion.MOOD_LABELS:
        return None
    strength = 1
    if len(parts) >= 2:
        try:
            strength = max(1, min(int(parts[1]), 3))
        except ValueError as e:
            degrade("libs/qq_bot_runtime/chat_service.py:2191 _parse_mood_line", e, "降级：strength = max(1, min(int(parts[1]), 3))")
    why = parts[2] if len(parts) >= 3 else ""
    return {"emotion": label, "strength": strength, "why": why[:80]}


async def retrieve_relevant_history(user_id, query: str, limit: int = 50) -> str:
    """语义检索：从用户历史里找与问题相关的内容。
    
    优先使用向量检索（如果启用），失败时回退到 LLM 检索。
    """
    # 优先使用向量检索（如果启用）
    if getattr(config, "ENABLE_VECTOR_MEMORY", False):
        try:
            results = await vector_memory.hybrid_search(user_id, query, top_k=10)
            if results:
                return "\n".join([f"- {r}" for r in results])
        except Exception as e:
            print(f"[VECTOR] 向量检索失败，回退到 LLM 检索: {e}")
    
    # 回退到原有的 LLM 检索逻辑
    history = long_term_memory.get_user_history(user_id, limit=200)
    if not history:
        return ""
    recent = history[-limit:]
    lines = []
    for r in recent:
        role = "用户" if r.get("role") == "user" else "AI"
        lines.append(f"[{r.get('time', '')}] {role}: {r.get('content', '')[:100]}")
    history_text = "\n".join(lines)
    prompt = (
        "下面是用户和AI的历史对话记录。请找出与用户当前问题「相关」的历史内容，"
        "提取出来用于帮助AI回答。\n"
        "只提取真正相关的信息，无关的忽略。如果没有任何相关内容，请只回答：无。\n\n"
        f"用户当前问题：{query}\n\n"
        f"历史记录：\n{history_text}"
    )
    messages = [{"role": "user", "content": prompt}]
    try:
        result = await get_llm().chat(messages, capability="chat", role="retrieval")
        result = result.strip()
        if not result or result == "无":
            return ""
        return result
    except Exception as e:
        print(f"[WARN] 历史检索失败: {e}")
        return ""


async def merge_profile_facts(user_id, new_facts: list) -> list:
    """智能合并人物档案：新事实与旧事实去重、纠错。"""
    old_profile = long_term_memory.get_profile(user_id)
    old_facts = old_profile.get("facts", [])
    if not new_facts:
        return old_facts
    prompt = (
        "下面是用户的「旧档案」和「新提取的事实」。请合并它们：\n"
        "1. 去掉重复的信息（意思相同只保留一条，保留表述更清晰的那条）；\n"
        "2. 如果新事实与旧事实矛盾（如旧说「喜欢猫」，新说「喜欢狗」），用新的替换旧的；\n"
        "3. 保留所有不冲突的信息。\n\n"
        f"旧档案：{'；'.join(old_facts) if old_facts else '（空）'}\n"
        f"新事实：{'；'.join(new_facts)}\n\n"
        "请输出合并后的完整档案，每行一条事实。"
    )
    messages = [{"role": "user", "content": prompt}]
    try:
        result = await get_llm().chat(messages, capability="chat", role="memory_merge")
        result = result.strip()
        if not result or result == "无":
            return old_facts
        facts = []
        for line in result.split("\n"):
            line = line.strip().lstrip("-•·0123456789.、 ")
            if line and line != "无" and len(line) > 2:
                facts.append(line)
        return facts[:50]
    except Exception as e:
        print(f"[WARN] 档案合并失败: {e}")
        return old_facts + new_facts


async def summarize_history(history_items: list) -> str:
    """把一段历史对话压缩成摘要（用于摘要记忆）。"""
    if not history_items:
        return ""
    lines = []
    for item in history_items:
        role = "用户" if item.get("role") == "user" else "AI"
        lines.append(f"{role}: {item.get('content', '')[:200]}")
    dialog = "\n".join(lines)
    prompt = (
        "请把下面这段对话压缩成简洁的摘要，保留关键信息（用户问过什么、AI答了什么、"
        "达成了什么结论、用户提到的偏好或事实）。控制在 100 字以内。\n\n"
        f"对话内容：\n{dialog}"
    )
    messages = [{"role": "user", "content": prompt}]
    try:
        result = await get_llm().chat(messages, capability="chat", role="summary")
        return result.strip()
    except Exception as e:
        print(f"[WARN] 摘要生成失败: {e}")
        return ""


async def is_same_topic(old_topic: str, new_text: str) -> bool:
    """判断新消息是否延续旧话题。"""
    if not old_topic:
        return True
    prompt = (
        "你是一个话题判断助手。请判断下面两段话是否在聊同一个话题。\n"
        "【宽松原则】只要两者有一定关联（哪怕关联较弱），都回答「是」。"
        "只有在明显是毫不相关、完全不同的话题时才回答「否」。\n"
        "例如用户前一句说「给你加了视觉插件」，后一句说「还加了语音插件」，"
        "这是同一话题（都在聊给机器人加功能），应回答「是」。\n"
        f"上一段（旧话题）：{old_topic}\n"
        f"下一段（新消息）：{new_text}\n"
        "请只回答：是 或 否。"
    )
    messages = [{"role": "user", "content": prompt}]
    try:
        result = await get_llm().chat(messages, capability="chat", role="judge")
        result = result.strip()
        return "是" in result and "否" not in result
    except Exception as e:
        print(f"[WARN] 话题判断失败，默认视为同一话题: {e}")
        return True


# 按智能体分桶的单例：{ agent_id: ChatService }
_chat_services: dict = {}


def get_chat_service(agent_id: str = None) -> ChatService:
    aid = agent_id or agent_ctx.current_agent() or "feiyu"
    if aid not in _chat_services:
        _chat_services[aid] = ChatService(agent_id=aid)
    return _chat_services[aid]
