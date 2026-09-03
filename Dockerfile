FROM python:3.14-slim-trixie

COPY --from=ghcr.io/astral-sh/uv:latest \
    /uv /uvx /bin/

ARG TARGETARCH

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
        amd64|arm64) ;; \
        *) echo "Unsupported container architecture: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    curl -fsSL \
        -o /usr/local/bin/supercronic \
        "https://github.com/aptible/supercronic/releases/latest/download/supercronic-linux-${TARGETARCH}"; \
    chmod 0755 /usr/local/bin/supercronic; \
    rm -rf /var/lib/apt/lists/*

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

COPY --chown=app:app pyproject.toml ./

USER app:app

RUN uv venv /app/.venv \
    && uv pip install \
        --python /app/.venv \
        --upgrade \
        --no-cache \
        --requirements pyproject.toml

COPY --chown=app:app main.py config.toml ./

RUN mkdir -p /app/data

CMD ["/app/.venv/bin/python", "/app/main.py", "--config", "/app/config.toml"]
