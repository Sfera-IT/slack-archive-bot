import os

from archivebot import init, start_link_enrichment_worker, stop_link_enrichment_worker

bind = f"0.0.0.0:{os.getenv('ARCHIVE_BOT_PORT', 3333)}"
workers = os.getenv("WORKERS", 4)
timeout = 300


def on_starting(server):
    init()


def post_fork(server, worker):
    start_link_enrichment_worker()


def worker_exit(server, worker):
    stop_link_enrichment_worker()
