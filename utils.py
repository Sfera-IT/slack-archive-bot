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
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
            """
        )
        conn.commit()
    except Exception as e:
        print(f"Error creating digest_details table: {e}")
        pass

def db_connect(database_path):
    conn = sqlite3.connect(database_path)
    cursor = conn.cursor()
    return conn, cursor
