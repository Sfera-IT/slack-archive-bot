import os
import sqlite3
import sys


ROOT_DIR = os.path.dirname(os.path.dirname(__file__))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from utils import migrate_db


def test_migrate_db_creates_xcancel_alerts_table():
    conn = sqlite3.connect(":memory:")
    cursor = conn.cursor()

    migrate_db(conn, cursor)

    columns = {
        row[1]
        for row in cursor.execute("PRAGMA table_info(xcancel_alerts)").fetchall()
    }
    assert columns == {
        "parent_message_ts",
        "alert_message_ts",
        "channel",
        "alert_text",
    }


def test_migrate_db_creates_hot_path_indexes():
    conn = sqlite3.connect(":memory:")
    cursor = conn.cursor()

    migrate_db(conn, cursor)

    indexes = {
        row[1]
        for row in cursor.execute(
            "SELECT type, name FROM sqlite_master WHERE type = 'index'"
        ).fetchall()
    }

    assert {
        "idx_messages_thread_channel",
        "idx_messages_user",
        "idx_messages_timestamp",
        "idx_messages_embedded_timestamp",
        "idx_members_channel_user",
        "idx_posted_links_normalized_posted_date",
    }.issubset(indexes)
