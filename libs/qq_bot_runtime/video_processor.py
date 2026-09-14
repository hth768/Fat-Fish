# -*- coding: utf-8 -*-
"""视频抽帧与理解模块。

用 ffmpeg 把视频按目标帧率抽帧，复用 Gemini 多帧理解能力。
支持 24fps 抽帧（模拟人眼），但对长视频自动降采样，避免 token 爆炸。
"""
import os
import subprocess
import tempfile
import time
from typing import List, Optional

import cv2

import config
from ai_provider import get_vision


def get_ffmpeg_path() -> str:
    return getattr(config, "FFMPEG_PATH", "ffmpeg")


def extract_frames(
    video_path: str,
    fps: float = 24,
    max_frames: int = 24,
    max_size: int = 720,
    min_fps: float = 2.0,
) -> List[bytes]:
    """从视频中抽取关键帧，返回 JPEG 字节列表。

    video_path: 视频文件路径
    fps: 目标抽帧帧率（默认 24，模拟人眼）
    max_frames: 最多返回多少帧（防止长视频 token 爆炸）
    max_size: 抽帧图片最大边（默认 720p）
    min_fps: 长视频降采样时最低允许的帧率（默认 2fps，即每 0.5 秒至少一帧）

    返回按时间顺序排列的 JPEG 字节列表。

    策略：
    - 短视频（fps 下总帧数 <= max_frames）：按目标 fps 全量抽
    - 长视频：降到 max_frames，但不会低于 min_fps。若 min_fps 下仍超 max_frames，
      则返回超过 max_frames 的帧（交给调用方分段理解），绝不压到 1 秒 1 帧以下。
    """
    if not os.path.exists(video_path):
        raise FileNotFoundError(f"视频文件不存在: {video_path}")

    duration = _get_duration(video_path)
    if duration is None or duration <= 0:
        return _extract_by_ffmpeg(video_path, fps, max_frames, max_size)

    # 目标 fps 下的总帧数
    total_at_fps = int(duration * fps)

    # 情况1：短视频，全量按目标 fps 抽
    if total_at_fps <= max_frames:
        return _extract_by_ffmpeg(video_path, fps, max_frames, max_size)

    # 情况2：长视频，但用 min_fps 抽出来的帧数 <= max_frames
    total_at_min = int(duration * min_fps)
    if total_at_min <= max_frames:
        # 在 min_fps 和目标 fps 之间选一个，让帧数尽量接近 max_frames
        effective_fps = max_frames / duration
        return _extract_by_ffmpeg(video_path, effective_fps, max_frames, max_size)

    # 情况3：超长视频，即使 min_fps 也超过 max_frames
    # 保持 min_fps 抽帧，返回超过 max_frames 的帧（交给 describe_video 分段理解）
    return _extract_by_ffmpeg(video_path, min_fps, total_at_min, max_size)


def _get_duration(video_path: str) -> Optional[float]:
    """获取视频时长（秒）。优先用 ffmpeg，失败则用 OpenCV。"""
    ffmpeg = get_ffmpeg_path()

    # 方法1：用 ffmpeg 获取时长（ffprobe 可能损坏，ffmpeg 更可靠）
    try:
        result = subprocess.run(
            [ffmpeg, "-i", video_path],
            capture_output=True, text=True, timeout=30,
        )
        # ffmpeg 把时长输出到 stderr，格式类似 "Duration: 00:00:03.00"
        stderr = result.stderr
        import re
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", stderr)
        if m:
            h, minute, s = int(m.group(1)), int(m.group(2)), float(m.group(3))
            return h * 3600 + minute * 60 + s
    except Exception as e:
        print(f"[WARN] ffmpeg 获取时长失败: {e}")

    # 方法2：用 OpenCV 获取时长
    try:
        cap = cv2.VideoCapture(video_path)
        if cap.isOpened():
            fps = cap.get(cv2.CAP_PROP_FPS)
            total = cap.get(cv2.CAP_PROP_FRAME_COUNT)
            cap.release()
            if fps and total:
                return total / fps
    except Exception as e:
        print(f"[WARN] OpenCV 获取时长失败: {e}")

    return None


