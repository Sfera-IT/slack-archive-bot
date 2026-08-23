"""Shared, transactional privacy operations for archived Slack users."""

from __future__ import annotations

import sqlite3

from archive_search import OPTED_OUT_TEXT


LEGACY_OPTOUT_MIGRATION = 'legacy_optout_artifacts_v2_2_0'


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table_name,),
        ).fetchone()
        is not None
    )


def _migration_applied(conn: sqlite3.Connection) -> bool:
    if not _table_exists(conn, 'privacy_migrations'):
        return False
    return (
        conn.execute(
            'SELECT 1 FROM privacy_migrations WHERE name = ?',
            (LEGACY_OPTOUT_MIGRATION,),
        ).fetchone()
        is not None
    )


def _legacy_cleanup_counts(conn: sqlite3.Connection) -> tuple[int, int]:
    if not _table_exists(conn, 'messages'):
        return 0, 0
    legacy_messages = conn.execute(
        'SELECT COUNT(*) FROM messages WHERE user = ? AND message = ?',
        ('USLACKBOT', OPTED_OUT_TEXT),
    ).fetchone()[0]
    directory_rows = 0
    if _table_exists(conn, 'users') and _table_exists(conn, 'optout'):
        user_columns = {
            row[1] for row in conn.execute('PRAGMA table_info(users)').fetchall()
        }
        optional_checks = [
            f"COALESCE({name}, '') != ''"  # nosec B608 -- PRAGMA-derived allowlist.
            for name in ('avatar', 'real_name', 'display_name', 'email')
            if name in user_columns
        ]
        name_check = "COALESCE(name, '') != 'Opted-out user'"
        profile_check = ' OR '.join([name_check, *optional_checks])
        directory_rows = conn.execute(
            f'''
            SELECT COUNT(*) FROM users
            WHERE id IN (SELECT user FROM optout)
              AND ({profile_check})
            '''  # nosec B608 -- columns come from the fixed allowlist above.
        ).fetchone()[0]
    return int(legacy_messages), int(directory_rows)


def purge_archived_user_data(conn: sqlite3.Connection, user_id: str) -> None:
    """Purge archived and derived content before replacing the message owner.

    The caller owns the transaction and must commit or roll it back. Keeping this
    operation shared prevents the web and admin-DM opt-out paths from diverging.
    """
    conn.execute(
        '''
        INSERT INTO optout (user, timestamp)
        SELECT ?, CURRENT_TIMESTAMP
        WHERE NOT EXISTS (SELECT 1 FROM optout WHERE user = ?)
        ''',
        (user_id, user_id),
    )
    conn.execute(
        '''
        DELETE FROM link_duplicate_alerts
        WHERE EXISTS (
            SELECT 1 FROM messages
            WHERE messages.user = ?
              AND (
                  (messages.channel = link_duplicate_alerts.current_channel
                   AND messages.timestamp = link_duplicate_alerts.current_message_ts)
                  OR
                  (messages.channel = link_duplicate_alerts.source_channel
                   AND messages.timestamp = link_duplicate_alerts.source_message_ts)
              )
        )
        ''',
        (user_id,),
    )
    conn.execute(
        '''
        DELETE FROM duplicate_alerts
        WHERE EXISTS (
            SELECT 1 FROM messages
            WHERE messages.user = ?
              AND messages.channel = duplicate_alerts.channel
              AND messages.timestamp = duplicate_alerts.parent_message_ts
        )
        ''',
        (user_id,),
    )
    conn.execute(
        '''
        DELETE FROM xcancel_alerts
        WHERE EXISTS (
            SELECT 1 FROM messages
            WHERE messages.user = ?
              AND messages.channel = xcancel_alerts.channel
              AND messages.timestamp = xcancel_alerts.parent_message_ts
        )
        ''',
        (user_id,),
    )
    conn.execute(
        '''
        DELETE FROM posted_links
        WHERE EXISTS (
            SELECT 1 FROM messages
            WHERE messages.user = ?
              AND messages.channel = posted_links.channel
              AND messages.timestamp = posted_links.message_timestamp
        )
        ''',
        (user_id,),
    )
    conn.execute(
        '''
        DELETE FROM message_links
        WHERE EXISTS (
            SELECT 1 FROM messages
            WHERE messages.user = ?
              AND messages.channel = message_links.channel
              AND messages.timestamp = message_links.message_timestamp
        )
        ''',
        (user_id,),
    )
    # A resumable scan can contain a serialized best candidate. The queue is
    # intentionally small, so resetting it is safer than retaining source text.
    conn.execute('DELETE FROM link_match_scans')
    conn.execute(
        '''
        DELETE FROM link_enrichment_jobs
        WHERE normalized_url NOT IN (SELECT normalized_url FROM message_links)
        '''
    )
    conn.execute(
        '''
        DELETE FROM link_documents
        WHERE normalized_url NOT IN (SELECT normalized_url FROM message_links)
        '''
    )
    conn.execute(
        '''
        UPDATE messages
        SET message = ?, user = 'USLACKBOT', permalink = '', embeddings = NULL
        WHERE user = ?
        ''',
        (OPTED_OUT_TEXT, user_id),
    )
    conn.execute(
        '''
        UPDATE users
        SET name = 'Opted-out user', avatar = '', real_name = '',
            display_name = '', email = ''
        WHERE id = ?
        ''',
        (user_id,),
    )
    conn.execute('DELETE FROM ai_requests WHERE user_id = ?', (user_id,))
    if _table_exists(conn, 'web_ai_requests'):
        conn.execute('DELETE FROM web_ai_requests WHERE user_id = ?', (user_id,))
    # Saved prompts and generated media can contain the original message text.
    conn.execute('DELETE FROM digest_details')
    conn.execute('DELETE FROM digests')


