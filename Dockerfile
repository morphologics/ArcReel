# ============================================================
# Stage 1: build the frontend
# ============================================================
# ============================================================
# Stage 1: 构建前端
# ============================================================
FROM node:24-slim AS frontend-builder

WORKDIR /build/frontend

# Enable corepack; the pnpm version is pinned by the packageManager field in frontend/package.json.
# Turn off the interactive download confirmation, otherwise non-TTY environments such as docker build hang at
# "Corepack is about to download ..." until timeout.
# 启用 corepack；pnpm 版本由 frontend/package.json 的 packageManager 字段固定
# 关闭交互式下载确认，否则 docker build 这种非 TTY 环境会卡在
# "Corepack is about to download ..." 直到超时
ENV COREPACK_ENABLE_DOWNLOAD_PROMPT=0
RUN corepack enable

# Copy dependency files first to use the cache (corepack downloads the matching pnpm per the packageManager field).
# 先复制依赖文件，利用缓存（corepack 按 packageManager 字段自动下载对应 pnpm）
COPY frontend/package.json frontend/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile

# Copy the frontend source and build. / 复制前端源码并构建
COPY frontend/ ./
RUN pnpm build

# ============================================================
# Stage 2: production image
# ============================================================
# ============================================================
# Stage 2: 生产镜像
# ============================================================
FROM python:3.12-slim AS production

# Install system dependencies and upgrade the system packages preinstalled in the base image to the current repo versions.
# APT_REFRESH is passed in by CI per ISO week; when the value changes this layer's cache is invalidated, so system packages are refreshed regularly.
# 安装系统依赖，并把基础镜像预装的系统包升级到当前仓库版本。
# APT_REFRESH 由 CI 按 ISO 周传入，值变化时本层缓存失效，保证系统包定期刷新
ARG APT_REFRESH=unset
RUN echo "apt refresh: ${APT_REFRESH}" \
    && apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get upgrade -y \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    curl \
    bubblewrap \
    socat \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

# Upgrade the pip preinstalled in the base image: all dependencies are installed by uv and pip is not called at runtime,
# but the old pip bundled with python:3.12-slim is flagged with CVEs by image scanners, and upgrading clears those alerts.
# 升级基础镜像预装的 pip：依赖全部由 uv 安装、运行时不调用 pip，
# 但 python:3.12-slim 自带的旧 pip 会被镜像扫描器报 CVE，升级以清除这些告警
RUN python -m pip install --no-cache-dir --upgrade pip

# Install uv. / 安装 uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Disable Python output buffering so logs reach Docker logs in real time. / 禁用 Python 输出缓冲，确保日志实时输出到 Docker logs
ENV PYTHONUNBUFFERED=1

# Default timezone; can be overridden by docker-compose or runtime -e TZ=... / 默认时区，可由 docker-compose / 运行时 -e TZ=... 覆盖
ENV TZ=Asia/Shanghai

# Copy dependency and package metadata files first to use the cache; workspace subpackages are installed as dependencies in this step, so their source must be in place first.
# 先复制依赖和包元数据文件，利用缓存；workspace 子包作为依赖随这一步安装，源码须先就位
COPY pyproject.toml uv.lock README.md ./
COPY packages/ packages/
RUN uv sync --no-dev --no-install-project

# Copy the application code. / 复制应用代码
COPY lib/ lib/
COPY server/ server/
COPY alembic/ alembic/
COPY alembic.ini ./
COPY scripts/ scripts/
COPY agent_runtime_profile/ agent_runtime_profile/
COPY public/ public/
# The license notice for the bundled ffmpeg (see NOTICE) is distributed with the image. / 随包 ffmpeg 的许可证声明（见 NOTICE）随镜像分发
COPY LICENSE NOTICE ./
COPY LICENSES/ LICENSES/

# Copy the frontend build output. / 复制前端构建产物
COPY --from=frontend-builder /build/frontend/dist/ frontend/dist/

# Expose the port. / 暴露端口
EXPOSE 1241

# Health check. / 健康检查
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -f http://localhost:1241/health || exit 1

# Start command. / 启动命令
# Start the backend directly rather than through `uv run`: that stayed up as the backend's parent in every container
# (~57 MB each, seen 2026-11-16) and checks / syncs the environment on every start. Its environment is set here: the
# virtualenv first on PATH (any `python` the backend or the agent's shell starts is the app's), VIRTUAL_ENV, and /app
# importable from anywhere (as the installed project made it).
ENV VIRTUAL_ENV=/app/.venv \
    PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH=/app
CMD ["uvicorn", "server.app:app", "--host", "0.0.0.0", "--port", "1241"]
