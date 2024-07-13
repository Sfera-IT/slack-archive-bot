FROM python:3.9

WORKDIR /usr/src/app

# Install build dependencies
RUN apt-get update && apt-get install -y gcc musl-dev libffi-dev

COPY . /usr/src/app

RUN pip install --no-cache-dir -r requirements.txt

VOLUME /data
ENV DB_NAME=slack.sqlite
ENV ARCHIVE_BOT_DATABASE_PATH=/data/$DB_NAME

ARG PORT=3333
ENV ARCHIVE_BOT_PORT=$PORT

ENV LOG_LEVEL=DEBUG
ENV ARCHIVE_BOT_LOG_LEVEL=$LOG_LEVEL

EXPOSE $PORT

CMD exec gunicorn flask_app:flask_app -c gunicorn_conf.py