import argparse
import logging
import os
import traceback
from sentence_transformers import SentenceTransformer
import re
from datetime import datetime, timedelta

from slack_bolt import App
from openai import OpenAI

from utils import db_connect, migrate_db
from url_cleaner import UrlCleaner

# Pre-compiled regex patterns
_X_COM_PATTERN = re.compile(r'^https?://(?:www\.)?x\.com/(.+)$', re.IGNORECASE)

# Lazy-loaded SentenceTransformer model (loaded once on first use)
_sentence_transformer_model = None


def _get_sentence_transformer():
    """Get or initialize the SentenceTransformer model (lazy loading)."""
    global _sentence_transformer_model
    if _sentence_transformer_model is None:
        logger.info("Loading SentenceTransformer model (one-time initialization)...")
        _sentence_transformer_model = SentenceTransformer('paraphrase-MiniLM-L6-v2')
        logger.info("SentenceTransformer model loaded successfully")
    return _sentence_transformer_model

parser = argparse.ArgumentParser()
parser.add_argument(
    "-d",
    "--database-path",
    default="slack.sqlite",
    help="path to the SQLite database. (default = ./slack.sqlite)",
)
parser.add_argument(
    "-l",
    "--log-level",
    default="debug",
    help="CRITICAL, ERROR, WARNING, INFO or DEBUG (default = DEBUG)",
)
parser.add_argument(
    "-p", "--port", default=3333, help="Port to serve on. (default = 3333)"
)
cmd_args, unknown = parser.parse_known_args()

# Check the environment too
log_level = os.environ.get("ARCHIVE_BOT_LOG_LEVEL", cmd_args.log_level)
database_path = os.environ.get("ARCHIVE_BOT_DATABASE_PATH", cmd_args.database_path)
port = os.environ.get("ARCHIVE_BOT_PORT", cmd_args.port)

# Setup logging
log_level = log_level.upper()
assert log_level in ["CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"]
logging.basicConfig(level=getattr(logging, log_level))
logger = logging.getLogger(__name__)

app = App(
    token=os.environ.get("SLACK_BOT_TOKEN"),
    signing_secret=os.environ.get("SLACK_SIGNING_SECRET"),
    logger=logger,
)

# URL cleaner instance loading local rules
_url_cleaner = UrlCleaner(rules_file=os.path.join(os.path.dirname(__file__), "url_rules.json"))

# Save the bot user's user ID
app._bot_user_id = app.client.auth_test()["user_id"]

# Nota: clown_users è ora memorizzato nel database per essere condiviso tra worker Gunicorn
# Le funzioni seguenti gestiscono la lettura/scrittura dal database


# Uses slack API to get most recent user list
# Necessary for User ID correlation
def update_users(conn, cursor):
    logger.info("Updating users")
    info = app.client.users_list()

    args = []
    for m in info["members"]:
        name = m["profile"]["display_name"]
        if not name:
            name = m["profile"]["real_name"]
        args.append(
            (
                name,
                m["id"],
                m["profile"].get(
                    "image_72",
                    "http://fst.slack-edge.com/66f9/img/avatars/ava_0024-32.png",
                ),
                m.get("deleted", False),
                m["profile"].get("real_name", ""),
                m["profile"].get("display_name", ""),
                m["profile"].get("email", "")
            )
        )
    cursor.executemany("INSERT OR REPLACE INTO users(name, id, avatar, is_deleted, real_name, display_name, email) VALUES(?,?,?,?,?,?,?)", args)
    conn.commit()


def create_embeddings(message):
    try:
        model = _get_sentence_transformer()
        embeddings = model.encode(message)
    except Exception as e:
        logger.warning(f"Error creating embeddings: {e}")
        embeddings = ""
    return embeddings


def get_channel_info(channel_id):
    channel = app.client.conversations_info(channel=channel_id)["channel"]

    # Get a list of members for the channel. This will be used when querying private channels.
    response = app.client.conversations_members(channel=channel["id"])
    members = response["members"]
    while response["response_metadata"]["next_cursor"]:
        response = app.client.conversations_members(
            channel=channel["id"], cursor=response["response_metadata"]["next_cursor"]
        )
        members += response["members"]

    return (
        channel["id"],
        channel["name"],
        channel["is_private"],
        [(channel["id"], m) for m in members],
    )


def update_channels(conn, cursor):
    logger.info("Updating channels")
    channels = app.client.conversations_list(types="public_channel,private_channel")[
        "channels"
    ]

    channel_args = []
    member_args = []
    for channel in channels:
        if channel["is_member"]:
            channel_id, channel_name, channel_is_private, members = get_channel_info(
                channel["id"]
            )

            channel_args.append((channel_name, channel_id, channel_is_private))

            member_args += members

    cursor.executemany(
        "INSERT INTO channels(name, id, is_private) VALUES(?,?,?)", channel_args
    )
    cursor.executemany("INSERT INTO members(channel, user) VALUES(?,?)", member_args)
    conn.commit()


