# Fase 1: Costruzione
ARG PY_BUILD_VERS=3.11
FROM ghcr.io/astral-sh/uv:0.11.24 AS uv
FROM python:${PY_BUILD_VERS}-slim AS build

WORKDIR /usr/src/app
ENV UV_COMPILE_BYTECODE=1
ENV UV_LINK_MODE=copy
ENV UV_PROJECT_ENVIRONMENT=/usr/local

# Install build dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    musl-dev \
    libffi-dev \
    cmake \
    pkg-config && \
    rm -rf /var/lib/apt/lists/*

# Installa dipendenze Python PRIMA di copiare il codice (cache layer)
COPY --from=uv /uv /uvx /usr/local/bin/
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# Copia il resto del codice (layer separato, cambia più spesso)
COPY . .
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# Fase 2: Esecuzione
ARG PY_BUILD_VERS
FROM python:${PY_BUILD_VERS}-slim AS final
ENV UV_PROJECT_ENVIRONMENT=/usr/local

WORKDIR /usr/src/app

# Installa ffmpeg direttamente nella fase finale
RUN apt-get update && \
    apt-get install -y ffmpeg && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

COPY --from=build /usr/local /usr/local
COPY --from=build /usr/src/app /usr/src/app

VOLUME /data
ENV DB_NAME=slack.sqlite
ENV ARCHIVE_BOT_DATABASE_PATH=/data/$DB_NAME

ARG PORT=3333
ENV ARCHIVE_BOT_PORT=$PORT

ENV LOG_LEVEL=DEBUG
ENV ARCHIVE_BOT_LOG_LEVEL=$LOG_LEVEL

EXPOSE $PORT

CMD exec uv run --frozen --no-dev --no-sync gunicorn flask_app:flask_app -c gunicorn_conf.py
