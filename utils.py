import sqlite3


def migrate_db(conn, cursor):
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS messages (
            message TEXT,
            user TEXT,
            channel TEXT,
            timestamp TEXT,
            permalink TEXT,
            UNIQUE(channel, timestamp) ON CONFLICT REPLACE
        )
    """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            name TEXT,
            id TEXT,
            avatar TEXT,
            UNIQUE(id) ON CONFLICT REPLACE
    )"""
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS channels (
            name TEXT,
            id TEXT,
            is_private BOOLEAN NOT NULL CHECK (is_private IN (0,1)),
            UNIQUE(id) ON CONFLICT REPLACE
    )"""
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS members (
            channel TEXT,
            user TEXT,
            FOREIGN KEY (channel) REFERENCES channels(id),
            FOREIGN KEY (user) REFERENCES users(id)
        )
    """
    )

    # Membership is a set. v2.2.0 already made this index UNIQUE in production,
    # while rollback-era fresh databases recreated it as non-unique. Normalize
    # only when needed so normal boots do not repeat DDL or data rewrites.
    member_indexes = {
        row[1]: bool(row[2])
        for row in cursor.execute("PRAGMA index_list('members')").fetchall()
    }
    if not member_indexes.get("idx_members_channel_user", False):
        cursor.execute(
            """
            DELETE FROM members
            WHERE rowid NOT IN (
                SELECT MIN(rowid) FROM members GROUP BY channel, user
            )
            """
        )
        if "idx_members_channel_user" in member_indexes:
            cursor.execute("DROP INDEX idx_members_channel_user")
        cursor.execute(
            """
            CREATE UNIQUE INDEX idx_members_channel_user
            ON members(channel, user)
            """
        )
    conn.commit()

    # Add `is_private` to channels for dbs that existed in v0.1
    try:
        cursor.execute(
            """
            ALTER TABLE channels
            ADD COLUMN is_private BOOLEAN default 1
            NOT NULL CHECK (is_private IN (0,1))
        """
        )
        conn.commit()
    except:
        pass


    # Add `thread_ts` to messages
    try:
        cursor.execute(
            """
            ALTER TABLE messages
            ADD COLUMN thread_ts TEXT default NULL
        """
        )
        conn.commit()
    except:
        pass

    # opt out table
    try:
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS optout (
                user TEXT,
                timestamp TEXT,
                FOREIGN KEY (user) REFERENCES users(id)
                UNIQUE(user, timestamp) ON CONFLICT REPLACE
            )
        """
        )
        conn.commit()
    except:
        pass

    # Add `embeddings` to messages
    try:
        cursor.execute(
            """
            ALTER TABLE messages
            ADD COLUMN embeddings BLOB default NULL
        """
        )
        conn.commit()
    except:
        pass

    # Index hot read/write paths. Keep this set small: these match existing
    # filters and joins used by archive browsing, search, stats, and link checks.
    for index_sql in [
        """
        CREATE INDEX IF NOT EXISTS idx_messages_thread_channel
        ON messages(thread_ts, channel)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_messages_user
        ON messages(user)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_messages_timestamp
        ON messages(timestamp)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_messages_embedded_timestamp
        ON messages(timestamp)
        WHERE embeddings IS NOT NULL
        """,
    ]:
        try:
            cursor.execute(index_sql)
            conn.commit()
        except:
            pass

    # digests table
    try:
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS digests (
                timestamp TEXT NOT NULL,
                period TEXT NOT NULL,
                digest TEXT NOT NULL
            )
        """
        )
        conn.commit()
    except:
        pass

    # add posts to digests
    try:
        cursor.execute(
            """
            ALTER TABLE digests
            ADD COLUMN posts TEXT
        """
        )
        conn.commit()
    except:
        pass

    # opt out from ai table
    try:
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS optout_ai (
                user TEXT,
                timestamp TEXT,
                FOREIGN KEY (user) REFERENCES users(id)
                UNIQUE(user, timestamp) ON CONFLICT REPLACE
            )
        """
        )
        conn.commit()
    except:
        pass

    # digest_details
    try:
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS digest_details (
                user_id TEXT NOT NULL,
                query TEXT NOT NULL,
                details TEXT NOT NULL,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                digest_timestamp TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
            """
        )
        conn.commit()
    except Exception as e:
        print(f"Error creating digest_details table: {e}")
        pass

    # Aggiungi is_deleted a users
    try:
        cursor.execute(
            """
            ALTER TABLE users
            ADD COLUMN is_deleted BOOLEAN DEFAULT FALSE
            """
        )
        conn.commit()
    except:
        pass

    # Add real_name, display_name, and email to users
    try:
        cursor.execute(
            """
            ALTER TABLE users
            ADD COLUMN real_name TEXT
            """
        )
        cursor.execute(
            """
            ALTER TABLE users
            ADD COLUMN display_name TEXT
            """
        )
        cursor.execute(
            """
            ALTER TABLE users
            ADD COLUMN email TEXT
            """
        )
        conn.commit()
    except:
        pass

    # Aggiungi la colonna podcast_content alla tabella digests
    try:
        cursor.execute('''
        ALTER TABLE digests
        ADD COLUMN podcast_content TEXT
        ''')
        conn.commit()
    except:
        pass

    # Tabella per tracciare i link postati
    try:
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS posted_links (
                normalized_url TEXT NOT NULL,
                original_url TEXT NOT NULL,
                message_timestamp TEXT NOT NULL,
                channel TEXT NOT NULL,
                permalink TEXT NOT NULL,
                posted_date DATETIME DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (normalized_url, message_timestamp)
            )
        """
        )
        conn.commit()
    except:
        pass

    # Aggiungi colonna duplicate_notified per tracciare se un link è già stato segnalato come duplicato
    try:
        cursor.execute(
            """
            ALTER TABLE posted_links
            ADD COLUMN duplicate_notified BOOLEAN DEFAULT 0
            NOT NULL CHECK (duplicate_notified IN (0,1))
        """
        )
        conn.commit()
    except:
        pass

    # Tabella per gli utenti clown (condivisa tra worker)
    try:
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS clown_users (
                nickname TEXT NOT NULL PRIMARY KEY,
                expiry_date TEXT NOT NULL
            )
        """
        )
        conn.commit()
    except:
        pass

    # Aggiungi colonne di tracking a clown_users (origine, autore, motivo)
    for col_def in [
        "source TEXT",
        "assigned_by TEXT",
        "assigned_at REAL",
        "reason TEXT",
        "thread_ts TEXT",
        "channel TEXT",
    ]:
        try:
            cursor.execute(f"ALTER TABLE clown_users ADD COLUMN {col_def}")
            conn.commit()
        except:
            pass

    # Tabella per il throttle delle richieste AI (condivisa tra worker)
    try:
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS ai_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                user_id TEXT NOT NULL,
                channel TEXT NOT NULL
            )
        """
        )
        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_ai_requests_timestamp ON ai_requests(timestamp)
        """
        )
        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_ai_requests_user_timestamp ON ai_requests(user_id, timestamp)
        """
        )
        conn.commit()
    except:
        pass
    
    # Tabella per tracciare gli alert di link duplicati (per cancellarli se il messaggio parent viene cancellato)
    try:
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS duplicate_alerts (
                parent_message_ts TEXT NOT NULL,
                alert_message_ts TEXT NOT NULL,
                channel TEXT NOT NULL,
                PRIMARY KEY (parent_message_ts, channel)
            )
        """
        )
        conn.commit()
    except:
        pass

    try:
        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_posted_links_normalized_posted_date
            ON posted_links(normalized_url, posted_date)
        """
        )
        conn.commit()
    except:
        pass

    # Tabella per tracciare gli alert xcancel (per cancellarli se il messaggio parent viene cancellato/modificato)
    try:
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS xcancel_alerts (
                parent_message_ts TEXT NOT NULL,
                alert_message_ts TEXT NOT NULL,
                channel TEXT NOT NULL,
                alert_text TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (parent_message_ts, channel)
            )
        """
        )
        conn.commit()
    except:
        pass

    # Aggiungi il testo atteso agli alert xcancel esistenti per gestire update idempotenti.
    try:
        cursor.execute(
            """
            ALTER TABLE xcancel_alerts
            ADD COLUMN alert_text TEXT NOT NULL DEFAULT ''
            """
        )
        conn.commit()
    except:
        pass

    # Durable Instagram-media queue. Rows transition to ``uploading`` before
    # Slack publication; that state is intentionally never auto-reclaimed,
    # because retrying an interrupted upload could publish duplicate files.
    jobs_exists = cursor.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'instagram_media_jobs'"
    ).fetchone() is not None
    legacy_jobs_table = None
    if jobs_exists:
        pk_columns = [
            row[1]
            for row in sorted(
                (row for row in cursor.execute("PRAGMA table_info(instagram_media_jobs)") if row[5]),
                key=lambda row: row[5],
            )
        ]
        if pk_columns != ["channel", "thread_ts", "shortcode"]:
            legacy_jobs_table = "instagram_media_jobs_legacy"
            cursor.execute("DROP INDEX IF EXISTS idx_instagram_media_jobs_ready")
            cursor.execute(f"DROP TABLE IF EXISTS {legacy_jobs_table}")
            cursor.execute(
                f"ALTER TABLE instagram_media_jobs RENAME TO {legacy_jobs_table}"
            )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS instagram_media_jobs (
            channel TEXT NOT NULL,
            message_timestamp TEXT NOT NULL,
            thread_ts TEXT NOT NULL,
            author TEXT NOT NULL DEFAULT '',
            instagram_url TEXT NOT NULL,
            shortcode TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            next_attempt_at REAL NOT NULL DEFAULT 0,
            claimed_at REAL,
            claim_token TEXT,
            last_error TEXT,
            completed_at REAL,
            PRIMARY KEY (channel, thread_ts, shortcode)
        )
        """
    )
    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_instagram_media_jobs_ready
        ON instagram_media_jobs(status, next_attempt_at)
        """
    )
    try:
        cursor.execute(
            """
            ALTER TABLE instagram_media_jobs
            ADD COLUMN author TEXT NOT NULL DEFAULT ''
            """
        )
    except sqlite3.OperationalError:
        pass

    if legacy_jobs_table is not None:
        legacy_columns = {
            row[1] for row in cursor.execute(f"PRAGMA table_info({legacy_jobs_table})")
        }

        def legacy_column(name, fallback):
            return name if name in legacy_columns else fallback

        author = legacy_column("author", "''")
        attempts = legacy_column("attempts", "0")
        next_attempt_at = legacy_column("next_attempt_at", "0")
        last_error = legacy_column("last_error", "NULL")
        completed_at = legacy_column("completed_at", "NULL")
        cursor.execute(
            f"""
            INSERT OR IGNORE INTO instagram_media_jobs
                (channel, message_timestamp, thread_ts, author, instagram_url,
                 shortcode, status, attempts, next_attempt_at, last_error, completed_at)
            SELECT channel, message_timestamp, thread_ts, {author}, instagram_url,
                   shortcode,
                   CASE
                       WHEN status = 'complete' THEN 'complete'
                       WHEN status IN ('uploading', 'publication_uncertain')
                           THEN 'publication_uncertain'
                       ELSE 'cancelled'
                   END,
                   {attempts}, {next_attempt_at},
                   COALESCE({last_error}, 'Legacy queue row migrated fail-closed'),
                   {completed_at}
            FROM {legacy_jobs_table}
            ORDER BY rowid
            """
        )
        cursor.execute(f"DROP TABLE {legacy_jobs_table}")

    sources_existed = cursor.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'instagram_media_sources'"
    ).fetchone() is not None
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS instagram_media_sources (
            channel TEXT NOT NULL,
            message_timestamp TEXT NOT NULL,
            thread_ts TEXT NOT NULL,
            author TEXT NOT NULL DEFAULT '',
            instagram_url TEXT NOT NULL,
            shortcode TEXT NOT NULL,
            PRIMARY KEY (channel, message_timestamp, shortcode)
        )
        """
    )
    # Backfill once when introducing source tracking. Re-running migrations must
    # never resurrect a source removed by an edit or deletion. Unknown legacy
    # authors stay fail-closed and are not eligible for publication.
    if not sources_existed and legacy_jobs_table is None:
        cursor.execute(
            """
            INSERT OR IGNORE INTO instagram_media_sources
                (channel, message_timestamp, thread_ts, author, instagram_url, shortcode)
            SELECT channel, message_timestamp, thread_ts, author, instagram_url, shortcode
            FROM instagram_media_jobs
            WHERE status != 'cancelled' AND author != ''
            """
        )
        cursor.execute(
            """
            UPDATE instagram_media_jobs
            SET status = 'cancelled', claimed_at = NULL, claim_token = NULL,
                last_error = 'Unknown legacy author migrated fail-closed'
            WHERE author = '' AND status IN ('pending', 'processing')
            """
        )
    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_instagram_media_sources_job
        ON instagram_media_sources(channel, thread_ts, shortcode)
        """
    )
    conn.commit()

    # Tabella per tracciare i thread su #trash in cui il bot si è auto-ingaggiato
    try:
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS trash_engaged_threads (
                thread_ts TEXT NOT NULL,
                channel TEXT NOT NULL,
                decided INTEGER NOT NULL DEFAULT 0,
                engaged INTEGER NOT NULL DEFAULT 0,
                evaluated_at REAL NOT NULL,
                last_reply_ts TEXT,
                clown_assigned TEXT,
                cooldown_deferred INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (thread_ts, channel)
            )
        """
        )
        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_trash_engaged_evaluated ON trash_engaged_threads(evaluated_at)
        """
        )
        conn.commit()
    except:
        pass

    # Add `stopped` to trash_engaged_threads (per il comando @bot stop)
    try:
        cursor.execute(
            """
            ALTER TABLE trash_engaged_threads
            ADD COLUMN stopped INTEGER NOT NULL DEFAULT 0
            """
        )
        conn.commit()
    except:
        pass

    # Add `cooldown_deferred` to distinguish a temporary cooldown skip from a real LLM pass.
    try:
        cursor.execute(
            """
            ALTER TABLE trash_engaged_threads
            ADD COLUMN cooldown_deferred INTEGER NOT NULL DEFAULT 0
            """
        )
        conn.commit()
    except:
        pass

    # Tabella generica per thread ingaggiati esplicitamente con @bot /engage.
    # Non riusa trash_engaged_threads per evitare che vecchi auto-engage su #trash restino attivi.
    try:
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS engaged_threads (
                thread_ts TEXT NOT NULL,
                channel TEXT NOT NULL,
                engaged INTEGER NOT NULL DEFAULT 1,
                stopped INTEGER NOT NULL DEFAULT 0,
                engaged_at REAL NOT NULL,
                engaged_by TEXT,
                last_reply_ts TEXT,
                PRIMARY KEY (thread_ts, channel)
            )
        """
        )
        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_engaged_threads_engaged_at
            ON engaged_threads(engaged_at)
        """
        )
        conn.commit()
    except:
        pass

    # Debug AI privato opt-in. Nessuna riga equivale a debug disabilitato.
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS ai_debug_subscribers (
            user_id TEXT PRIMARY KEY,
            enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0,1)),
            updated_at REAL NOT NULL
        )
        """
    )
    conn.commit()

    # Migrazione: se la colonna timestamp è TEXT, la convertiamo in REAL
    try:
        cursor.execute("PRAGMA table_info(ai_requests)")
        columns = cursor.fetchall()
        timestamp_type = None
        for col in columns:
            if col[1] == 'timestamp':
                timestamp_type = col[2]
                break
        
        if timestamp_type == 'TEXT':
            # Crea una tabella temporanea con il nuovo schema
            cursor.execute("""
                CREATE TABLE ai_requests_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp REAL NOT NULL,
                    user_id TEXT NOT NULL,
                    channel TEXT NOT NULL
                )
            """)
            # Copia i dati convertendo i timestamp da ISO a Unix timestamp
            cursor.execute("""
                INSERT INTO ai_requests_new (id, timestamp, user_id, channel)
                SELECT id, 
                       CASE 
                           WHEN timestamp LIKE '%-%-% %:%:%' THEN 
                               (julianday(timestamp) - 2440587.5) * 86400.0
                           ELSE 
                               CAST(timestamp AS REAL)
                       END,
                       user_id, 
                       channel
                FROM ai_requests
            """)
            cursor.execute("DROP TABLE ai_requests")
            cursor.execute("ALTER TABLE ai_requests_new RENAME TO ai_requests")
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_ai_requests_timestamp ON ai_requests(timestamp)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_ai_requests_user_timestamp ON ai_requests(user_id, timestamp)
            """)
            conn.commit()
    except Exception as e:
        # Se la migrazione fallisce, continua (potrebbe essere già migrata o non esistere)
        pass

    # Cache condivisa dei documenti esterni usati per il confronto dei link.
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS link_documents (
            normalized_url TEXT PRIMARY KEY,
            requested_url TEXT NOT NULL,
            final_url TEXT,
            canonical_url TEXT,
            title TEXT,
            description TEXT,
            content TEXT,
            content_hash TEXT,
            embedding BLOB,
            extraction_quality TEXT NOT NULL DEFAULT 'pending',
            fetch_status TEXT NOT NULL DEFAULT 'pending',
            http_status INTEGER,
            fetched_at REAL,
            expires_at REAL,
            last_error TEXT
        )
        """
    )
    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_link_documents_content_hash
        ON link_documents(content_hash)
        WHERE content_hash IS NOT NULL
        """
    )
    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_link_documents_status_expiry
        ON link_documents(fetch_status, expires_at)
        """
    )

    # Associazione tra un messaggio Slack e ogni link esterno che contiene.
    # Lo stato del documento resta separato per poter riusare la cache senza
    # perdere il ciclo di vita del messaggio che ha condiviso il link.
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS message_links (
            channel TEXT NOT NULL,
            message_timestamp TEXT NOT NULL,
            thread_ts TEXT NOT NULL,
            normalized_url TEXT NOT NULL,
            original_url TEXT NOT NULL,
            permalink TEXT NOT NULL DEFAULT '',
            posted_at REAL NOT NULL,
            deterministic_checked_at REAL,
            duplicate_checked_at REAL,
            PRIMARY KEY (channel, message_timestamp, normalized_url)
        )
        """
    )
    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_message_links_url_posted
        ON message_links(normalized_url, posted_at)
        """
    )
    added_deterministic_gate = False
    try:
        cursor.execute(
            """
            ALTER TABLE message_links
            ADD COLUMN deterministic_checked_at REAL
            """
        )
        added_deterministic_gate = True
    except sqlite3.OperationalError:
        pass
    if added_deterministic_gate:
        # Rows written by the pre-gate implementation already completed the
        # synchronous deterministic path before this migration existed.
        cursor.execute(
            """
            UPDATE message_links
            SET deterministic_checked_at = posted_at
            WHERE deterministic_checked_at IS NULL
            """
        )
    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_message_links_thread
        ON message_links(channel, thread_ts)
        """
    )
    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_message_links_unchecked
        ON message_links(duplicate_checked_at, posted_at)
        """
    )
    # Importa le righe legacy senza cambiare o eliminare posted_links. I
    # duplicati storici restano così candidati del nuovo percorso.
    cursor.execute(
        """
        INSERT OR IGNORE INTO message_links
        (channel, message_timestamp, thread_ts, normalized_url, original_url,
         permalink, posted_at, deterministic_checked_at)
        SELECT p.channel,
               p.message_timestamp,
               COALESCE(m.thread_ts, p.message_timestamp),
               p.normalized_url,
               p.original_url,
               p.permalink,
               COALESCE(CAST(m.timestamp AS REAL), CAST(strftime('%s', p.posted_date) AS REAL), 0),
               COALESCE(CAST(m.timestamp AS REAL), CAST(strftime('%s', p.posted_date) AS REAL), 0)
        FROM posted_links p
        LEFT JOIN messages m
          ON m.channel = p.channel AND m.timestamp = p.message_timestamp
        """
    )

    # Una coda durevole per URL (non per messaggio): più messaggi che puntano
    # allo stesso documento condividono un solo fetch. INSERT OR IGNORE e il
    # claim atomico rendono sicuri retry Slack e worker Gunicorn multipli.
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS link_enrichment_jobs (
            normalized_url TEXT PRIMARY KEY,
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            recoveries INTEGER NOT NULL DEFAULT 0,
            available_at REAL NOT NULL,
            claimed_at REAL,
            claim_token TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            last_error TEXT
        )
        """
    )
    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_link_enrichment_jobs_claim
        ON link_enrichment_jobs(status, available_at, created_at)
        """
    )

    # Un solo alert di duplicato per messaggio nuovo. Il claim token evita che
    # eventi Slack concorrenti pubblichino lo stesso avviso due volte.
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS link_duplicate_alerts (
            current_channel TEXT NOT NULL,
            current_message_ts TEXT NOT NULL,
            current_thread_ts TEXT NOT NULL,
            current_normalized_url TEXT NOT NULL,
            source_channel TEXT NOT NULL,
            source_message_ts TEXT NOT NULL,
            source_normalized_url TEXT NOT NULL,
            source_permalink TEXT NOT NULL,
            match_type TEXT NOT NULL,
            score REAL,
            alert_message_ts TEXT NOT NULL DEFAULT '',
            alert_text TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'claimed',
            claim_token TEXT NOT NULL,
            claimed_at REAL NOT NULL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            PRIMARY KEY (current_channel, current_message_ts)
        )
        """
    )
    try:
        cursor.execute(
            """
            ALTER TABLE link_duplicate_alerts
            ADD COLUMN current_normalized_url TEXT NOT NULL DEFAULT ''
            """
        )
    except sqlite3.OperationalError:
        pass
    try:
        cursor.execute(
            """
            ALTER TABLE link_duplicate_alerts
            ADD COLUMN source_normalized_url TEXT NOT NULL DEFAULT ''
            """
        )
    except sqlite3.OperationalError:
        pass
    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_link_duplicate_alerts_source
        ON link_duplicate_alerts(source_channel, source_message_ts)
        """
    )

    # Stato resumable del confronto semantico: un callback elabora solo un
    # budget limitato e conserva cursore/miglior evidenza per l'iterazione dopo.
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS link_match_scans (
            current_channel TEXT NOT NULL,
            current_message_ts TEXT NOT NULL,
            current_normalized_url TEXT NOT NULL,
            candidate_after_rowid INTEGER NOT NULL DEFAULT 0,
            state_json TEXT NOT NULL DEFAULT '{}',
            claim_token TEXT NOT NULL DEFAULT '',
            claimed_at REAL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            PRIMARY KEY (
                current_channel,
                current_message_ts,
                current_normalized_url
            )
        )
        """
    )
    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_link_match_scans_claim
        ON link_match_scans(claim_token, claimed_at, created_at)
        """
    )
    conn.commit()



def claim_xcancel_alert(cursor, parent_ts, channel, alert_text):
    """Riserva atomicamente lo slot dell'alert xcancel per (parent_ts, channel).

    Sfrutta la PRIMARY KEY (parent_message_ts, channel) con INSERT OR IGNORE:
    solo il primo handler che processa il messaggio (evento message, message_changed
    dell'unfurl o retry di Slack) acquisisce lo slot e deve postare l'alert.

    Args:
        cursor: Cursore SQLite su un DB già migrato.
        parent_ts: Timestamp del messaggio che contiene i link x.com.
        channel: Canale del messaggio.
        alert_text: Testo dell'alert che verrà postato.

    Returns:
        bool: True se lo slot è stato riservato da questa chiamata, False se
        un alert per lo stesso messaggio è già tracciato o in corso di post.
    """
    cursor.execute(
        """
        INSERT OR IGNORE INTO xcancel_alerts
        (parent_message_ts, alert_message_ts, channel, alert_text)
        VALUES (?, '', ?, ?)
        """,
        (parent_ts, channel, alert_text),
    )
    return cursor.rowcount == 1


def finalize_xcancel_alert(cursor, parent_ts, alert_ts, channel, alert_text):
    """Completa la riserva dell'alert xcancel con il ts del messaggio postato.

    Aggiorna solo la riserva originale (alert_message_ts vuoto e stesso testo):
    se nel frattempo la riserva è stata rimossa o sostituita (parent cancellato
    o testo modificato durante il post), non tocca nulla.

    Args:
        cursor: Cursore SQLite su un DB già migrato.
        parent_ts: Timestamp del messaggio che contiene i link x.com.
        alert_ts: Timestamp dell'alert appena postato su Slack.
        channel: Canale del messaggio.
        alert_text: Testo con cui era stata acquisita la riserva.

    Returns:
        bool: True se la riserva è stata completata, False se non esiste più.
    """
    cursor.execute(
        """
        UPDATE xcancel_alerts
        SET alert_message_ts = ?
        WHERE parent_message_ts = ? AND channel = ?
          AND alert_message_ts = '' AND alert_text = ?
        """,
        (alert_ts, parent_ts, channel, alert_text),
    )
    return cursor.rowcount == 1


def db_connect(database_path):
    conn = sqlite3.connect(database_path)
    cursor = conn.cursor()
    return conn, cursor
