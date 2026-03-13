# Fase 1: Costruzione
ARG PY_BUILD_VERS
FROM python:${PY_BUILD_VERS} AS build

WORKDIR /usr/src/app

# Install build dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    musl-dev \
    libffi-dev \
    cmake \
    pkg-config && \
    rm -rf /var/lib/apt/lists/*

# Installa dipendenze Python PRIMA di copiare il codice (cache layer)
COPY requirements.txt ./
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r requirements.txt --extra-index-url https://download.pytorch.org/whl/cpu

# Copia il resto del codice (layer separato, cambia più spesso)
COPY . .

# Fase 2: Esecuzione
ARG PY_BUILD_VERS
FROM python:${PY_BUILD_VERS}-slim AS final
ARG PY_BUILD_VERS

WORKDIR /usr/src/app

# Installa ffmpeg direttamente nella fase finale
RUN apt-get update && \
    apt-get install -y ffmpeg && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

COPY --from=build /usr/local/lib/python${PY_BUILD_VERS}/site-packages /usr/local/lib/python${PY_BUILD_VERS}/site-packages
COPY --from=build /usr/local/bin/gunicorn /usr/local/bin/gunicorn
COPY --from=build /usr/src/app /usr/src/app

VOLUME /data
ENV DB_NAME=slack.sqlite
ENV ARCHIVE_BOT_DATABASE_PATH=/data/$DB_NAME

ARG PORT=3333
ENV ARCHIVE_BOT_PORT=$PORT

ENV LOG_LEVEL=DEBUG
ENV ARCHIVE_BOT_LOG_LEVEL=$LOG_LEVEL

EXPOSE $PORT

CMD exec gunicorn flask_app:flask_app -c gunicorn_conf.py