def clean_expired_clown_users(conn, cursor):
    """Rimuove gli utenti scaduti dalla lista clown nel database."""
    now = datetime.now().isoformat()
    cursor.execute("SELECT nickname FROM clown_users WHERE expiry_date < ?", (now,))
    expired = [row[0] for row in cursor.fetchall()]
    
    if expired:
        logger.info(f"[CLOWN] Cleaning {len(expired)} expired users: {expired}")
        cursor.execute("DELETE FROM clown_users WHERE expiry_date < ?", (now,))
        conn.commit()
        for nickname in expired:
            logger.info(f"[CLOWN] Removed expired clown user: {nickname}")
    
    # Log stato attuale della lista
    cursor.execute("SELECT nickname, expiry_date FROM clown_users")
    current_users = cursor.fetchall()
    if current_users:
        user_list = [f"{nickname} (expires: {expiry})" for nickname, expiry in current_users]
        logger.info(f"[CLOWN] Current clown users: {user_list}")
    else:
        logger.info("[CLOWN] No users in clown list")


def is_user_in_clown_list(conn, cursor, nickname_lower):
    """Verifica se un utente è nella lista clown e non è scaduto."""
    clean_expired_clown_users(conn, cursor)
    cursor.execute("SELECT expiry_date FROM clown_users WHERE nickname = ?", (nickname_lower,))
    result = cursor.fetchone()
    return result is not None


def add_clown_user(conn, cursor, nickname_lower, expiry_date):
    """Aggiunge un utente alla lista clown nel database."""
    expiry_str = expiry_date.isoformat()
    cursor.execute(
        "INSERT OR REPLACE INTO clown_users (nickname, expiry_date) VALUES (?, ?)",
        (nickname_lower, expiry_str)
    )
    conn.commit()
    logger.info(f"[CLOWN] Added {nickname_lower} to clown list in DB, expires: {expiry_date}")


def remove_clown_user(conn, cursor, nickname_lower):
    """Rimuove un utente dalla lista clown nel database."""
    cursor.execute("DELETE FROM clown_users WHERE nickname = ?", (nickname_lower,))
    conn.commit()
    logger.info(f"[CLOWN] Removed {nickname_lower} from clown list in DB")


def handle_query(event, cursor, say):
    text = event.get("text", "").strip()
    user_id = event.get("user", "unknown")
    
    logger.info(f"[CLOWN] Received DM from user {user_id}, text: '{text}'")
    
    # Gestisci comando /clown
    if text.startswith("/clown "):
        nickname = text[7:].strip()  # Rimuovi "/clown " e spazi
        logger.info(f"[CLOWN] Processing /clown command with nickname: '{nickname}'")
        if nickname:
            nickname_lower = nickname.lower()
            expiry_date = datetime.now() + timedelta(days=7)
            add_clown_user(cursor.connection, cursor, nickname_lower, expiry_date)
            clean_expired_clown_users(cursor.connection, cursor)  # Pulisci utenti scaduti
            say(f"✅ Aggiunto {nickname} alla lista clown per una settimana (scade il {expiry_date.strftime('%Y-%m-%d %H:%M:%S')})")
        else:
            logger.warning("[CLOWN] /clown command without nickname")
            say("❌ Devi specificare un nickname. Uso: /clown nickname")
        return
    
    # Gestisci comando /clownremove
    if text.startswith("/clownremove "):
        nickname = text[13:].strip()  # Rimuovi "/clownremove " e spazi
        logger.info(f"[CLOWN] Processing /clownremove command with nickname: '{nickname}'")
        if nickname:
            nickname_lower = nickname.lower()
            if is_user_in_clown_list(cursor.connection, cursor, nickname_lower):
                remove_clown_user(cursor.connection, cursor, nickname_lower)
                say(f"✅ Rimosso {nickname} dalla lista clown")
            else:
                # Mostra lista corrente per debug
                cursor.execute("SELECT nickname FROM clown_users")
                current_list = [row[0] for row in cursor.fetchall()]
                logger.info(f"[CLOWN] {nickname} (lowercase: {nickname_lower}) not found in clown list. Current list: {current_list}")
                say(f"❌ {nickname} non è nella lista clown")
        else:
            logger.warning("[CLOWN] /clownremove command without nickname")
            say("❌ Devi specificare un nickname. Uso: /clownremove nickname")
        return
    
    # Comportamento di default per altri messaggi
    logger.debug(f"[CLOWN] DM not a clown command, using default response")
    say("Questa interfaccia è stata disattivata. Ora puoi andare qui: https://sferaarchive-client.vercel.app/")
    return


def get_first_reply_in_thread(res):
    # get all ther replies of the message
    try:
        replies = app.client.conversations_replies(channel=res[3], ts=res[2])
        # if we have at least one reply
        if len(replies.data["messages"]) > 0:
            # if the timestamp of the actual message is equal to thread_ts of the first message in the replies, it means 
            # that it's the main (parent) message.
            if "thread_ts" in replies.data["messages"][0]:
                if res[2] == replies.data["messages"][0]["thread_ts"]:
                    # since main (parent) message cannot be referenced via permalink in Slack Free, we point the permalink 
                    # to the first child
                    if len(replies.data["messages"]) > 1:
                        # get the timestamp of the first reply and replace the link to it
                        reslist = list(res)
                        reslist[2] = replies.data["messages"][1]["ts"]
                        res = tuple(reslist)
    except Exception as e:
        logger.debug("An error occurred fetching replies: ", e)

    return res


def get_permalink_and_save(res):
    if res[4] == "":
        newres = get_first_reply_in_thread(res)
        logger.debug("Getting Permalink for res: ")
        logger.debug(res)
        conn, cursor = db_connect(database_path)

        permalink = app.client.chat_getPermalink(channel=newres[3], message_ts=newres[2])
        logger.debug(permalink["permalink"])
        res = res[:-1]
        res = res + (permalink["permalink"],)

        cursor.execute(
            "UPDATE messages SET permalink = ? WHERE user = ? AND channel = ? AND timestamp = ?",
            (permalink["permalink"], res[1], res[3], res[2]),
        )
        conn.commit()
    else:
        logger.debug("Permalink already in database, skipping get_permalink_and_save")

    return res


