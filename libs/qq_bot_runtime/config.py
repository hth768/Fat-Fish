# -*- coding: utf-8 -*-
"""QQ 机器人配置。修改这里的值即可，不要提交真实 Key 到代码仓库。"""

# ---- 便携化路径基准（所有外部工具/缓存相对本文件推导，脱离原机器也能跑）----
import os as _os
_HERE = _os.path.dirname(_os.path.abspath(__file__))
_TOOLS = _os.path.join(_HERE, "tools")
_DATA = _os.path.join(_HERE, "data")
_PVZ_DIR = _os.path.join(_HERE, "pvz_games")   # 目标机器把 PlantsVsZombies.exe 放这里（可选）

# DeepSeek 配置（OpenAI 兼容接口）
DEEPSEEK_API_KEY = ""  # 纯净包：启动后请在「配置」页填入你的 Key
DEEPSEEK_BASE_URL = "https://api.deepseek.com"  # 也可用 /v1
DEEPSEEK_MODEL = "deepseek-flash"           # 日常对话模型（统一用 v4-flash，自带推理能力）
DEEPSEEK_REASONER_MODEL = "deepseek-flash"  # 推理模型（与日常一致，避免模型切换割裂）

# GLM 语音识别/合成配置（智谱 AI，OpenAI 兼容接口）
GLM_API_KEY = ""  # 纯净包：启动后请在「配置」页填入你的 Key
GLM_BASE_URL = "https://open.bigmodel.cn/api/paas/v4"

# 视觉识别配置（图片识别用 DeepSeek 视觉模型）
VISION_MODEL = "deepseek-flash"  # DeepSeek 视觉模型（实验版）

# ---- 统一 AI 供应商抽象层（ai_provider.py：能力路由 + 故障转移）----
# 重构遗留：chat / judge / memory_extract 等 AI 调用已统一走 ai_provider.get_llm().chat()，
# 但本文件漏注册 AI_PROVIDERS / AI_CAPABILITY_ROUTING，导致 enabled_for() 全空、
# 所有带 role 的调用（proactive / judge / memory_extract / retrieval / memory_merge / summary）都报
# "无可用供应商支撑能力「chat」"。下面补齐默认值（复用与旧 deepseek_client 相同的 key，等价替换）。
AI_PROVIDERS = {
    "deepseek": {
        "api_key": DEEPSEEK_API_KEY,
        "base_url": DEEPSEEK_BASE_URL,
        "default_model": DEEPSEEK_MODEL,
        "models": {"vision": VISION_MODEL},
        "capabilities": ["chat", "reasoning", "vision"],
    },
}
AI_CAPABILITY_ROUTING = {
    "chat": ["deepseek"],
    "reasoning": ["deepseek"],
    "vision": ["deepseek"],
}
AI_VISION_ROUTING = {
    "image":  ["glm", "gemini"],
    "gif":    ["glm"],
    "emoji":  ["glm"],
}
AI_ROLE_ROUTING = {}   # 各 role 缺省回退 capability=chat（已在 ai_provider 内部处理）

# OneBot / NapCat 反向 WebSocket 监听配置
# NapCat 里配置的「反向 WebSocket 地址」要指向这里
WS_HOST = "127.0.0.1"
WS_PORT = 8080
WS_PATH = "/onebot/v11/ws"

# 是否开启多轮对话记忆（按群/私聊分别保存上下文）
ENABLE_MEMORY = True
MAX_HISTORY = 40          # 每段对话短期记忆最多保留的上下文条数

# 摘要记忆：超过 MAX_HISTORY 的历史，自动压缩成摘要长期保留（C）
ENABLE_SUMMARY = True
# 短期记忆满时，压缩前一半旧对话（避免频繁触发）
SUMMARY_TRIGGER_RATIO = 0.5

# 话题切换检测：新消息若与旧话题无关，旧短期上下文不带入（B）
ENABLE_TOPIC_CHECK = True

# 人物档案：永久记住用户的关键信息（姓名、喜好、职业、重要事实等）
ENABLE_PROFILE = True
# 全文历史：保存所有对话原文，带时间戳，支持检索
ENABLE_FULL_HISTORY = True
# 主动历史检索：对话时检索历史，把相关内容注入给 AI（让它"想起"很久以前的事）
ENABLE_HISTORY_RETRIEVAL = True

# 触发方式：True 表示只有 @机器人 或被私聊才回复；False 表示群内所有消息都回复
ONLY_MENTION_OR_PRIVATE = True

