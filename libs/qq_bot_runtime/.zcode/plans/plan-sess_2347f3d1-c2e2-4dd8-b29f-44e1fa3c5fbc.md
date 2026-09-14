## VoxCPM2 本地 TTS 接入方案

### 背景事实(已调研确认)
- 现有 TTS 链:唯一总入口 `chat_service.text_to_voice_wav()` → `voice_client.text_to_speech()`(智谱 GLM 云,24kHz PCM)→ ffmpeg 转 wav → `reply_voice(wav_path)` 交给 NapCat 自动转 SILK。平台层零改动即可换引擎
- 本机:RTX 5060 Laptop 8GB(当前空闲仅 ~5.1GB);主 venv Python 3.13 + torch 2.9.1+cu128 + **transformers 5.15.0**(活跃 bot 运行环境,不可污染)
- voxcpm 依赖树巨大(gradio/funasr/modelscope/torchcodec 等),与主 venv 生态冲突风险高(依赖解析 10 分钟未完成 = 解析风暴)
- 网络:huggingface.co 与 hf-mirror 均不通,**ModelScope 可达**,已有 `OpenBMB/VoxCPM2` 权重(约 6GB:model.safetensors 4.6G + audiovae 0.38G 等)

### 架构:独立 venv + sidecar 本地服务(用户已确认 bot 自动拉起)
```
┌─ bot 进程(主 venv,零新依赖)─────────────┐
│  tts_vox.py(httpx 客户端)                │
│    └→  POST http://127.0.0.1:8765/tts   │
│       异常/超时/不健康 → 自动回退 GLM     │
└──────────────┬──────────────────────────┘
               │ localhost HTTP(标准库 http.server,无新依赖)
┌──────────────▼──────────────────────────┐
│ vox_tts_server.py + venv_vox(独立进程)   │
│  VoxCPM2 常驻显存,串行推理,GET /health   │
└─────────────────────────────────────────┘
```

### 实施步骤
1. **独立环境**:用现有 base(Python 3.13.12)创建 `F:\qq_bot\venv_vox`;优先复用 pip 缓存安装主 venv 同款 `torch 2.9.1+cu128`(命中缓存免 3GB 下载),再装 `voxcpm`(PyPI 2.0.3;torchcodec 等关键包已确认有 cp313 win wheel)
2. **下载权重**:ModelScope 拉 `OpenBMB/VoxCPM2` 到 `F:\qq_bot\models\VoxCPM2`(约 6GB,磁盘 917G 充足)
3. **写 `vox_tts_server.py`**(放项目根,venv_vox 运行):
   - 标准库 `http.server` 即可(不引 fastapi);`GET /health`(模型就绪/显存状态)、`POST /tts {text, seed}` → 48kHz wav bytes
   - 模型惰性加载 + 首次预热;推理放线程池串行;失败返回明确错误码
   - 显存策略按序尝试:fp16 + `load_denoiser=False`(省显存)→ 仍 OOM 则报不健康,由 bot 回退 GLM
4. **单句验证**:server 起后 POST 一段中文,确认能合成 wav 并播放
5. **bot 侧接入(改动很小)**:
   - `config.py`:`VOICE_TTS_ENGINE = "voxcpm"`(voxcpm/glm)、`VOXCPM_TTS_URL = "http://127.0.0.1:8765"`、超时/缓存上限开关
   - 新文件 `tts_vox.py`:`VoxTTSPlugin(FeaturePlugin)`(start 时 spawn `venv_vox` 子进程、健康轮询、stop 时 terminate)+ 异步 httpx 客户端 + wav 磁盘缓存;spawn 失败/未安装 → 打印告警并让引擎回退 GLM
   - `agent_core.register_builtin_plugins()` 注册该插件(按 config 开关)
   - `chat_service.text_to_voice_wav()` 里做引擎分发(唯一入口,语音回复/主动语音全生效):engine=voxcpm 且服务健康 → sidecar;否则原 GLM 路径;voice_client.py 与 QQ 平台层**不动**
   - 缓存:VoxCPM 复用同一套 MD5 缓存思路但独立目录(48k 参数与 GLM 24k PCM 不同,键含引擎名,免冲突)
6. **端到端验证**:bot 起服务 → QQ/控制台触发语音回复(如发「语音 你好」)→ 确认收到 VoxCPM 音色语音;手动 kill sidecar → 再触发 → 确认自动回退 GLM 且 bot 不崩
7. **后续说明**(不做,留档):VoxCPM 的音色设计/克隆 API 需要参考音频或描述参数,第一版只做普通 TTS,接口预留 seed/voice 参数

### 风险与预案
- **安装失败**(依赖互斥/无 wheel):分步安装定位问题;核心兜底是 bot 侧回退 GLM,bot 永远不哑
- **显存不足**:5.1G 空闲 vs fp16 ~5.5G+ → 依次试 load_denoiser=False、8bit bnb;都不行则 /health 报不健康 → 自动 GLM
- **速度**:5060 Laptop 一段 ~20s 语音预计 10~25s(4090 基准 RTF 0.3),语音回复会明显变慢,属预期;嫌慢可 config 一行切回 GLM
- **QQ 语音上限**:沿用现有 140 字分片逻辑,不需改