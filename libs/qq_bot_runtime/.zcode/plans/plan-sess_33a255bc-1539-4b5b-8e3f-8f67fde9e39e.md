# 低延迟实时语音对话(本机免提全双工)— 实施计划

## 目标与默认决策
在电脑上实现"对着肥鱼娘说话、她即时开口回答"的全双工实时对话,端到端延迟目标 <1.5s,可随时打断。
默认路线(你未回复选型问题,按推荐项定案,全部可改):本机麦克风+音箱;引擎=智谱 GLM-Realtime WebSocket 端到端全双工;大脑=独立精简会话(人设+短上下文),转录异步同步记忆,不改动 QQ 聊天主链路。QQ 端只加"开关语音房间"命令。

## 引擎协议(已核实,按此实现)
- 连接:`wss://open.bigmodel.cn/api/paas/v4/realtime`,header `Authorization: Bearer <key>`(key 留空用 config.GLM_API_KEY;若 401,config 可改 z.ai 镜像地址,报错信息给出提示)
- session.update 关键参数:model=`glm-realtime-flash`、input_audio_format=pcm16、output_audio_format=pcm(24kHz 单声道 16bit 固定)、voice=tongtong(沿用肥鱼娘音色)、turn_detection.type=server_vad(免提)/client_vad(PTT)、beta_fields={chat_mode:"audio", tts_source:"e2e", auto_search:False(省费用)}+instructions(精简实时版人设)、temperature
- 上行事件:input_audio_buffer.append(100ms/帧,base64 PCM)/commit/clear、response.cancel(打断)
- 下行事件:response.audio.delta(base64 PCM→即时播放)、response.audio_transcript.delta/text.delta(转录落日志与记忆)、input_audio_buffer.speech_started/stopped、response.created/done、error、heartbeat

## 新增文件
1. **`realtime_voice.py`(引擎封装,约 300 行)**
   - RealtimeSession:连接/断线重连(退避)、session.update、两套线程:采集线程(sounddevice InputStream → 线程安全队列 → asyncio 任务逐帧 append)+ 播放线程(sounddevice OutputStream,由 asyncio 播放任务从队列喂 delta;interrupt=清队列即刻静音+发 response.cancel)
   - 事件回调抽象(on_audio_delta/on_transcript/on_turn_end/on_state/on_error),引擎与"房间状态机"解耦,未来换本地引擎只改这一层
2. **`voice_room.py`(会话控制器,约 250 行)**
   - 单例 VoiceRoom:状态机 idle/listening/talking;free 模式(server_vad 免提)与 ptt 模式(按住热键说话,松开提交)共用同一引擎,模式由 config.REALTIME_TALK_MODE 决定
   - 空闲看门狗:REALTIME_IDLE_TIMEOUT 秒无对话自动挂断并播放提示音(防挂机计费);只允许一个会话
   - 交互:keyboard 库全局热键(free=单击开关、ptt=按住说话),可选配置;会话开关状态用简短合成语音/提示音反馈
   - 转录落盘:data/realtime_chat_YYYYMMDD.jsonl;若配了 REALTIME_OWNER_QQ,把对话写入该 QQ 在现有 Memory 的私聊上下文(异步,不阻塞对话),让 QQ 端能接续话题
   - 开启时自检:枚举音频输入/输出设备,无输入设备给出中文错误并拒绝启动;导出 start()/stop()/status()/handle_command() 供 chat_service 调用
3. **config.py 新增(全部默认关闭/留空,不动现有行为)**:ENABLE_REALTIME_VOICE=False、GLM_REALTIME_WS_URL、GLM_REALTIME_API_KEY=""、GLM_REALTIME_MODEL=glm-realtime-flash、GLM_REALTIME_VOICE=tongtong、REALTIME_TALK_MODE=free、REALTIME_INPUT_DEVICE=""、REALTIME_OUTPUT_DEVICE=""、REALTIME_INPUT_SAMPLE_RATE=16000、REALTIME_IDLE_TIMEOUT=300、REALTIME_AUTO_SEARCH=False、REALTIME_TALK_HOTKEY=f8、REALTIME_OWNER_QQ=""、REALTIME_MEMORY_LOG=True
4. **chat_service.py**:命令分发加 3 条(gated by ENABLE_REALTIME_VOICE):`/语音对讲`(或 `/开语音`)、`/语音对讲关`、`/语音对讲状态`;回复经正常 QQ 通道;关时确认、401/设备缺失等错误原样回传
5. **requirements.txt**:+sounddevice(采集/播放)、keyboard(全局热键;若安装失败热键降级为仅 QQ 命令开关并提示)

## 依赖安装
`venv/Scripts/pip install sounddevice keyboard`(用 E:\qq_bot\venv 现有环境)

## 实施顺序
1. config 键 + requirements + pip 安装
2. realtime_voice.py 引擎(先跑通协议:连接/音频上行/音频下行播放/打断)
3. voice_room.py 状态机 + 热键 + 看门狗 + 转录日志与记忆同步
4. chat_service 命令接线 + 设备自检错误提示
5. 验证:py_compile + `--voice` 自检模式(枚举设备、仅连接不打字) + 真机试讲(PTT 模式先行,free 模式实测回声)

## 已知风险与对策(写进代码注释与错误提示)
- **Key/端点归属**:现有 GLM key 走 api.z.ai;bigmodel realtime 若 401,报错提示改用 z.ai 镜像地址(配置可改)
- **回声**:外放时麦克风会收进机器人自己的声音;server_vad 模式靠 speech_started 打断自愈,实测严重则建议戴耳机或切 PTT;不引入本地 AEC(超出 MVP)
- **计费**:按音频时长;auto_search 默认关、空闲自动挂断、转录日志留作计量
- **同屋他人说话会被识别**:本机私用场景,不做声纹鉴权
- **本轮不做**:GLM tools/Minecraft 状态注入、video_passive 视觉、唤醒词本地模型——引擎接口已留扩展位(后续可给 free 模式加唤醒词/给引擎加本地 ASR 替换)