def extract_urls(text):
    """Estrae tutti gli URL HTTP/HTTPS da un testo."""
    # Pattern per rilevare URL http/https
    url_pattern = r'https?://[^\s<>":{}|\\^`\[\]]+'
    urls = re.findall(url_pattern, text, flags=re.IGNORECASE)
    # Rimuovi eventuali caratteri di punteggiatura alla fine dell'URL
    cleaned_urls = []
    for url in urls:
        # Rimuovi caratteri di punteggiatura comuni alla fine
        url = url.rstrip('.,;:!?')
        cleaned_urls.append(url)
    return cleaned_urls


def normalize_url(url):
    """Normalizza un URL applicando le regole ClearURLs (provider-aware)."""
    try:
        return _url_cleaner.clean(url)
    except Exception as e:
        logger.warning(f"Error normalizing URL {url}: {e}")
        return url


def post_xcancel_alternatives(message, say):
    """Se il messaggio contiene link a x.com, posta le alternative xcancel.com nel thread."""
    text = message.get("text", "")
    if not text:
        return
    
    urls = extract_urls(text)
    if not urls:
        return
    
    xcancel_links = set()  # Use set to deduplicate
    for url in urls:
        match = _X_COM_PATTERN.match(url)
        if match:
            path = match.group(1)
            xcancel_url = f"https://xcancel.com/{path}"
            # Controlla che l'utente non abbia già postato il link xcancel
            if xcancel_url.lower() not in text.lower():
                xcancel_links.add(xcancel_url)
    
    if not xcancel_links:
        return
    
    # Costruisci il messaggio
    xcancel_list = list(xcancel_links)
    if len(xcancel_list) == 1:
        response_text = f"🔗 Link senza Shitler: {xcancel_list[0]}"
    else:
        links_formatted = "\n".join(f"• {link}" for link in xcancel_list)
        response_text = f"🔗 Link senza Shitler:\n{links_formatted}"
    
    # Posta nel thread (usa thread_ts se esiste, altrimenti ts del messaggio)
    thread_ts = message.get("thread_ts", message.get("ts"))
    try:
        say(text=response_text, thread_ts=thread_ts)
        logger.info(f"Posted xcancel alternatives for {len(xcancel_links)} x.com link(s)")
    except Exception as e:
        logger.error(f"Error posting xcancel alternative: {e}")