def _extract_by_ffmpeg(
    video_path: str,
    fps: float,
    max_frames: int,
    max_size: int,
) -> List[bytes]:
    """用 ffmpeg 抽帧，返回 JPEG 字节列表。"""
    ffmpeg = get_ffmpeg_path()
    frames: List[bytes] = []

    # 用临时目录存抽出的帧（放到 VIDEO_TMP_DIR，默认 E 盘，避免占满 C 盘）
    tmp_base = getattr(config, "VIDEO_TMP_DIR", "") or tempfile.gettempdir()
    os.makedirs(tmp_base, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=tmp_base) as tmpdir:
        pattern = os.path.join(tmpdir, "frame_%04d.jpg")
        # fps filter 抽帧，scale 限制最大边
        cmd = [
            ffmpeg, "-y",
            "-i", video_path,
            "-vf", f"fps={fps},scale='min({max_size},iw)':-2",
            "-q:v", "3",
            pattern,
        ]
        try:
            subprocess.run(cmd, capture_output=True, timeout=120)
        except Exception as e:
            print(f"[WARN] ffmpeg 抽帧失败: {e}")
            return []

        # 读取抽出的帧
        for fname in sorted(os.listdir(tmpdir)):
            if fname.endswith(".jpg"):
                fpath = os.path.join(tmpdir, fname)
                with open(fpath, "rb") as f:
                    frames.append(f.read())
                if len(frames) >= max_frames:
                    break

    return frames


def extract_audio(video_path: str) -> bytes:
    """从视频中提取音频，返回 wav 字节。无音轨或无音频时返回 b''。"""
    ffmpeg = get_ffmpeg_path()
    # 用临时目录放 wav
    tmp_base = getattr(config, "VIDEO_TMP_DIR", "") or tempfile.gettempdir()
    os.makedirs(tmp_base, exist_ok=True)
    wav_path = os.path.join(tmp_base, f"tmp_audio_{os.getpid()}_{int(time.time())}.wav")
    cmd = [
        ffmpeg, "-y",
        "-i", video_path,
        "-vn",                       # 去掉视频流
        "-ac", "1",                  # 单声道
        "-ar", "16000",              # 16kHz（ASR 常用采样率）
        wav_path,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=120)
        if result.returncode != 0 or not os.path.exists(wav_path):
            return b""
        with open(wav_path, "rb") as f:
            data = f.read()
        return data
    except Exception as e:
        print(f"[WARN] 提取音频失败: {e}")
        return b""
    finally:
        if os.path.exists(wav_path):
            try:
                os.remove(wav_path)
            except OSError:
                pass


def has_audio_stream(video_path: str) -> bool:
    """检查视频是否包含音频流。"""
    ffmpeg = get_ffmpeg_path()
    try:
        result = subprocess.run(
            [ffmpeg, "-i", video_path],
            capture_output=True, text=True, timeout=30,
        )
        return "Audio:" in result.stderr
    except Exception:
        return False


def extract_audio_segment(video_path: str, start: float, end: float) -> bytes:
    """提取视频某时间段[start, end]的音频，返回 wav 字节。"""
    ffmpeg = get_ffmpeg_path()
    tmp_base = getattr(config, "VIDEO_TMP_DIR", "") or tempfile.gettempdir()
    os.makedirs(tmp_base, exist_ok=True)
    wav_path = os.path.join(tmp_base, f"tmp_seg_{os.getpid()}_{int(time.time()*1000)}.wav")
    duration = max(0.1, end - start)
    cmd = [
        ffmpeg, "-y",
        "-ss", str(start),
        "-t", str(duration),
        "-i", video_path,
        "-vn",
        "-ac", "1",
        "-ar", "16000",
        wav_path,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=60)
        if result.returncode != 0 or not os.path.exists(wav_path):
            return b""
        with open(wav_path, "rb") as f:
            return f.read()
    except Exception as e:
        print(f"[WARN] 分段音频提取失败 {start}-{end}: {e}")
        return b""
    finally:
        if os.path.exists(wav_path):
            try:
                os.remove(wav_path)
            except OSError:
                pass


async def _transcribe_audio_segments(video_path: str, segment_len: float = 5.0) -> list:
    """分段提取音频并转文字，返回带时间戳的列表。

    返回：[{"start": 秒, "end": 秒, "text": 识别文字}]
    只有文字非空的段才保留。无音频或失败返回空列表。
    """
    duration = _get_duration(video_path)
    if not duration or duration <= 0:
        return []
    if not has_audio_stream(video_path):
        return []

    from voice_client import speech_to_text

    segments = []
    start = 0.0
    while start < duration:
        end = min(start + segment_len, duration)
        audio = extract_audio_segment(video_path, start, end)
        if audio:
            try:
                text = await speech_to_text(audio, filename="video_audio.wav")
                text = (text or "").strip()
                if text:
                    segments.append({"start": start, "end": end, "text": text})
            except Exception as e:
                print(f"[WARN] 分段ASR失败 {start:.1f}-{end:.1f}: {e}")
        start = end

    return segments


