# syntax=docker/dockerfile:1.7
FROM python:3.14-slim-trixie

COPY --from=ghcr.io/astral-sh/uv:0.11.28@sha256:0f36cb9361a3346885ca3677e3767016687b5a170c1a6b88465ec14aefec90aa \
    /uv /uvx /bin/

ARG TARGETARCH
ARG SUPERCRONIC_VERSION=v0.2.47

ARG APP_UID=1000
ARG APP_GID=1000

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0 \
    PATH="/app/.venv/bin:${PATH}" \
    HOME=/home/app \
    CHROME_BIN=/usr/bin/chromium \
    CHROMEDRIVER_PATH=/usr/bin/chromedriver

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        bash \
        ca-certificates \
        chromium \
        chromium-driver \
        curl \
        tzdata; \
    case "${TARGETARCH}" in \
        amd64) checksum="dcb1403c188a9438c47d4bba82a9c357fc9351ce91627fb2bae627f0f5becfc4" ;; \
        arm64) checksum="e1124aa34294e2bb8ab7002f347f4363ba35097f3daf4d3c44e9d813c1fb2bb8" ;; \
        *) echo "Unsupported container architecture: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    curl -fsSL \
        -o /usr/local/bin/supercronic \
        "https://github.com/aptible/supercronic/releases/download/${SUPERCRONIC_VERSION}/supercronic-linux-${TARGETARCH}"; \
    echo "${checksum}  /usr/local/bin/supercronic" | sha256sum -c -; \
    chmod 0755 /usr/local/bin/supercronic; \
    rm -rf /var/lib/apt/lists/*

# 创建与宿主机相同 UID/GID 的 app 用户
RUN set -eux; \
    groupadd --gid "${APP_GID}" app; \
    useradd \
        --uid "${APP_UID}" \
        --gid "${APP_GID}" \
        --create-home \
        --home-dir /home/app \
        --shell /bin/bash \
        --no-log-init \
        app; \
    mkdir -p /app /app/data; \
    chown -R app:app /app /home/app

WORKDIR /app

# 显式指定复制文件的所有者
COPY --chown=app:app pyproject.toml uv.lock ./

# 从这里开始，构建步骤也以 app 用户运行
USER app:app

RUN uv sync --no-dev --no-install-project --no-cache

COPY --chown=app:app main.py config.toml ./

RUN mkdir -p /app/data

CMD ["/app/.venv/bin/python", "/app/main.py", "--config", "/app/config.toml"]