def check_and_store_links(message, permalink_dict, say):
    """Controlla se ci sono link nel messaggio e verifica duplicati.
    Il controllo viene fatto solo sui messaggi principali, non sulle risposte nei thread."""
    # Salta il controllo se è una risposta in un thread (ha thread_ts diverso dal timestamp)
    if "thread_ts" in message and message.get("thread_ts") != message.get("ts"):
        logger.debug("Skipping link check for thread reply (not a main message)")
        return
    
    text = message.get("text", "")
    if not text:
        return
    
    urls = extract_urls(text)
    if not urls:
        return
    
    conn, cursor = db_connect(database_path)
    
    try:
        # Ottieni il permalink del messaggio corrente
        current_permalink = permalink_dict.get("permalink", "")
        # Se non c'è permalink, prova a ottenerlo
        if not current_permalink and message.get("ts"):
            try:
                current_permalink = app.client.chat_getPermalink(
                    channel=message["channel"], 
                    message_ts=message["ts"]
                )["permalink"]
            except Exception as e:
                logger.warning(f"Could not get permalink for message: {e}")
        
        # Ottieni il nome utente per la risposta
        user_name = message.get("user", "")
        try:
            user_info = app.client.users_info(user=user_name)
            user_display_name = user_info["user"]["profile"].get("display_name") or user_info["user"]["profile"].get("real_name", "utente")
        except:
            user_display_name = "utente"
        
        for original_url in urls:
            # Escludi i link di Slack dall'analisi
            if original_url.startswith("https://sferait-ws.slack.com/") or original_url.startswith("http://sferait-ws.slack.com/"):
                logger.debug(f"Skipping Slack link from duplicate check: {original_url}")
                continue
            
            normalized_url = normalize_url(original_url)
            
            # Controlla se esiste già un link normalizzato simile negli ultimi 45 giorni
            # Escludi il messaggio corrente dalla ricerca per evitare di trovare il link appena salvato
            forty_five_days_ago = datetime.now() - timedelta(days=45)
            cursor.execute(
                """
                SELECT normalized_url, permalink, posted_date, duplicate_notified, message_timestamp
                FROM posted_links 
                WHERE normalized_url = ? 
                AND posted_date >= ?
                AND message_timestamp != ?
                ORDER BY posted_date DESC
                LIMIT 1
                """,
                (normalized_url, forty_five_days_ago.isoformat(), message.get("ts", ""))
            )
            
            existing_link = cursor.fetchone()
            
            if existing_link:
                # Link duplicato trovato
                # existing_link è una tuple: (normalized_url, permalink, posted_date, duplicate_notified, message_timestamp)
                original_permalink = existing_link[1] if len(existing_link) > 1 else ""
                posted_date_str = existing_link[2] if len(existing_link) > 2 else "unknown"
                already_notified = existing_link[3] if len(existing_link) > 3 else 0
                previous_message_ts = existing_link[4] if len(existing_link) > 4 else ""
                
                # Logging dettagliato per debug
                logger.info(
                    f"DUPLICATE_LINK_DETECTED: original_url='{original_url}' "
                    f"normalized_url='{normalized_url}' "
                    f"previous_permalink='{original_permalink}' "
                    f"previous_posted_date='{posted_date_str}' "
                    f"previous_message_ts='{previous_message_ts}' "
                    f"already_notified={bool(already_notified)} "
                    f"current_message_ts='{message.get('ts', '')}' "
                    f"current_channel='{message.get('channel', '')}' "
                    f"user='{user_display_name}'"
                )
                
                # Notifica solo se non è già stato notificato
                if not already_notified:
                    response_text = f"Ciao {user_display_name}, questo link è stato già postato e lo trovi qui: {original_permalink}"
                    
                    try:
                        # Rispondi nel thread se il messaggio è parte di un thread, altrimenti come risposta normale
                        if "thread_ts" in message:
                            # Se è già un thread, rispondi nello stesso thread
                            say(text=response_text, thread_ts=message["thread_ts"])
                            logger.debug(f"Sent duplicate notification in existing thread: {message.get('thread_ts')}")
                        else:
                            # Se non è un thread, crea una risposta nel thread del messaggio originale
                            say(text=response_text, thread_ts=message["ts"])
                            logger.debug(f"Sent duplicate notification in new thread: {message.get('ts')}")
                        
                        # Aggiorna il flag duplicate_notified per il link trovato
                        cursor.execute(
                            """
                            UPDATE posted_links
                            SET duplicate_notified = 1
                            WHERE normalized_url = ? AND message_timestamp = ?
                            """,
                            (normalized_url, previous_message_ts)
                        )
                        conn.commit()
                        logger.debug(f"Marked link as notified: {normalized_url} (message_ts: {previous_message_ts})")
                    except Exception as e:
                        logger.error(f"Error sending duplicate link notification or updating flag: {e}")
                        conn.rollback()
                else:
                    logger.debug(f"Link already notified, skipping notification: {normalized_url}")
                
                # NON salvare il link se è duplicato
                logger.debug(f"Skipping database insert for duplicate link: {normalized_url}")
                continue
            
            # Link non duplicato: salvalo nella tabella
            logger.debug(
                f"NEW_LINK_SAVING: original_url='{original_url}' "
                f"normalized_url='{normalized_url}' "
                f"message_ts='{message.get('ts', '')}' "
                f"channel='{message.get('channel', '')}'"
            )
            
            try:
                timestamp = float(message.get("ts", 0))
                posted_date = datetime.fromtimestamp(timestamp).isoformat()
                
                cursor.execute(
                    """
                    INSERT OR IGNORE INTO posted_links 
                    (normalized_url, original_url, message_timestamp, channel, permalink, posted_date, duplicate_notified)
                    VALUES (?, ?, ?, ?, ?, ?, 0)
                    """,
                    (
                        normalized_url,
                        original_url,
                        message["ts"],
                        message["channel"],
                        current_permalink,
                        posted_date
                    )
                )
                conn.commit()
                logger.debug(f"Successfully saved new link to database: {normalized_url}")
            except Exception as e:
                logger.error(f"Error storing link in database: {e}")
                conn.rollback()
                
    except Exception as e:
        logger.error(f"Error in check_and_store_links: {e}")
        conn.rollback()
    finally:
        conn.close()


@app.event("member_joined_channel")
def handle_join(event):
    conn, cursor = db_connect(database_path)
    try:
        # If the user added is archive bot, then add the channel too
        if event["user"] == app._bot_user_id:
            channel_id, channel_name, channel_is_private, members = get_channel_info(
                event["channel"]
            )
            cursor.execute(
                "INSERT INTO channels(name, id, is_private) VALUES(?,?,?)",
                (channel_name, channel_id, channel_is_private),
            )
            cursor.executemany("INSERT INTO members(channel, user) VALUES(?,?)", members)
        else:
            cursor.execute(
                "INSERT INTO members(channel, user) VALUES(?,?)",
                (event["channel"], event["user"]),
            )
        conn.commit()
    finally:
        conn.close()


@app.event("member_left_channel")
def handle_left(event):
    conn, cursor = db_connect(database_path)
    try:
        cursor.execute(
            "DELETE FROM members WHERE channel = ? AND user = ?",
            (event["channel"], event["user"]),
        )
        conn.commit()
    finally:
        conn.close()


def handle_rename(event):
    channel = event["channel"]
    conn, cursor = db_connect(database_path)
    try:
        cursor.execute(
            "UPDATE channels SET name = ? WHERE id = ?", (channel["name"], channel["id"])
        )
        conn.commit()
    finally:
        conn.close()


@app.event("channel_rename")
def handle_channel_rename(event):
    handle_rename(event)


@app.event("group_rename")
def handle_group_rename(event):
    handle_rename(event)


# For some reason slack fires off both *_rename and *_name events, so create handlers for them
# but don't do anything in the *_name events.
@app.event({"type": "message", "subtype": "group_name"})
def handle_group_name():
    pass


@app.event({"type": "message", "subtype": "channel_name"})
def handle_channel_name():
    pass


@app.event("user_change")
def handle_user_change(event):
    user_id = event["user"]["id"]
    new_username = event["user"]["profile"]["display_name"]
    if not new_username:
        new_username = event["user"]["profile"]["real_name"]

    conn, cursor = db_connect(database_path)
    try:
        cursor.execute("UPDATE users SET name = ? WHERE id = ?", (new_username, user_id))
        conn.commit()
    finally:
        conn.close()


