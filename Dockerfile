# 使用官方 uv 镜像作为基础镜像
FROM ghcr.io/astral-sh/uv:python3.10-bookworm-slim

# 设置工作目录
WORKDIR /app

# 设置环境变量
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# 安装系统依赖（包括 curl 用于健康检查）
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# 复制项目文件
COPY pyproject.toml uv.lock ./
COPY app ./app
COPY templates ./templates
COPY README.md ./

# 安装依赖
RUN uv sync --frozen --no-dev

# 暴露端口
EXPOSE 5111

# 创建数据目录
RUN mkdir -p /app/data /app/config /app/logs

# 健康检测
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:5111/health || exit 1

# 运行应用
CMD ["uv", "run", "bs"]