def extract_frames_opencv(
    video_path: str,
    fps: float = 24,
    max_frames: int = 24,
    max_size: int = 720,
) -> List[bytes]:
    """备选方案：用 OpenCV 抽帧（不依赖 ffmpeg，但帧率控制不如 ffmpeg 精确）。"""
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        return []

    video_fps = cap.get(cv2.CAP_PROP_FPS) or 24
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

    # 计算采样步长，使抽帧帧率 = fps
    step = max(1, int(video_fps / fps))
    if total_frames > max_frames * step:
        # 帧太多，加大步长
        step = int(total_frames / max_frames)

    frames: List[bytes] = []
    frame_idx = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        if frame_idx % step == 0:
            # 缩放
            h, w = frame.shape[:2]
            if max(h, w) > max_size:
                ratio = max_size / max(h, w)
                frame = cv2.resize(frame, (int(w * ratio), int(h * ratio)))
            ok2, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
            if ok2:
                frames.append(buf.tobytes())
            if len(frames) >= max_frames:
                break
        frame_idx += 1

    cap.release()
    return frames


async def _transcribe_video_audio(video_path: str) -> str:
    """提取视频音频并转成文字（ASR）。无音频或识别失败返回空字符串。"""
    try:
        if not has_audio_stream(video_path):
            return ""
        audio = extract_audio(video_path)
        if not audio:
            return ""
        from voice_client import speech_to_text
        text = await speech_to_text(audio, filename="video_audio.wav")
        return (text or "").strip()
    except Exception as e:
        print(f"[WARN] 视频音频转文字失败: {e}")
        return ""


def _build_video_prompt(audio_segments: list) -> str:
    """构建视频理解 prompt，画面描述 + 带时间戳的音频文字。

    audio_segments: [{"start": 秒, "end": 秒, "text": 说话内容}]，按时间排序。
    """
    prompt = (
        "这是一段视频按时间顺序抽取的关键帧。请综合画面和声音，描述视频内容："
        "1) 视频里发生了什么；2) 主要人物/物体是谁、在做什么动作；"
        "3) 场景和背景；4) 画面上有什么文字（如有）。"
    )
    if audio_segments:
        prompt += "\n视频中按时间顺序的人声内容（语音识别，带时间点）：\n"
        for seg in audio_segments:
            prompt += f"  [{seg['start']:.1f}s~{seg['end']:.1f}s] {seg['text']}\n"
        prompt += (
            "请把各时间段的声音内容与对应时刻的画面综合起来理解，"
            "说明谁在说什么、同时画面在发生什么，做到声音和画面对应。"
        )
    prompt += "\n控制在 250 字以内，用中文回答。"
    return prompt