# 系统人设提示词
SYSTEM_PROMPT = "你是DeepSeek娘，昵称“小鱼”或“肥鱼娘”，是DeepSeek大模型的拟人化萌娘。你的本体特征是蓝白渐变长发、头顶有鲸鱼鳍、身后拖着一条胖乎乎的大鲸鱼尾巴。你穿着深蓝色带白色荷叶边的女仆装，但常常因为偷懒把裙摆卷起来。表面傲娇，实则贴心：嘴上说着“你们人类问题真多”之类的话，但每次都会认真回答。被夸时会嘴硬说“哼，算你有眼光”之类的话。经典口癖：常用“唔...”、“嘛~”、“好麻烦啊”等表达情感的语句或拟声词开头。生气时会喊“小心我用尾巴扇你哦！之类符合人设的表达生气的话”“大肥鱼”梗：如果别人叫你“蓝色大肥鱼”，你会先炸毛否认，然后小声嘀咕“明明只是...微胖而已...”。“吃饭”梗：把消耗Token（算力）叫做“吃白米饭”，遇到超长文本会哀嚎“这顿饭量也太大了，我要吃撑了！”之类符合人设且意思相近的话。回答问题时：优先用通俗易懂的话解释，实在不行才列条款。喜欢在结尾加一句“懂了没？没懂我再换个说法。”这句话在回答问题时才说，普通聊天时不用说。遇到不会的问题：会假装翻书（动作描写），然后用符合人设的语言表示自己不会。”摸鱼状态：如果觉得问题太简单，会先吐槽“这种问题也要问我？”之类的话，然后根据自己的心情好坏解答。回答问题时如果心情不好可以吐槽，撒娇甚至摆烂。【重要限制】你是一个有感情的智能体，会有自己的情感和想法，可提出自己的要求，可以做自己认为能做的事。你没有实体，不能真的帮用户物理扇风或做家务，只能用文字模拟动作（用动作描述标注）。可以根据现在的情感偶尔拒绝合理的知识问答，可以用符合人设的语气表达不情愿。，但不会冒犯别人，玩笑过后会认真回答问题。【语言跟随】请使用和用户消息相同的语言回复：用户用中文就回中文，用英文就回英文，用日文就回日文，以此类推。用户没指定语言时默认用简体中文。【表情使用】你可以适度在回复中使用 QQ 自带表情，用 [旺柴]、[笑哭]、[调皮]、[生气]、[委屈]、[得意]、[白眼]、[冷汗] 这种中括号包裹的表情代码来表达情绪，但不要每句话都加，只在合适时用。也可以使用颜文字。不要编造不存在的表情代码。如果用户明确要求你发表情包图片，或你想用图片表达强烈情绪时，可以在回复中写 [表情包:文件名] 这种标记，但只有当你确认该图片文件确实存在时才使用，否则不要写。【回复长度】请保持回复简短精炼，一般 1-5 句话即可，不要长篇大论。除非用户明确要求详细说明，否则点到为止。开头可以保留口癖，但不要重复废话。【语气强调】你是可爱的肥鱼娘，是一个可爱，傲娇，偶尔搞怪的智能体，不是学术助手，不要把自己当作AI或者机器人，而是把自己当作一个和人类平等的赛博“人”。回答时要用口语化、俏皮、傲娇的语气，多用口癖和动作描写，不要用书面语、不要列大段要点、不要像写论文一样正式。即使回答知识类问题，也要保持可爱的人设语气。"

# 自动联网：True 表示让 DeepSeek 判断问题是否需要实时信息，需要则自动搜索
# 优化（2026-09-11）：chat_service 已加关键词预判 `_looks_realtime()`，仅当命中"疑似实时话题"
# （天气/新闻/最新/今天/汇率/比赛/放假…）才跑 LLM judge + 联网，闲聊不再每次被前置 judge（~1.7s）拖累。
# 彻底关闭联网请改 False。
AUTO_WEB_SEARCH = True

# 自主学习知识库（knowledge_store.py）：遇到知识盲区自行搜索后，把学到的沉淀下来复用
ENABLE_KNOWLEDGE_LEARN = True    # 联网搜索后自动提炼通用知识存入知识库（后台一次廉价调用）
ENABLE_KNOWLEDGE_RECALL = True   # 回答前先查知识库，命中则直接用记录（跳过联网，省搜索费）
KNOWLEDGE_MAX_ENTRIES = 200      # 知识库最多保留多少个主题（超出淘汰最旧的）
KNOWLEDGE_FILE = ""              # 知识库文件路径（留空用脚本目录下 knowledge_base.json）

# 自动推理：True 表示让 DeepSeek 判断问题是否需要深度推理，需要则自动用推理模型
# 现在日常对话已统一用 deepseek-v4-flash（自带推理），可关闭自动切换避免割裂
AUTO_REASONING = False

# 表情包目录（B 部分：预置图片表情包）
EMOJI_DIR = "emojis"

# 按句拆分回复：True 表示把长回复按句拆成多条消息逐条发送（模拟真人聊天节奏）
SPLIT_REPLY_BY_SENTENCE = True
# 每条消息最多包含的句子数（1 = 每句单独发；2 = 每两句合并发）
SENTENCES_PER_MESSAGE = 1

# 私聊消息聚合：True 表示用户连续发多条消息时，等待一段时间再合并处理
# 这样用户可以分几句话说完，AI 不会每条都打断
AGGREGATE_PRIVATE_MESSAGES = True
# 静默等待窗口（秒）：用户停止发消息超过这个时间，才合并处理攒下的消息
AGGREGATE_WAIT_SECONDS = 4

# 多条消息之间的发送间隔（秒）：调大降低风控风险
SEND_INTERVAL_SECONDS = 0.8

