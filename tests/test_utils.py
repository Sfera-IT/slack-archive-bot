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


def test_migrate_db_creates_link_enrichment_tables_and_indexes():
    conn = sqlite3.connect(":memory:")
    cursor = conn.cursor()

    migrate_db(conn, cursor)

    tables = {
        row[0]
        for row in cursor.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    assert {
        "link_documents",
        "message_links",
        "link_enrichment_jobs",
        "link_duplicate_alerts",
        "link_match_scans",
    }.issubset(tables)

    job_columns = {
        row[1]
        for row in cursor.execute("PRAGMA table_info(link_enrichment_jobs)").fetchall()
    }
    assert {"claim_token", "recoveries", "claimed_at", "attempts"}.issubset(job_columns)

    message_link_columns = {
        row[1]
        for row in cursor.execute("PRAGMA table_info(message_links)").fetchall()
    }
    assert "deterministic_checked_at" in message_link_columns

    alert_columns = {
        row[1]
        for row in cursor.execute("PRAGMA table_info(link_duplicate_alerts)").fetchall()
    }
    assert {"current_normalized_url", "source_normalized_url"}.issubset(alert_columns)

    indexes = {
        row[0]
        for row in cursor.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index'"
        ).fetchall()
    }
    assert {
        "idx_link_documents_content_hash",
        "idx_link_documents_status_expiry",
        "idx_message_links_url_posted",
        "idx_message_links_thread",
        "idx_message_links_unchecked",
        "idx_link_enrichment_jobs_claim",
        "idx_link_duplicate_alerts_source",
        "idx_link_match_scans_claim",
    }.issubset(indexes)


def test_migrate_db_backfills_legacy_posted_links():
    conn = sqlite3.connect(":memory:")
    cursor = conn.cursor()
    migrate_db(conn, cursor)
    cursor.execute(
        "INSERT INTO channels(name, id, is_private) VALUES ('general', 'C1', 0)"
    )
    cursor.execute(
        """
        INSERT INTO messages
        (message, user, channel, timestamp, permalink, thread_ts, embeddings)
        VALUES ('story', 'U1', 'C1', '900.1', 'https://slack/message', '800.1', NULL)
        """
    )
    cursor.execute(
        """
        INSERT INTO posted_links
        (normalized_url, original_url, message_timestamp, channel, permalink, posted_date)
        VALUES ('https://example.com/story', 'https://example.com/story?utm=x',
                '900.1', 'C1', 'https://slack/message', '1970-01-01T00:15:00')
        """
    )
    conn.commit()

    migrate_db(conn, cursor)

    row = cursor.execute(
        """
        SELECT channel, message_timestamp, thread_ts, normalized_url, permalink, posted_at
        FROM message_links
        """
    ).fetchone()
    assert row == (
        "C1",
        "900.1",
        "800.1",
        "https://example.com/story",
        "https://slack/message",
        900.1,
    )


def test_migrate_db_scrubs_legacy_optout_data_without_retaining_pii_copy(tmp_path):
    database_path = tmp_path / "archive.sqlite"
    conn = sqlite3.connect(database_path)
    cursor = conn.cursor()
    migrate_db(conn, cursor)
    cursor.execute(
        "INSERT INTO users(name, id, avatar, real_name, display_name, email) "
        "VALUES ('Secret Name', 'U1', 'https://avatar', 'Real Name', 'Display', "
        "'secret@example.test')"
    )
    cursor.execute("INSERT INTO optout(user, timestamp) VALUES ('U1', 'now')")
    cursor.execute(
        """
        INSERT INTO messages
            (message, user, channel, timestamp, permalink, thread_ts, embeddings)
        VALUES (?, 'USLACKBOT', 'C1', '1710000000.000100', '',
                '1710000000.000100', X'01020304')
        """,
        ("User opted out of archiving. This message has been deleted",),
    )
    cursor.execute(
        "DELETE FROM privacy_migrations "
        "WHERE name = 'legacy_optout_artifacts_v2_2_0'"
    )
    conn.commit()

    assert migrate_db(conn, cursor) == 2

    assert not (tmp_path / "archive.sqlite.pre-v2.2.bak").exists()
    assert cursor.execute(
        "SELECT name, email FROM users WHERE id = 'U1'"
    ).fetchone() == ("Opted-out user", "")
    assert cursor.execute(
        "SELECT embeddings FROM messages WHERE timestamp = '1710000000.000100'"
    ).fetchone() == (None,)
    assert migrate_db(conn, cursor) == 0
    conn.close()
