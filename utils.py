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



def db_connect(database_path):
    conn = sqlite3.connect(database_path)
    cursor = conn.cursor()
    return conn, cursor