def handle_message(message, say):
    logger.debug(message)
    user_id = message.get("user", "unknown")
    channel_type = message.get("channel_type", "unknown")
    text_preview = message.get("text", "")[:50] if message.get("text") else "(no text)"
    
    logger.info(f"[CLOWN] handle_message called - user: {user_id}, channel_type: {channel_type}, text_preview: '{text_preview}...'")
    
    if "text" not in message or message["user"] == "USLACKBOT":
        logger.debug("[CLOWN] Skipping message: no text or from USLACKBOT")
        return

    # Controlla se il bot è menzionato nel messaggio
    bot_user_id = app._bot_user_id
    text = message.get("text", "")
    if bot_user_id and f"<@{bot_user_id}>" in text:
        logger.info(f"[AI] Bot mentioned in message (via handle_message) by user {user_id}")
        # Gestisci la menzione
        try:
            handle_app_mention(message, say)
            return
        except Exception as e:
            logger.error(f"[AI] Error handling mention in handle_message: {e}")
            logger.error(traceback.format_exc())

    conn, cursor = db_connect(database_path)

    # If it's a DM, treat it as a search query
    if message["channel_type"] == "im":
        logger.info(f"[CLOWN] Message is a DM, routing to handle_query")
        handle_query(message, cursor, say)
    elif "user" not in message:
        logger.warning("No valid user. Previous event not saved")
    else:  # Otherwise save the message to the archive.
        # get the permalink only if the message is not the main post (slack bug), otherwise leave it empty
        if "thread_ts" in message:
            permalink = app.client.chat_getPermalink(
                channel=message["channel"], message_ts=message["ts"]
            )
        else:
            permalink = {'permalink': ''}

        # Save original message data before opt-out check
        original_text = message.get("text", "")
        original_user = message.get("user", "")
        
        # Check if user opted out
        cursor.execute("SELECT user, timestamp FROM optout WHERE user = ?", (message["user"],))
        row = cursor.fetchone()

        clown_user = message["user"]

        if row is not None:
            message["text"] = "User opted out of archiving. This message has been deleted"
            message["user"] = "USLACKBOT"
            message["permalink"] = ""

        logger.debug(permalink["permalink"])
        cursor.execute(
            "INSERT INTO messages VALUES(?, ?, ?, ?, ?, ?, ?)",
            (
                message["text"],
                message["user"],
                message["channel"],
                message["ts"],
                permalink["permalink"],
                message["thread_ts"] if "thread_ts" in message else message["ts"],
                create_embeddings(message["text"])
            ),
        )
        conn.commit()
        conn.close()

        # Check for duplicate links and respond if found (using original message data)
        # Create a copy of the message with original data for link checking
        original_message = message.copy()
        original_message["text"] = original_text
        original_message["user"] = original_user
        check_and_store_links(original_message, permalink, say)
        
        # Post xcancel.com alternatives for any x.com links
        post_xcancel_alternatives(original_message, say)

        # Ensure that the user exists in the DB
        conn, cursor = db_connect(database_path)
        cursor.execute("SELECT * FROM users WHERE id = ?", (message["user"],))
        row = cursor.fetchone()
        if row is None:
            update_users(conn, cursor)
        
        # Ottieni il nome utente per controllare se è nella lista clown
        # Controlla name, display_name e real_name per trovare il match
        cursor.execute("SELECT name, display_name, real_name FROM users WHERE id = ?", (clown_user,))
        user_row = cursor.fetchone()
        
        # Controlla se l'utente è nella lista clown e aggiungi la reaction
        if user_row:
            name = user_row[0] if user_row[0] else ""
            display_name = user_row[1] if user_row[1] else ""
            real_name = user_row[2] if user_row[2] else ""
            
            logger.debug(f"[CLOWN] User data from DB - name: '{name}', display_name: '{display_name}', real_name: '{real_name}'")
            
            # Pulisci utenti scaduti e controlla se l'utente è nella lista
            clean_expired_clown_users(conn, cursor)
            
            # Controlla tutti i possibili nickname (name, display_name, real_name)
            # in ordine di priorità: display_name > name > real_name
            user_names_to_check = []
            if display_name:
                user_names_to_check.append(display_name.lower())
            if name and name.lower() not in user_names_to_check:
                user_names_to_check.append(name.lower())
            if real_name and real_name.lower() not in user_names_to_check:
                user_names_to_check.append(real_name.lower())
            
            logger.debug(f"[CLOWN] Checking user names (lowercase): {user_names_to_check}")
            
            # Controlla se uno dei nickname corrisponde
            found_in_list = False
            matched_nickname = None
            for user_name_lower in user_names_to_check:
                if is_user_in_clown_list(conn, cursor, user_name_lower):
                    found_in_list = True
                    matched_nickname = user_name_lower
                    break
            
            if found_in_list:
                # Ottieni la data di scadenza per il log
                cursor.execute("SELECT expiry_date FROM clown_users WHERE nickname = ?", (matched_nickname,))
                expiry_result = cursor.fetchone()
                expiry = expiry_result[0] if expiry_result else "unknown"
                logger.info(f"[CLOWN] User '{matched_nickname}' found in clown list (expires: {expiry})")
                try:
                    result = app.client.reactions_add(
                        channel=message["channel"],
                        timestamp=message["ts"],
                        name="clown_face"
                    )
                    if result.get("ok"):
                        logger.info(f"[CLOWN] ✅ Successfully added clown reaction to message from user: {matched_nickname}")
                    else:
                        logger.warning(f"[CLOWN] ❌ Failed to add reaction: {result.get('error', 'unknown error')}")
                except Exception as e:
                    logger.error(f"[CLOWN] ❌ Exception adding clown reaction: {e}")
                    logger.error(traceback.format_exc())
            else:
                logger.debug(f"[CLOWN] User not in clown list (checked: {user_names_to_check})")
        else:
            logger.warning(f"[CLOWN] Could not find user in database for user_id: {message.get('user', 'unknown')}")
        
        conn.close()

    logger.debug("--------------------------")