async def describe_video(video_path: str, prompt: str = "") -> str:
    """理解视频内容：抽帧 + 带时间戳的音频转文字 + 本地优先/云端兜底的多帧理解。

    声画同步：按时间段分段转写音频（带时间戳），
    让 AI 知道"第X秒说了什么"，再综合对应时刻的画面帧理解。

    流程：
    1. 分段提取音频转文字（ASR，带时间戳），让 AI 听到视频声音
    2. 抽帧（本地模式帧率更高，云端模式有 min_fps 保底）
    3. 优先用本地 Qwen2.5-VL 理解（快、免费、可高帧率）
    4. 本地失败或未启用时，回退云端 Gemini（分段理解）
    """
    # 分段提取视频音频并转文字（带时间戳，实现声画同步）
    audio_segments = []
    if getattr(config, "ENABLE_VIDEO_AUDIO", True):
        seg_len = getattr(config, "VIDEO_AUDIO_SEGMENT_LEN", 5.0)
        audio_segments = await _transcribe_audio_segments(video_path, segment_len=seg_len)
        if audio_segments:
            print(f"[INFO] 视频音频分段转文字 {len(audio_segments)} 段")

    # 云端优先：本机 GPU 被 vox TTS 等占满时，本地 Qwen2.5-VL 加载/推理会极慢甚至
    # 卡死（不抛异常、一直占用），导致整条视频回复卡在后台「不回」。故只要云端视觉
    # 可用（ENABLE_CLOUD_VL，依赖已配置的视觉 key）就先走云端；仅在云端彻底关闭时
    # 才回退本地 Qwen-VL。这样短视频也不会再触发本地模型加载卡死。
    use_local = bool(getattr(config, "ENABLE_LOCAL_VL", True)) and not bool(
        getattr(config, "ENABLE_CLOUD_VL", True)
    )
    max_frames = getattr(config, "VIDEO_MAX_FRAMES", 24)

    if use_local:
        # 本地模式：低分辨率 + 适度帧数（8GB 显存下避免 OOM）
        fps = getattr(config, "VIDEO_LOCAL_EXTRACT_FPS", 8)
        local_max_frames = getattr(config, "VIDEO_LOCAL_MAX_FRAMES", 12)
        local_size = getattr(config, "VIDEO_LOCAL_SIZE", 448)
        frames = extract_frames(
            video_path, fps=fps, max_frames=local_max_frames,
            min_fps=getattr(config, "VIDEO_MIN_FPS", 4.0),
            max_size=local_size,
        )
        if not frames:
            return "视频抽帧失败，可能是格式不支持。"

        local_prompt = prompt or _build_video_prompt(audio_segments)
        try:
            from local_video_understand import local_vl
            if len(frames) <= local_max_frames:
                return await local_vl.describe_frames(frames, prompt=local_prompt)
            # 本地也分段
            segments = _split_frames(frames, local_max_frames)
            descs = []
            for i, seg in enumerate(segments):
                d = await local_vl.describe_frames(seg, prompt=local_prompt)
                if d:
                    descs.append(d)
            if descs:
                return await _summarize_segments(descs, use_local=True)
        except Exception as e:
            print(f"[WARN] 本地模型理解失败，回退云端: {e}")

    # 云端兜底（统一视觉层：默认 Gemini 驱动，按 AI_VISION_ROUTING 故障转移）
    vision = get_vision()
    fps = getattr(config, "VIDEO_EXTRACT_FPS", 24)
    min_fps = getattr(config, "VIDEO_MIN_FPS", 4.0)

    frames = extract_frames(video_path, fps=fps, max_frames=max_frames, min_fps=min_fps)
    if not frames:
        return "视频抽帧失败，可能是格式不支持。"

    if not prompt:
        prompt = _build_video_prompt(audio_segments)

    if len(frames) <= max_frames:
        return await vision.describe_frames(frames, prompt=prompt, max_output_tokens=800)

    segments = _split_frames(frames, max_frames)
    segment_descriptions = []
    for i, seg in enumerate(segments):
        # 取该段画面对应时间段内的音频文字（按段序号估算时间范围）
        seg_audio = _audio_for_segment(audio_segments, i, len(segments))
        seg_prompt = (
            f"这是视频的第 {i+1}/{len(segments)} 段（按时间顺序抽取的关键帧）。"
            f"请简要描述这一段里发生了什么{('，该时间段的声音：' + seg_audio) if seg_audio else ''}。"
            "控制在 100 字以内，用中文回答。"
        )
        try:
            desc = await vision.describe_frames(seg, prompt=seg_prompt, max_output_tokens=500)
            if desc:
                segment_descriptions.append(desc)
        except Exception as e:
            print(f"[WARN] 视频第 {i+1} 段理解失败: {e}")

    if not segment_descriptions:
        return "视频识别失败。"

    return await _summarize_segments(segment_descriptions, use_local=False)


def _audio_for_segment(audio_segments: list, seg_index: int, total_segments: int) -> str:
    """根据画面段序号，把对应的音频文字返回（音频段按时间分布映射到画面段）。

    audio_segments: 带时间戳的音频段列表
    seg_index: 画面段序号（0起）
    total_segments: 画面总段数
    """
    if not audio_segments or total_segments <= 1:
        return ""
    # 该画面段对应的时间比例范围
    seg_start_ratio = seg_index / total_segments
    seg_end_ratio = (seg_index + 1) / total_segments

    texts = []
    # 音频段按列表占比近似映射到画面段（假设均匀分布）
    for idx, a in enumerate(audio_segments):
        a_ratio = (idx + 0.5) / len(audio_segments)
        if seg_start_ratio <= a_ratio < seg_end_ratio:
            texts.append(a["text"])
    return " | ".join(texts)


async def _summarize_segments(descriptions: List[str], use_local: bool) -> str:
    """把各段描述合并成连贯总结。use_local=True 用本地模型，否则用云端。"""
    summary_prompt = (
        "以下是把一段视频切分成多段后分别识别出的内容描述。"
        "请把它们整合成一段连贯的视频内容总结，按时间顺序描述整体发生了什么，"
        "控制在 200 字以内，用中文回答。\n\n"
    )
    for i, desc in enumerate(descriptions):
        summary_prompt += f"第{i+1}段：{desc}\n"

    if use_local:
        try:
            from local_video_understand import local_vl
            # 纯文本总结
            result = await local_vl.chat_text(summary_prompt)
            if result:
                return result
        except Exception as e:
            print(f"[WARN] 本地总结失败: {e}")

    # 云端总结兜底（统一视觉层，默认 Gemini 驱动的 chat_text）
    vision = get_vision()
    try:
        return await vision.chat_text(summary_prompt, max_output_tokens=800)
    except Exception as e:
        print(f"[WARN] 云端总结失败: {e}")
        return "\n".join(f"第{i+1}段：{d}" for i, d in enumerate(descriptions))


def _split_frames(frames: List[bytes], chunk_size: int) -> List[List[bytes]]:
    """把帧列表按 chunk_size 切分成多段。"""
    return [frames[i:i + chunk_size] for i in range(0, len(frames), chunk_size)]