def scrub_legacy_optout_artifacts(conn: sqlite3.Connection) -> int:
    """One-time cleanup for partial opt-outs created by releases before v2.2."""
    conn.execute(
        '''
        CREATE TABLE IF NOT EXISTS privacy_migrations (
            name TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        '''
    )
    if _migration_applied(conn):
        return 0

    params = ('USLACKBOT', OPTED_OUT_TEXT)
    legacy_count, directory_count = _legacy_cleanup_counts(conn)
    conn.execute(
        '''
        UPDATE users
        SET name = 'Opted-out user', avatar = '', real_name = '',
            display_name = '', email = ''
        WHERE id IN (SELECT user FROM optout)
        '''
    )
    if legacy_count:
        conn.execute(
            '''
            DELETE FROM link_duplicate_alerts
            WHERE EXISTS (
                SELECT 1 FROM messages
                WHERE messages.user = ? AND messages.message = ?
                  AND messages.channel = link_duplicate_alerts.current_channel
                  AND messages.timestamp = link_duplicate_alerts.current_message_ts
            ) OR EXISTS (
                SELECT 1 FROM messages
                WHERE messages.user = ? AND messages.message = ?
                  AND messages.channel = link_duplicate_alerts.source_channel
                  AND messages.timestamp = link_duplicate_alerts.source_message_ts
            )
            ''',
            (*params, *params),
        )
        conn.execute(
            '''
            DELETE FROM duplicate_alerts
            WHERE EXISTS (
                SELECT 1 FROM messages
                WHERE messages.user = ? AND messages.message = ?
                  AND messages.channel = duplicate_alerts.channel
                  AND messages.timestamp = duplicate_alerts.parent_message_ts
            )
            ''',
            params,
        )
        conn.execute(
            '''
            DELETE FROM xcancel_alerts
            WHERE EXISTS (
                SELECT 1 FROM messages
                WHERE messages.user = ? AND messages.message = ?
                  AND messages.channel = xcancel_alerts.channel
                  AND messages.timestamp = xcancel_alerts.parent_message_ts
            )
            ''',
            params,
        )
        conn.execute(
            '''
            DELETE FROM posted_links
            WHERE EXISTS (
                SELECT 1 FROM messages
                WHERE messages.user = ? AND messages.message = ?
                  AND messages.channel = posted_links.channel
                  AND messages.timestamp = posted_links.message_timestamp
            )
            ''',
            params,
        )
        conn.execute(
            '''
            DELETE FROM message_links
            WHERE EXISTS (
                SELECT 1 FROM messages
                WHERE messages.user = ? AND messages.message = ?
                  AND messages.channel = message_links.channel
                  AND messages.timestamp = message_links.message_timestamp
            )
            ''',
            params,
        )
        conn.execute(
            '''
            DELETE FROM link_match_scans
            WHERE EXISTS (
                SELECT 1 FROM messages
                WHERE messages.user = ? AND messages.message = ?
                  AND messages.channel = link_match_scans.current_channel
                  AND messages.timestamp = link_match_scans.current_message_ts
            )
            ''',
            params,
        )
        conn.execute(
            'UPDATE messages SET embeddings = NULL WHERE user = ? AND message = ?',
            params,
        )
        conn.execute(
            '''
            DELETE FROM link_enrichment_jobs
            WHERE normalized_url NOT IN (SELECT normalized_url FROM message_links)
            '''
        )
        conn.execute(
            '''
            DELETE FROM link_documents
            WHERE normalized_url NOT IN (SELECT normalized_url FROM message_links)
            '''
        )
        conn.execute('DELETE FROM digest_details')
        conn.execute('DELETE FROM digests')

    conn.execute(
        'INSERT INTO privacy_migrations(name) VALUES (?)',
        (LEGACY_OPTOUT_MIGRATION,),
    )
    return legacy_count + directory_count