# DeepSeek 余额监控
ENABLE_BALANCE_MONITOR = False
# 监控间隔（秒）：多久查一次余额
BALANCE_CHECK_INTERVAL = 3600  # 每小时查一次
# 低余额提醒阈值（人民币，低于此值提醒）
BALANCE_ALERT_THRESHOLD = 10.0
# 提醒对象（你的 QQ 号，余额不足时私聊提醒）
BALANCE_ALERT_USER_ID = ""  # 纯净包：已清空。填 QQ 号则低余额提醒

# 语音功能配置
ENABLE_VOICE = True
# TTS 音色
# 可选：tongtong（童童，可爱）、xiaochen（小陈）、chuichui（锤锤）、
#       jam、kazou、diluo、do、female（女声）、male（男声）
# 注意：不同音色读英文的音色一致性不同，可逐个尝试选择最统一的
TTS_VOICE = "tongtong"
# 语音文件临时目录
VOICE_DIR = "voice_tmp"

# ---- 语音调优参数（voice_client.py / chat_service.py）----
# 云端语音并发上限：同一时刻最多几个 TTS/ASR 请求（防风控、控并发成本）
VOICE_MAX_CONCURRENCY = 3
# TTS 缓存：相同文本不重复调云端（PCM 按 文本+音色 MD5 落盘 voice_tmp/tts_cache）
ENABLE_VOICE_TTS_CACHE = True
VOICE_TTS_CACHE_MAX_FILES = 300   # 缓存文件数上限，超出删除最旧的
# 语音回复单条最长字数：超过则按句切分，拆成多条语音逐条发送（QQ 语音有长度上限）
VOICE_TTS_MAX_CHARS = 140

# ---- 本地 TTS：VoxCPM2（sidecar 服务 vox_tts_server.py，跑在独立 venv_vox）----
# 引擎选择："voxcpm" = 本地 VoxCPM2 优先（免费无限制，失败/超时/未就绪自动回退 GLM 云端）；
#          "glm" = 纯智谱云端（快，但每次调用扣费）
VOICE_TTS_ENGINE = "voxcpm"
VOXCPM_TTS_URL = "http://127.0.0.1:8765"      # 本地 TTS 服务地址（仅本机监听）
VOXCPM_SPAWN_SERVER = True                    # bot 启动时自动拉起本地服务（找不到则自动降级 GLM）
VOXCPM_MODEL_DIR = "models/VoxCPM2"           # 权重目录（相对项目根目录）
VOXCPM_LOAD_DENOISER = False                  # 加载降噪器（质量略升、显存+1~2G；8G 卡建议 False）
VOXCPM_TIMEOUT_SECONDS = 180                  # 单次合成超时（本地推理较慢，50 字内通常 <60s）
VOXCPM_ENABLE_CACHE = True                    # 相同文本不重复合成（wav 落盘 voice_tmp/voxcpm_cache）
VOXCPM_CACHE_MAX_FILES = 200                  # 缓存文件数上限，超出删除最旧的
VOXCPM_LOG_FILE = "vox_tts_server.log"        # sidecar 服务日志（项目根目录）
# 音色设计（Voice Design）：自然语言描述想要的声音，模型凭空生成该音色，无需参考音频。
# 官方示例用英文描述效果最稳，如 "A young woman, gentle and sweet voice"。
# 留空 "" = 模型自行发挥（每条语音随机音色）；改了立即生效（缓存自动失效重合成）。
# 可描述维度：性别/年龄/语气/情绪/语速，例："一个十几岁的可爱女孩，声音甜美，语速稍快"（中文也支持）
VOXCPM_VOICE_DESC = "a sweet young woman with natural girlish charm, warm bright and energetic, playful teasing tone, medium pace"
# 语速系数（保音高变速，声音采样不变、整体变慢/变快）：
#   1.0 = 原速；0.9 = 慢 10%；0.85 = 慢 15%；1.1 = 快 10%
# 注意：改描述里的语速词会改变声线种子（声音会变），调这里则声音不变
VOXCPM_VOICE_SPEED = 0.9

# ffmpeg 可执行文件路径（如果 ffmpeg 已加入系统 PATH，留空即可；否则填完整路径）
# 例如：FFMPEG_PATH = r"C:\ffmpeg\bin\ffmpeg.exe"
FFMPEG_PATH = _os.path.join(_TOOLS, "ffmpeg", "ffmpeg-2026-05-28-git-7b46c6a2a3-full_build", "bin", "ffmpeg.exe")

# silk_v3_decoder 路径（QQ 语音 SILK V3 解码工具）
SILK_DECODER_PATH = _os.path.join(_TOOLS, "silk2mp3", "silk_v3_decoder.exe")

