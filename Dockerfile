# Fase 1: Costruzione
FROM python:3.9 AS build

WORKDIR /usr/src/app

# Install build dependencies
RUN apt-get update && apt-get install -y gcc musl-dev libffi-dev ffmpeg && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
# remove cuda stuff for size optimization
RUN pip install --no-cache-dir -r requirements.txt --extra-index-url https://download.pytorch.org/whl/cpu

COPY . .

# Fase 2: Esecuzione
FROM python:3.9-slim AS final

WORKDIR /usr/src/app

COPY --from=build /usr/src/app /usr/src/app
COPY --from=build /usr/local/lib/python3.9/site-packages /usr/local/lib/python3.9/site-packages
COPY --from=build /usr/local/bin/gunicorn /usr/local/bin/gunicorn

VOLUME /data
ENV DB_NAME=slack.sqlite
ENV ARCHIVE_BOT_DATABASE_PATH=/data/$DB_NAME

ARG PORT=3333
ENV ARCHIVE_BOT_PORT=$PORT

ENV LOG_LEVEL=DEBUG
ENV ARCHIVE_BOT_LOG_LEVEL=$LOG_LEVEL

EXPOSE $PORT

CMD exec gunicorn flask_app:flask_app -c gunicorn_conf.py