@app.event({"type": "message", "subtype": "file_share"})
def handle_message_with_file(event, say):
    logger = logging.getLogger(__name__)
    logger.debug(event)

    # Extract the text and other necessary information from the event
    message = {
        "text": event.get("text", "") + " - Il messaggio conteneva un media ma non è stato possibile salvarlo.",
        "user": event["user"],
        "channel": event["channel"],
        "ts": event["ts"],
        "thread_ts": event.get("thread_ts"),
        "channel_type": event["channel_type"]
    }

    # Call handle_message with the extracted information
    handle_message(message, say)


@app.message("")
def handle_message_default(message, say):
    handle_message(message, say)


def get_thread_messages(channel, thread_ts):
    """Recupera tutti i messaggi di un thread."""
    try:
        all_messages = []
        cursor = None
        
        # Usa conversations_replies per recuperare tutti i messaggi del thread
        response = app.client.conversations_replies(channel=channel, ts=thread_ts)
        messages = response.get("messages", [])
        
        # Continua a recuperare se ci sono più pagine
        while response.get("has_more", False):
            cursor = response.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break
            response = app.client.conversations_replies(
                channel=channel, ts=thread_ts, cursor=cursor
            )
            messages.extend(response.get("messages", []))
        
        # Ordina i messaggi per timestamp
        messages.sort(key=lambda x: float(x.get("ts", 0)))
        
        # Recupera i nomi utente dal database
        conn, db_cursor = db_connect(database_path)
        
        for msg in messages:
            user_id = msg.get("user", "")
            if not user_id or user_id == "USLACKBOT":
                continue
            
            # Recupera il nome utente dal database
            db_cursor.execute("SELECT name, display_name, real_name FROM users WHERE id = ?", (user_id,))
            user_row = db_cursor.fetchone()
            
            if user_row:
                # Usa display_name, poi name, poi real_name
                user_name = user_row[1] if user_row[1] else (user_row[0] if user_row[0] else user_row[2] if user_row[2] else "Unknown")
            else:
                user_name = "Unknown"
            
            all_messages.append({
                "user": user_name,
                "text": msg.get("text", ""),
                "ts": msg.get("ts", "")
            })
        
        conn.close()
        return all_messages
        
    except Exception as e:
        logger.error(f"Error getting thread messages: {e}")
        logger.error(traceback.format_exc())
        return []


def check_ai_throttle(conn, cursor, user_id, channel):
    """Controlla se la richiesta rispetta i limiti di throttle.
    Limiti: 2 messaggi al minuto, 10 messaggi ogni ora.
    Ritorna (allowed, message, throttle_info) dove:
    - allowed: True se permesso, False se throttled
    - message: messaggio da inviare se throttled
    - throttle_info: dict con info sul throttle per logging"""
    now = datetime.now()
    # Usa timestamp Unix (numerici) per confronti precisi
    now_timestamp = now.timestamp()
    one_minute_ago_timestamp = (now - timedelta(minutes=1)).timestamp()
    one_hour_ago_timestamp = (now - timedelta(hours=1)).timestamp()
    
    # Prima pulisci richieste vecchie (più di 1 ora e 5 minuti) per mantenere il database pulito
    cleanup_threshold = (now - timedelta(hours=1, minutes=5)).timestamp()
    cursor.execute("DELETE FROM ai_requests WHERE timestamp < ?", (cleanup_threshold,))
    deleted_count = cursor.rowcount
    if deleted_count > 0:
        logger.debug(f"[AI] Cleaned up {deleted_count} old throttle records")
    conn.commit()
    
    # Conta richieste nell'ultimo minuto (confronto numerico)
    cursor.execute(
        "SELECT COUNT(*) FROM ai_requests WHERE timestamp > ? AND user_id = ?",
        (one_minute_ago_timestamp, user_id)
    )
    requests_last_minute = cursor.fetchone()[0]
    
    # Conta richieste nell'ultima ora (confronto numerico)
    cursor.execute(
        "SELECT COUNT(*) FROM ai_requests WHERE timestamp > ? AND user_id = ?",
        (one_hour_ago_timestamp, user_id)
    )
    requests_last_hour = cursor.fetchone()[0]
    
    throttle_info = {
        "requests_last_minute": requests_last_minute,
        "requests_last_hour": requests_last_hour,
        "limit_per_minute": 2,
        "limit_per_hour": 10,
        "one_hour_ago_timestamp": one_hour_ago_timestamp,
        "now_timestamp": now_timestamp
    }
    
    # Controlla limiti
    if requests_last_minute >= 2:
        # Calcola quando sarà possibile inviare di nuovo (tra 1 minuto)
        next_available = (now + timedelta(minutes=1)).strftime("%H:%M:%S")
        message = f"⏱️ Troppe richieste! Hai già fatto {requests_last_minute} richieste nell'ultimo minuto (limite: 2). Prova di nuovo dopo le {next_available}."
        logger.warning(f"[AI] Throttle exceeded: {requests_last_minute} requests in last minute (limit: 2)")
        return False, message, throttle_info
    
    if requests_last_hour >= 10:
        # Calcola quando sarà possibile inviare di nuovo (tra 1 ora)
        next_available = (now + timedelta(hours=1)).strftime("%H:%M:%S")
        message = f"⏱️ Troppe richieste! Hai già fatto {requests_last_hour} richieste nell'ultima ora (limite: 10). Prova di nuovo dopo le {next_available}."
        logger.warning(f"[AI] Throttle exceeded: {requests_last_hour} requests in last hour (limit: 10)")
        return False, message, throttle_info
    
    # Registra la richiesta con timestamp Unix
    cursor.execute(
        "INSERT INTO ai_requests (timestamp, user_id, channel) VALUES (?, ?, ?)",
        (now_timestamp, user_id, channel)
    )
    conn.commit()
    
    logger.info(f"[AI] Throttle OK: {requests_last_minute}/2 per minuto, {requests_last_hour}/10 per ora (now_ts: {now_timestamp:.2f}, one_hour_ago_ts: {one_hour_ago_timestamp:.2f})")
    return True, None, throttle_info