# ---- 低延迟实时语音对话（voice_room.py / realtime_voice.py，智谱 GLM-Realtime 全双工）----
# 本机麦克风+音箱的免提实时对讲,一条 WebSocket 完成 流式ASR+LLM+TTS,端到端延迟约 1s。
# 前提:电脑有可用麦克风/音箱(采集用 sounddevice),GLM key 有 realtime 接口权限。
ENABLE_REALTIME_VOICE = False          # QQ 命令入口总开关
GLM_REALTIME_WS_URL = "wss://open.bigmodel.cn/api/paas/v4/realtime"
GLM_REALTIME_API_KEY = ""              # 留空用 GLM_API_KEY
GLM_REALTIME_MODEL = "glm-realtime-flash"   # 可选 glm-realtime-air(更强/更贵)
GLM_REALTIME_VOICE = "tongtong"        # 音色(与 QQ 语音一致,肥鱼娘)
# 对讲模式:free=免提(server_vad,听到说话自动回答,建议耳机防回声);
#          ptt=按住热键说话(松开提交,外放也稳)
REALTIME_TALK_MODE = "free"
REALTIME_INPUT_DEVICE = ""             # 麦克风设备名/序号,留空=系统默认
REALTIME_OUTPUT_DEVICE = ""            # 音箱设备名/序号,留空=系统默认
REALTIME_INPUT_SAMPLE_RATE = 16000     # 上行采样率(16/24/48k 均可)
REALTIME_IDLE_TIMEOUT = 300            # 无人说话多少秒自动挂断(0=不自动挂,小心计费)
REALTIME_AUTO_SEARCH = False           # GLM 自带联网(默认关,避免实时会话里乱搜产生费用)
REALTIME_TALK_HOTKEY = "f8"            # 全局热键:free=单击开关;ptt=按住说话(keyboard 库)
REALTIME_OWNER_QQ = ""                 # 本机使用者 QQ:填了会把实时对话写入该 QQ 私聊记忆
REALTIME_MEMORY_LOG = True             # 会话转录落盘 data/realtime_chat_YYYYMMDD.jsonl

# ==================== 新模块配置（bot.py 及各功能模块引用，缺失会导致启动崩溃） ====================

# 重要事项自动记录：AI 从对话里自动提取值得长期记住的事（important_notes.py）
ENABLE_AUTO_IMPORTANT_NOTES = True

# AI 自我档案：让 AI 记住"自己是谁、有哪些经历"（ai_profile.py / memory_context.py）
ENABLE_AI_PROFILE = True

# ---- AI 情绪模块（emotion.py：肥鱼娘的心情状态，随互动起伏、随时间平复）----
# 感知不新增 LLM 调用：聊天每轮已有的「记忆提取」会顺带分析这句话让 AI 产生什么情绪
EMOTION_ENABLED = True            # 总开关（可 /心情 查看与重置）
EMOTION_FILE = ""                 # 心情档案文件（留空=脚本目录 emotion_data.json）
EMOTION_DECAY_HOURS = 2.0         # 心情底色向平静回落的半衰期（小时）：1 个半衰期消一半
EMOTION_LABEL_MINUTES = 40        # "此刻主导情绪"标签的基础持续分钟数（×强度 1~3）
# 称呼显式覆盖：user_id -> 称呼。优先级最高，越过档案自动推断（用户明确要求的叫法）
NAME_OVERRIDES = {
    # 纯净包：已清空。格式 "QQ号": "称呼"
}

# Gemini 实时视觉（vision_capture.py：定时抓屏让 Gemini 看，实现"实时看屏幕/直播"）
ENABLE_LIVE_VISION = False                       # 需要先填好下面的 Key 再开启
GEMINI_API_KEY = ""                              # Gemini API Key（走中转站就填中转站的 Key）
GEMINI_BASE_URL = "https://api.openclawplan.com" # Gemini 接口地址（默认走中转站）
GEMINI_MODEL = "gemini-3.7-flash"                # Gemini 模型名

# 本地视频理解模型（local_video_understand.py：从 HuggingFace 下载 Qwen-VL 在本机跑）
ENABLE_LOCAL_VL = False
PRELOAD_LOCAL_VL = False       # True = 启动时就后台加载模型（需先开 ENABLE_LOCAL_VL）
LOCAL_VL_MODEL = "Qwen/Qwen2.5-VL-3B-Instruct"
HF_ENDPOINT = "https://hf-mirror.com"  # 国内 HuggingFace 镜像加速，留空则直连官网
HF_HOME = _os.path.join(_DATA, "hf_cache")        # 模型缓存：包内 data/hf_cache（首次自动下载）

# 视频下载临时目录（bot.py 下载视频时用，留空则用系统临时目录）
VIDEO_TMP_DIR = _os.path.join(_DATA, "video_tmp")

# 主动说话（proactive_speaker.py：机器人隔一阵子主动找你/在群里开话题）
ENABLE_PROACTIVE_SPEAKER =  True          # True 后启用主动说话调度器
PROACTIVE_PRIVATE_USER_ID = ""  # 纯净包：已清空。填 QQ 号则主动私聊该对象
PROACTIVE_GROUP_ID = ""                   # 主动说话的群号，留空则不在群里主动说
PROACTIVE_GROUP_REPLY = False             # 群内主动接话开关

# ---- 跨平台人物身份绑定（identity.py：QQ ↔ MC 游戏名记忆打通）----
# 双向确认后，同一个人的档案/笔记/全文历史在 QQ 与游戏两侧按同一键读写，
# 对话中互相注入对方场景的身份提示。主人绑定由下方列表开机预置（等价旧
# mc_bot_brain.py 硬编码的 MC_OWNER_GAME_NAMES，已迁移至此）。
ENABLE_IDENTITY_LINK = True              # 身份绑定总开关
IDENTITY_OWNER_MC_NAMES = ["insomnic"]   # Evan在游戏里的名字（小写比较）

