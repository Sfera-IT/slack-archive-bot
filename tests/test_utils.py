import os
import sqlite3
import sys


ROOT_DIR = os.path.dirname(os.path.dirname(__file__))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from utils import claim_xcancel_alert, finalize_xcancel_alert, migrate_db


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


def test_claim_xcancel_alert_only_first_claim_wins():
    conn = sqlite3.connect(":memory:")
    cursor = conn.cursor()
    migrate_db(conn, cursor)

    assert claim_xcancel_alert(cursor, "1710000000.000100", "C123", "alert") is True
    # Un secondo claim per lo stesso messaggio (evento message vs message_changed
    # dell'unfurl, o retry di Slack) non deve vincere.
    assert claim_xcancel_alert(cursor, "1710000000.000100", "C123", "alert") is False


def test_claim_xcancel_alert_is_scoped_per_message_and_channel():
    conn = sqlite3.connect(":memory:")
    cursor = conn.cursor()
    migrate_db(conn, cursor)

    assert claim_xcancel_alert(cursor, "1710000000.000100", "C123", "alert") is True
    assert claim_xcancel_alert(cursor, "1710000000.000100", "C456", "alert") is True
    assert claim_xcancel_alert(cursor, "1710000000.000200", "C123", "alert") is True


def test_claim_xcancel_alert_can_be_reclaimed_after_delete():
    conn = sqlite3.connect(":memory:")
    cursor = conn.cursor()
    migrate_db(conn, cursor)

    assert claim_xcancel_alert(cursor, "1710000000.000100", "C123", "alert") is True
    cursor.execute(
        "DELETE FROM xcancel_alerts WHERE parent_message_ts = ? AND channel = ?",
        ("1710000000.000100", "C123"),
    )
    assert claim_xcancel_alert(cursor, "1710000000.000100", "C123", "alert") is True


def test_finalize_xcancel_alert_updates_claimed_slot():
    conn = sqlite3.connect(":memory:")
    cursor = conn.cursor()
    migrate_db(conn, cursor)

    claim_xcancel_alert(cursor, "1710000000.000100", "C123", "alert")

    assert finalize_xcancel_alert(
        cursor, "1710000000.000100", "1710000001.000500", "C123", "alert"
    ) is True
    row = cursor.execute(
        "SELECT alert_message_ts FROM xcancel_alerts WHERE parent_message_ts = ?",
        ("1710000000.000100",),
    ).fetchone()
    assert row == ("1710000001.000500",)


def test_finalize_xcancel_alert_fails_if_claim_was_removed():
    conn = sqlite3.connect(":memory:")
    cursor = conn.cursor()
    migrate_db(conn, cursor)

    claim_xcancel_alert(cursor, "1710000000.000100", "C123", "alert")
    # Il messaggio parent viene cancellato mentre il post dell'alert è in corso.
    cursor.execute("DELETE FROM xcancel_alerts")

    assert finalize_xcancel_alert(
        cursor, "1710000000.000100", "1710000001.000500", "C123", "alert"
    ) is False


def test_finalize_xcancel_alert_fails_if_claim_was_replaced():
    conn = sqlite3.connect(":memory:")
    cursor = conn.cursor()
    migrate_db(conn, cursor)

    claim_xcancel_alert(cursor, "1710000000.000100", "C123", "old alert")
    # Il testo del messaggio è cambiato durante il post: la riserva è stata
    # sostituita da sync_xcancel_alternatives_for_message con un nuovo testo.
    cursor.execute("DELETE FROM xcancel_alerts")
    claim_xcancel_alert(cursor, "1710000000.000100", "C123", "new alert")

    assert finalize_xcancel_alert(
        cursor, "1710000000.000100", "1710000001.000500", "C123", "old alert"
    ) is False
    # La riserva nuova non deve essere stata toccata.
    row = cursor.execute("SELECT alert_message_ts, alert_text FROM xcancel_alerts").fetchone()
    assert row == ("", "new alert")


def test_finalize_xcancel_alert_does_not_overwrite_completed_alert():
    conn = sqlite3.connect(":memory:")
    cursor = conn.cursor()
    migrate_db(conn, cursor)

    claim_xcancel_alert(cursor, "1710000000.000100", "C123", "alert")
    finalize_xcancel_alert(
        cursor, "1710000000.000100", "1710000001.000500", "C123", "alert"
    )

    assert finalize_xcancel_alert(
        cursor, "1710000000.000100", "1710000002.000900", "C123", "alert"
    ) is False


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
