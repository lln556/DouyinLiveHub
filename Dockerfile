# 抖音直播监控平台 Dockerfile
# 使用 uv 管理 Python 依赖

ARG PYTHON_IMAGE=python:3.11-slim
ARG UV_IMAGE=ghcr.io/astral-sh/uv:latest

# ==================== 工具镜像 ====================
FROM ${UV_IMAGE} AS uv

# ==================== 构建阶段 ====================
FROM ${PYTHON_IMAGE} AS builder

# 设置环境变量
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# 从官方镜像复制 uv
COPY --from=uv /uv /uvx /bin/

# 使用国内 Debian 镜像源
RUN sed -i 's|deb.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list.d/debian.sources

# 安装构建依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    gcc \
    g++ \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 复制依赖文件
COPY pyproject.toml uv.lock* ./

# 使用 uv 创建虚拟环境并安装依赖
RUN uv sync --frozen --no-dev --no-install-project

# ==================== 运行阶段 ====================
FROM ${PYTHON_IMAGE}

# 设置环境变量
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai \
    DEBUG=False \
    LOG_LEVEL=INFO \
    PATH="/app/.venv/bin:$PATH"

# 使用国内 Debian 镜像源
RUN sed -i 's|deb.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list.d/debian.sources

# 安装运行时依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    nodejs \
    tzdata \
    curl \
    && rm -rf /var/lib/apt/lists/* \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# 设置工作目录
WORKDIR /app

# 从构建阶段复制虚拟环境
COPY --from=builder /app/.venv /app/.venv

# 复制应用代码
COPY . .

# 创建必要的目录
RUN mkdir -p /app/data /app/logs

# 暴露端口
EXPOSE 7654

# 健康检查
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:7654/healthz || exit 1

# 启动命令
CMD ["python", "app.py"]