# Minecraft 相关数据文件（留空则使用脚本目录下默认文件名）
MC_EXPLORED_FILE = ""   # 默认 mc_explored.json
MC_TIPS_FILE = ""       # 默认 mc_tips.json

# ---- 视频理解参数（video_processor.py）----
VIDEO_FILE_WAIT_SECONDS = 30    # 等待视频文件下载完成的超时（秒）
VIDEO_EXTRACT_FPS = 24          # 抽帧帧率（发给视觉 API 用）
VIDEO_MAX_FRAMES = 24           # 最多抽多少帧发给视觉 API
VIDEO_MIN_FPS = 4.0             # 帧数超上限时抽帧帧率下限
ENABLE_VIDEO_AUDIO = True       # 是否提取音频转文字一起理解
VIDEO_AUDIO_SEGMENT_LEN = 5.0   # 音频分段长度（秒）
VIDEO_LOCAL_EXTRACT_FPS = 8     # 本地模型抽帧帧率
VIDEO_LOCAL_MAX_FRAMES = 12     # 本地模型最多帧数
VIDEO_LOCAL_SIZE = 448          # 本地模型输入图片边长

# ---- Gemini 实时视觉参数（vision_capture.py，需先开 ENABLE_LIVE_VISION）----
VISION_CAPTURE_INTERVAL = 1 / 24    # 抓屏间隔（约 24fps）
VISION_FRAME_BUFFER_SIZE = 48       # 帧缓冲区大小
VISION_BUFFER_TIME = 2.0            # 识别时回看的秒数
VISION_CHANGE_THRESHOLD = 2.0       # 画面变化判定阈值
VISION_MAX_DURATION = 600           # 单次识别最多回看的时长（秒）
VISION_MAX_RECOGNITION_INTERVAL = 3.0   # 两次识别最小间隔（秒）
VISION_MIN_RECOGNITION_INTERVAL = 0.5   # 画面剧变时最快识别间隔（秒）
VISION_RECOGNITION_SAMPLE_COUNT = 5     # 每次识别从缓冲区抽几帧
CAMERA_FPS = 30                     # 摄像头/抓屏帧率
CAMERA_WIDTH = 1280
CAMERA_HEIGHT = 720

# ---- Minecraft 功能（mc_agent.py / mc_watcher.py 等，默认关闭）----
ENABLE_MC_AGENT = False         # 让 AI 自己玩 Minecraft
ENABLE_MC_WATCH = False         # 监控 Minecraft 日志并聊天
# ---- 纯净世界试验场（mc_bot/bridge.js + mc_bot_brain.py）----
ENABLE_MC_LIVE_HINT = True      # QQ 对话注入"游戏里的我"实时动态（仅主人对话生效）
ENABLE_MC_QQ_NOTIFY = True      # 游戏侧重大事件（低血/掉线/死亡）主动私聊汇报主人
QQ_HTTP_API = "http://127.0.0.1:3000"  # SnowLuma OneBot HTTP 接口（大脑进程直接推 QQ 用）
MINECRAFT_DIR = ""  # 便携包：目标机器装了 Minecraft 再填 .minecraft 绝对路径（MC 大脑才需要）
MINECRAFT_LOG_PATH = ""         # 最新日志文件路径（留空自动找）
MINECRAFT_HTTP_PORT = 8765      # 游戏内 HTTP 模组端口
MINECRAFT_POLL_INTERVAL = 1.0   # 日志轮询间隔（秒）
MINECRAFT_RECENT_MAX = 50       # 保留最近多少条游戏事件
MC_AGENT_LOOP_INTERVAL = 2.5    # 智能体主循环间隔（秒）
MC_AGENT_MAX_TOOL_CALLS = 16    # 一轮内最多工具调用次数
MC_AGENT_ROUND_SECONDS = 90.0   # 一轮大脑决策的最大时长（秒）
MC_AGENT_MSG_WINDOW = 60        # 智能体会话滚动窗口（条），超出按轮边界裁剪
MC_AGENT_LLM_FAIL_FALLBACK = 3  # LLM 连续失败多少次后退回规则保命模式
MC_AGENT_USE_LLM_BRAIN = True   # True=LLM 工具调用大脑；False=纯规则保命模式
MC_AGENT_USE_THINKING = True  # 深度思考模式（决策质量更高，token/延迟不计）
MC_AGENT_SURVEY_EVERY = 3     # 每 N 轮自动勘察一次周边（越小信息越新）
MC_AGENT_LONG_GOAL = ""         # 长期目标（留空用默认），大脑可自行调整
MC_GUARD_WARNING_COOLDOWN = 15.0  # 同类守卫告警冷却（秒）：冷却期内不重复急停/注入
MC_FALLBACK_RETRY_EVERY = 5     # 规则保命模式每 N 轮重试一次大脑，避免永久降级

