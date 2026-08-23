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

ARG APP_VERSION=2.2.0
ARG APP_REVISION=unknown
ENV APP_VERSION=$APP_VERSION
ENV APP_REVISION=$APP_REVISION
# This image is the SferaIT deployment artifact. The non-secret workspace ID is
# pinned so the first secure release cannot fail solely because the legacy
# runtime never needed EXPECTED_TEAM_ID; operators may still override it.
ENV EXPECTED_TEAM_ID=T011MV24J1Y

WORKDIR /usr/src/app

# Installa ffmpeg direttamente nella fase finale
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg gosu && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

RUN groupadd --system archivebot && \
    useradd --system --gid archivebot --create-home --home-dir /home/archivebot \
    --shell /usr/sbin/nologin archivebot && \
    mkdir -p /data && \
    chown archivebot:archivebot /data

COPY --from=build /usr/local /usr/local
COPY --from=build /usr/src/app /usr/src/app
COPY --chmod=755 docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod -R a+rX /usr/src/app

VOLUME /data
ENV DEFAULT_DATABASE_PATH=/data/slack.sqlite
ENV PODCAST_AUDIO_PATH=/data/podcast.mp3
ENV XDG_CACHE_HOME=/data/.cache
ENV HF_HOME=/data/.cache/huggingface
ENV UV_CACHE_DIR=/data/.cache/uv
ENV HOME=/home/archivebot

ARG PORT=3333
ENV ARCHIVE_BOT_PORT=$PORT

ENV LOG_LEVEL=INFO
ENV ARCHIVE_BOT_LOG_LEVEL=$LOG_LEVEL

EXPOSE $PORT

HEALTHCHECK --interval=30s --timeout=5s --start-period=45s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.getenv('ARCHIVE_BOT_PORT', '3333') + '/healthz', timeout=3)" || exit 1

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["uv", "run", "--frozen", "--no-dev", "--no-sync", "gunicorn", "flask_app:flask_app", "-c", "gunicorn_conf.py"]
