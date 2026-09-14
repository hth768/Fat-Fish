# 肥鱼娘 · Web 控制台镜像（参考猫娘计划的 Docker 化思路）
#
# 仅容器化跨平台的「网页控制台」(web_plugin.py)。
# 重型 / Windows 专属能力（MC、PVZ、B 站、本地视觉、TTS、NapCat/QQ 等）
# 依赖 Windows 运行环境，不在本镜像内启用（默认 --no-core 只读模式）。
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai

RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 仅复制网页控制台与其依赖的源码（避免拉入大体积资源 / 重型依赖）
COPY config.py plugin_base.py message_bus.py file_lock.py ./
COPY web_plugin.py telemetry.py telemetry_server.py ./
COPY reflection_memory.py persona_memory.py important_notes.py ./
COPY ai_provider.py agent_core.py launcher.py ./
COPY webui/ ./webui/

# 网页控制台（只读 / 无核心模式，跨平台安全，监听 0.0.0.0:8800）
EXPOSE 8800
CMD ["python", "web_plugin.py", "--no-core", "--port", "8800"]