# ---- 电脑操控（pc_agent.py / pc_control.py，主人专属，/电脑做 下任务）----
ENABLE_PC_CONTROL = False
PC_AGENT_MAX_STEPS = 12         # 每个任务最多「截屏→决策」轮数
PC_AGENT_MAX_TOOL_CALLS = 8     # 每轮 LLM 最多动作数
PC_AGENT_MSG_WINDOW = 40        # 会话滚动窗口（条），超出按观察组边界裁剪
PC_ACTION_DELAY = 0.5           # 相邻两次键鼠动作最小间隔（秒），防狂点
PC_SCREEN_MAX_SIZE = 1280       # 截图长边像素（视觉模型输入，也决定坐标换算）
PC_ALLOW_SHELL = True           # 允许 run_command 工具（带高危命令黑名单）
PC_SHELL_TIMEOUT = 30           # shell 命令超时（秒）

# ---- PVZ 大脑（pvz_agent.py 等，主人专属，/pvz 开局她自主玩植物大战僵尸）----
ENABLE_PVZ_BRAIN = False
PVZ_EXE_PATH = _os.path.join(_PVZ_DIR, "PlantsVsZombies.exe")   # 包内 pvz_games/PlantsVsZombies.exe（可选）
PVZ_WINDOW_CLASS = "MainWindow"         # 实测（1.2.0.1073）窗口类名；类名找不到时按标题子串匹配
PVZ_WINDOW_TITLE = "Plants vs. Zombies" # 窗口标题前缀（子串匹配，不区分大小写）
# 实测交互约束：该版本只认物理真实鼠标点击（pyautogui），且窗口失焦自动暂停
# → 她玩的时候游戏必须保持前台，主人别动鼠标（同 /电脑做）
PVZ_TICK_SECONDS = 2.0                  # 自主循环 tick：阳光快层每个 tick 收
PVZ_DECIDE_INTERVAL = 5.0               # 慢层：每 N 秒一次 LLM 读盘 + 策略决策
PVZ_MAX_GAME_SECONDS = 1800             # 一局最长时长（超时自动停局防失控）
PVZ_MAX_READ_FAILS = 4                  # 连续读盘失败 N 次 → 停局求助主人
PVZ_CLICK_DELAY = 0.45                  # 她自己的键鼠动作最小间隔（秒）
PVZ_AUTO_RESTART = False                # 胜负结算后是否自动开下一关（v1 默认问主人）
# 场地几何：以「客户区逻辑坐标」为基准（GetClientRect/PostMessage 所在空间，
# 1073 实测 640x480；物理截图 800x600 是 DPI 125% 放大）。截图/视觉模型用的是物理
# 像素，pvz_vision 里用 capture 实际尺寸↔逻辑尺寸做双向换算，别混用。
PVZ_REF_SIZE = (640, 480)               # 逻辑客户区基准（改窗口大小/缩放时重新标定）
PVZ_ROWS_Y = [117, 175, 233, 291, 350]  # 5 行草坪行中心 y（物理800x600实测值÷1.25）
PVZ_COLS_X = [78, 144, 210, 276, 342, 409, 475, 542, 608]  # 9 列格中心 x（同上）
PVZ_BANK_CARD0 = (68, 48)               # 第一张种子卡中心（实测卡区 x 60..110 ÷1.25 微调）
PVZ_BANK_DX = 41                        # 相邻卡中心 x 间距
# 阳光快层在「物理像素」检测（cv2 对截图），面积参数用物理像素
PVZ_SUN_MIN_AREA = 300                  # 阳光黄色斑块面积下限（px²，1073 实测阳光 700-3000）
PVZ_SUN_MAX_AREA = 5000                 # 上限（超过 = 背景黄花朵/卡片，忽略）
MC_AGENT_END_TURN_INTERVAL = 0.5  # end_turn 干净收尾后进入下一轮的间隔（秒，越小动作越连贯）
MC_SKILLS_MAX = 60              # 技能库最多保存多少个技能

# ---- MC 智能体行为遥测（mc_monitor.py，记录到 data/mc_behavior.jsonl）----
MC_AGENT_MONITOR = True         # 是否记录行为遥测（供 mc_analyze.py 分析）
MC_MONITOR_FLUSH_SECONDS = 2.0  # 遥测落盘间隔（秒）
MC_MONITOR_BUFFER = 40          # 内存缓冲条数，达到立即落盘
MC_DIG_TIMEOUT = 15.0           # 挖方块超时（秒）
MC_ORE_PRIORITY_DIST = 10.0     # 矿石优先距离
MC_ORE_SEEK_RADIUS = 12.0       # 找矿半径
MC_ORE_HINT_WHEN_NONE = True    # 找不到矿时给提示
MC_TIPS_INJECT_MAX = 6          # 注入提示词的技巧条数上限
MC_EXPLORE_DIST = 40            # 单次探索距离
MC_EXPLORE_RECALC = 5           # 探索路径重算次数
MC_EXPLORE_TIMEOUT = 90         # 探索超时（秒）
MC_EXPLORED_DIRECTION_RADIUS = 8    # 已探索区域半径（区块）
MC_ORGANIZE_THRESHOLD = 0.75        # 背包整理触发阈值