def handle_app_mention(event, say):
    """Gestisce le menzioni del bot in una conversazione.
    Può essere chiamata sia dall'evento app_mention che da handle_message."""
    try:
        channel = event.get("channel")
        thread_ts = event.get("ts")  # Timestamp del messaggio che menziona il bot
        text = event.get("text", "")
        user_id = event.get("user", "")
        
        logger.info(f"[AI] Bot mentioned by user {user_id} in channel {channel}, thread_ts: {thread_ts}, text: '{text[:100]}...'")
        
        # Controlla throttle
        conn, cursor = db_connect(database_path)
        allowed, throttle_message, throttle_info = check_ai_throttle(conn, cursor, user_id, channel)
        
        logger.info(f"[AI] Throttle status: {throttle_info}")
        
        if not allowed:
            # Determina thread_ts per la risposta
            actual_thread_ts = event.get("thread_ts", thread_ts)
            say(throttle_message, thread_ts=actual_thread_ts)
            conn.close()
            return
        
        conn.close()
        
        # Rimuovi la menzione del bot dal testo
        bot_user_id = app._bot_user_id
        text = re.sub(rf'<@{bot_user_id}>', '', text).strip()
        
        if not text:
            text = "Puoi aiutarmi con questa conversazione?"
        
        # Determina se è un thread o un messaggio principale
        # Se il messaggio ha thread_ts diverso da ts, è una risposta in un thread
        # Altrimenti, potrebbe essere l'inizio di un thread o un messaggio principale
        is_thread_reply = "thread_ts" in event and event.get("thread_ts") != thread_ts
        
        if is_thread_reply:
            # È una risposta in un thread esistente, usa thread_ts
            actual_thread_ts = event.get("thread_ts")
        else:
            # Potrebbe essere un messaggio principale o l'inizio di un thread
            # Usa il timestamp del messaggio corrente come thread_ts
            actual_thread_ts = thread_ts
        
        logger.info(f"[AI] Fetching thread messages for thread_ts: {actual_thread_ts}")
        
        # Recupera tutti i messaggi del thread
        thread_messages = get_thread_messages(channel, actual_thread_ts)
        
        if not thread_messages:
            say("Non ho trovato messaggi in questa conversazione.", thread_ts=actual_thread_ts)
            return
        
        logger.info(f"[AI] Found {len(thread_messages)} messages in thread")
        
        # Formatta i messaggi per il context
        formatted_messages = "\n".join([
            f"{msg['user']}: {msg['text']}" for msg in thread_messages
        ])
        
        # Prepara il prompt per ChatGPT
        system_prompt = """Sei un assistente utile che assiste gli utenti nelle loro richieste e conversazioni slack, e quando richiesto risponde alle domande basandosi sul contesto della conversazione di Slack viene fornita. 
Rispondi sempre in italiano, in modo chiaro e conciso. 
Se la domanda non può essere risposta basandosi sulla conversazione fornita, dillo chiaramente, ma rispondi comunque in base alla tua conoscenza (ripeto, specificandolo chiaramente) e assisti l'utente nelle sue richieste"""
        
        user_prompt = f"""Ecco la conversazione completa:

{formatted_messages}

Domanda: {text}

Rispondi basandoti sulla conversazione sopra."""
        
        # Chiama ChatGPT
        openai_api_key = os.environ.get("OPENAI_API_KEY")
        if not openai_api_key:
            logger.error("[AI] OPENAI_API_KEY not set")
            say("Errore: chiave API OpenAI non configurata.", thread_ts=actual_thread_ts)
            return
        
        client = OpenAI(api_key=openai_api_key)
        
        logger.info(f"[AI] Sending request to OpenAI with {len(thread_messages)} messages")
        
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            max_tokens=2000,
            temperature=0.7,
        )
        
        ai_response = response.choices[0].message.content.strip()
        
        logger.info(f"[AI] Received response from OpenAI, length: {len(ai_response)}")
        
        # Recupera le informazioni sul throttle corrente per aggiungerle alla risposta
        conn_throttle, cursor_throttle = db_connect(database_path)
        now = datetime.now()
        one_minute_ago_timestamp = (now - timedelta(minutes=1)).timestamp()
        one_hour_ago_timestamp = (now - timedelta(hours=1)).timestamp()
        
        cursor_throttle.execute(
            "SELECT COUNT(*) FROM ai_requests WHERE timestamp > ? AND user_id = ?",
            (one_minute_ago_timestamp, user_id)
        )
        current_minute_count = cursor_throttle.fetchone()[0]
        
        cursor_throttle.execute(
            "SELECT COUNT(*) FROM ai_requests WHERE timestamp > ? AND user_id = ?",
            (one_hour_ago_timestamp, user_id)
        )
        current_hour_count = cursor_throttle.fetchone()[0]
        
        conn_throttle.close()
        
        # Aggiungi la riga con i rate limit alla risposta
        rate_limit_info = f"\n\n_📊 Rate limit per user: {current_minute_count}/2 al minuto, {current_hour_count}/10 all'ora_"
        final_response = ai_response + rate_limit_info
        
        logger.info(f"[AI] Added rate limit info: {current_minute_count}/2 per minuto, {current_hour_count}/10 per ora")
        
        # Rispondi nel thread
        say(final_response, thread_ts=actual_thread_ts)
        
    except Exception as e:
        logger.error(f"[AI] Error handling app mention: {e}")
        logger.error(traceback.format_exc())
        try:
            say("Mi dispiace, c'è stato un errore nel processare la tua richiesta.", thread_ts=event.get("thread_ts", event.get("ts")))
        except:
            pass


