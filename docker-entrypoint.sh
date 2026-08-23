#!/bin/sh
set -eu

# Existing releases ran as root, so an already-populated Coolify volume can
# contain root-owned SQLite/WAL files. Repair only the dedicated data volume,
# then drop privileges permanently before Gunicorn starts.
if [ "$(id -u)" = "0" ]; then
    mkdir -p /data/.cache
    chown -R archivebot:archivebot /data
    exec gosu archivebot "$@"
fi

exec "$@"