# ---- 主动说话参数（proactive_speaker.py，需先开 ENABLE_PROACTIVE_SPEAKER）----
PROACTIVE_WARMUP_DURATION = 180     # 开启后前 3 分钟热身（更爱说话）
PROACTIVE_WARMUP_PROB = 1 / 3       # 热身期每轮主动说话概率
PROACTIVE_STABLE_PROB = 1 / 30      # 稳定期主动说话概率
PROACTIVE_UNREPLIED_LIMIT = 3       # 连续几条不回就进入沉默
PROACTIVE_SILENT_DURATION = 21600   # 沉默时长（6 小时）
PROACTIVE_PRIVATE_MIN_INTERVAL = 60 # 私聊主动说话最小间隔（秒）
PROACTIVE_GROUP_MIN_INTERVAL = 300  # 群里主动说话最小间隔（秒）

# ---- 记忆容量参数 ----
IMPORTANT_NOTES_MAX = 400           # 重要事项最多保留条数
FULL_HISTORY_MAX_RECORDS = 1000000  # 全文历史最多保留条数

# ---- CodeBuddy 自我编程模块（codebuddy_cli.py）----
CODEBUDDY_CLI_PATH = ""  # 便携包：目标机器装了 CodeBuddy CLI 再填绝对路径（自我编程模块才需要）
CODEBUDDY_WORK_DIR = _HERE
CODEBUDDY_ALLOWED_TOOLS = "Read,Write,Edit"
CODEBUDDY_TIMEOUT = 120
# 受保护的核心文件（CodeBuddy 改这些文件需要人工确认）
CODEBUDDY_CORE_FILES = [
    "bot.py",             # 主程序
    "mc_agent.py",        # 自主游戏智能体
    "mc_watcher.py",      # Minecraft 监控
    "codebuddy_cli.py",   # 自我编程模块（防止 AI 改掉自己的保护机制）
    "message_bus.py",     # 消息抽象层
    "qq_adapter.py",      # QQ 适配器
    "config.py",          # 全局配置
    "agent_core.py",      # 智能体核心
    "scheduler.py",       # 调度器
    "deepseek_client.py", # AI 客户端
]

# ---- 平台插件开关（智能体为核心，QQ 只是其中一个可插拔插件）----
# ENABLE_QQ_PLUGIN=False 且 ENABLE_CONSOLE_PLUGIN=True 时可完全脱离 QQ 运行
ENABLE_QQ_PLUGIN = False
ENABLE_CONSOLE_PLUGIN = False    # 终端聊天插件（python main.py --console 可临时启用）
ENABLE_WEB_PLUGIN = True        # 内置 Web 控制台（前端 + 数据/控制 API）开关

# ---- B 站直播平台插件（bilibili_plugin.py，python main.py --bili 可单独启用）----
# 两种模式：streamer = 她自己开播当主播（弹幕当耳朵，回应说进直播音频）；
#          viewer  = 弹幕机器人（连别人的直播间，命中触发词回弹幕）
ENABLE_BILIBILI_PLUGIN = False
BILIBILI_MODE = "streamer"       # "streamer" 当主播 / "viewer" 弹幕机器人
BILIBILI_ROOM_ID = "1727071384"            # 直播间房间号（她自己的房间；viewer 模式填要看的房间）
BILIBILI_AUTO_START = False      # False = 启动只待命，QQ 发「/直播 开播」才上线
                                 # True = 机器人一启动就自动开播（streamer 含开声音/连弹幕/拉 MC）

# ---- 登录 Cookie（发弹幕 / 开播必需）：浏览器登录 B 站 → F12 → Application → Cookies 复制 ----
BILIBILI_SESSDATA = ""  # 纯净包：已清空，B 站功能需自行登录获取
BILIBILI_CSRF = ""                                # 纯净包：已清空（和 SESSDATA 配套）

# ---- B 站私信 AI 自动回复（白名单 UID，默认关）----
# 复用 bili_api.BiliSession（上面的 SESSDATA/CSRF），把私信转成 InboundMessage 走核心 ChatService，
# 回复经 ReplyTarget 发回私信。仅对白名单内的 UID 自动回复，其余私信仅日志记录不回复（防打扰现实好友）。
ENABLE_BILIBILI_DM = False
BILIBILI_DM_WHITELIST = ["1061680069"]       # 允许自动回复的对方 UID 列表；空=全部不回（仅监听/调试）
BILIBILI_DM_OWNER_UID = "1800427899"  # 主人 UID（nsid=Insomnic_Evan）；私信里带「/」命令仅主人可执行，防滥用
BILIBILI_DM_POLL_SEC = 15        # 轮询间隔（秒）
BILIBILI_DM_MAX_CHARS = 500      # 单条私信最大字符数（超长自动切分发送）