@app.event("app_mention")
def handle_app_mention_event(event, say):
    """Handler per l'evento app_mention da Slack."""
    logger.info(f"[AI] Received app_mention event: {event}")
    handle_app_mention(event, say)


@app.event({"type": "message", "subtype": "thread_broadcast"})
def handle_message_thread_broadcast(event, say):
    handle_message(event, say)


@app.event({"type": "message", "subtype": "message_changed"})
def handle_message_changed(event):
    message = event["message"]
    conn, cursor = db_connect(database_path)
    try:
        cursor.execute(
            "UPDATE messages SET message = ? WHERE user = ? AND channel = ? AND timestamp = ?",
            (message["text"], message["user"], event["channel"], message["ts"]),
        )
        conn.commit()
    finally:
        conn.close()


@app.event({"type": "message", "subtype": "message_deleted"})
def handle_message_deleted(event):
    """Gestisce la cancellazione di un messaggio e rimuove i link associati dalla tabella posted_links."""
    deleted_ts = event.get("deleted_ts")
    channel = event.get("channel")
    
    if not deleted_ts:
        logger.warning("MESSAGE_DELETED: No deleted_ts in event, skipping link cleanup")
        return
    
    conn, cursor = db_connect(database_path)
    
    try:
        # Cerca tutti i link associati a questo messaggio
        cursor.execute(
            """
            SELECT normalized_url, original_url, message_timestamp, channel, permalink
            FROM posted_links
            WHERE message_timestamp = ?
            """,
            (deleted_ts,)
        )
        
        deleted_links = cursor.fetchall()
        
        if deleted_links:
            # Rimuovi i link dalla tabella
            cursor.execute(
                """
                DELETE FROM posted_links
                WHERE message_timestamp = ?
                """,
                (deleted_ts,)
            )
            conn.commit()
            
            # Logging dettagliato
            logger.info(
                f"MESSAGE_DELETED_LINKS_REMOVED: deleted_ts='{deleted_ts}' "
                f"channel='{channel}' "
                f"links_count={len(deleted_links)} "
                f"links={[(link[0], link[1]) for link in deleted_links]}"
            )
            
            # Log dettagliato per ogni link rimosso
            for link in deleted_links:
                logger.debug(
                    f"REMOVED_LINK: normalized_url='{link[0]}' "
                    f"original_url='{link[1]}' "
                    f"message_ts='{link[2]}' "
                    f"channel='{link[3]}' "
                    f"permalink='{link[4]}'"
                )
        else:
            logger.debug(
                f"MESSAGE_DELETED_NO_LINKS: deleted_ts='{deleted_ts}' "
                f"channel='{channel}' - No links found for this message"
            )
            
    except Exception as e:
        logger.error(f"Error handling message deletion for ts={deleted_ts}: {e}")
        conn.rollback()
    finally:
        conn.close()


@app.event("channel_created")
def handle_channel_created(event):
    channel_id = event["channel"]["id"]
    channel_is_private = app.client.conversations_info(channel=channel_id)["channel"]["is_private"]

    if channel_is_private is False:
        logger.debug("Channel id %s is public, joining", channel_id)
        app.client.conversations_join(channel=channel_id)

def init():
    # Initialize the DB if it doesn't exist
    conn, cursor = db_connect(database_path)
    migrate_db(conn, cursor)
    logger.info("Database migrated")

    # Update the users and channels in the DB and in the local memory mapping
    try:
        update_users(conn, cursor)
        update_channels(conn, cursor)
    except Exception as e:
        logger.error("Error updating users and channels: %s" % e)
    
    # Log stato iniziale della lista clown
    logger.info(f"[CLOWN] Bot initialized. Clown list is empty (will be populated via DM commands)")
        
        
def main():
    init()

    # Start the development server
    app.start(port=port)


if __name__ == "__main__":
    main()

# Make sure this function is accessible when imported
__all__ = ['update_users', 'app']