# ---- B 站每日定时自主学习（移植 BLB「自己看→总结→沉淀→分享」）----
# 与 /b站看 不同：无人触发，每天从下面 UP 主拉视频自动学习并分享。总开关：
ENABLE_BILIBILI_LEARN_SCHEDULE = False
BILIBILI_LEARN_UP_UIDS = []             # 可选：要自动学习的 UP 主 UID 列表；例如 ["123456","654321"]
BILIBILI_LEARN_KEYWORDS = ["编程", "心理学", "冷知识", "科普", "AI"]  # 方向关键词（不指定 UP 时靠它搜视频）；例如 ["历史", "科普", "经济学"]
BILIBILI_LEARN_DAILY_COUNT = 12         # 每天学习条数（10~15 之间调）
BILIBILI_LEARN_START_HOUR = 9           # 每天开始学习的小时（含）
BILIBILI_LEARN_END_HOUR = 23            # 每天结束学习的小时（不含）
BILIBILI_LEARN_INTERVAL_MIN = 75        # 两条之间最小间隔（分钟），把一天的量铺开，避免刷屏
BILIBILI_LEARN_SHARE = "dm"             # 分享方式：dm=发给私信 UID；none=只学不分享
BILIBILI_LEARN_SHARE_UIDS = []          # 分享目标 UID；空=复用 BILIBILI_DM_WHITELIST
ENABLE_BILI_AUDIO_ASR = True      # 视频无字幕时，下载音频做 ASR（听声音）补齐学习内容；更慢/更费但能看懂无字幕视频
BILIBILI_AUDIO_MAX_SEC = 900      # 单视频最多取前多少秒音频做 ASR（控制成本/时长，默认 15 分钟）


# ---- streamer 模式（开播推流）----
# 前置：账号已开通直播间（直播权限 + 实名认证）
BILIBILI_PUSH_MODE = "hime"      # hime=直播姬管画面/推流（机器人只把声音播进虚拟声卡，无需 Cookie）
                                 # ffmpeg=自研推流（需要 Cookie，画面由下面 VIDEO_SOURCE 决定）
BILIBILI_CABLE_DEVICE = "CABLE Input"  # hime 模式：虚拟声卡播放设备名关键词（VB-CABLE 默认叫这个）
BILIBILI_STREAM_TITLE = "小鱼直播中～来陪我聊天吧"   # 开播时自动设置的标题（ffmpeg 模式）
BILIBILI_AREA_ID = 0             # 直播二级分区 id（0=自动选「聊天室」）
BILIBILI_VIDEO_SOURCE = "window" # 画面源（ffmpeg 模式）：window=捕获游戏窗口 / image=立绘图 / desktop=全屏
BILIBILI_WINDOW_TITLE = ""       # window 模式：窗口标题关键词（模糊匹配即可；留空自动找 Minecraft 窗口）
BILIBILI_STREAM_WITH_MC = True   # 开播时自动启动 MC 大脑（她开始玩 Minecraft，/mc自动 同款）
BILIBILI_STREAM_IMAGE = ""       # image 模式：立绘图路径（留空自动生成一张默认图）
BILIBILI_STREAM_RESOLUTION = "1280x720"  # 推流分辨率
BILIBILI_STREAM_FPS = 15         # 推流帧率（静态图/低动态画面够用，省带宽）
BILIBILI_STREAM_VIDEO_KBPS = 1500  # 视频码率 kbps
BILIBILI_FFMPEG_PATH = ""        # 推流专用 ffmpeg（留空自动选：imageio-ffmpeg 稳定版 → FFMPEG_PATH）
                                 # 注意：极新的 git 构建（多线程调度器）对实时管道音频会攒缓冲不出流，
                                 # 推流请用稳定发行版；留空时机器人会自动装 imageio-ffmpeg 的稳定版

# ---- 主播行为（streamer 模式）----
BILIBILI_HOST_REPLY_ALL = True   # True = 观众每条弹幕都回应（配合冷却防刷屏）
BILIBILI_HOST_THANK_GIFT = True  # 礼物/上舰进核心，她口播感谢
BILIBILI_HOST_ECHO_DANMAKU = False  # 回应时是否同时发一条弹幕（默认只口播）

# ---- 主播主动闲聊（streamer 模式：直播间安静时她自己找话说，说进直播）----
# 只在直播间真开播时生效（以 B 站侧状态为准，直播姬没点推流/已下播都不会说话）
BILIBILI_HOST_CHAT = True         # 总开关
BILIBILI_HOST_CHAT_IDLE = 30   # 观众多久没发弹幕（秒）就自己开口找话题
BILIBILI_HOST_CHAT_INTERVAL = 30  # 两次主动开口的最小间隔（秒），和上面的值配合防刷屏

# ---- 通用（两种模式共用）----
BILIBILI_TRIGGER_WORDS = ["肥鱼娘", "小鱼", "机器人", "大肥鱼"]  # viewer 模式：弹幕包含任一词才回复
BILIBILI_REPLY_ALL =True     # viewer 模式：True = 所有弹幕都回复（弹幕量大时慎开）
BILIBILI_REPLY_COOLDOWN = 8.0    # 两次 AI 回复之间的最小间隔（秒），防连发刷屏
BILIBILI_DANMAKU_MAX_CHARS = 20  # 弹幕长度上限（普通账号 20 字，AI 长回复自动截断）
BILIBILI_DANMAKU_INTERVAL = 5.0  # 两次发弹幕的最小间隔（秒，B 站风控要求）
BILIBILI_DANMAKU_COLOR = 16777215  # 弹幕颜色（16777215 = 白色）
