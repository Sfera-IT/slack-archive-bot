import argparse
import json
import logging
import os
import traceback
from sentence_transformers import SentenceTransformer
import re
from datetime import datetime, timedelta

from slack_bolt import App
from openai import OpenAI

from ai_context import format_messages_for_prompt, get_ai_context_scope
from utils import db_connect, migrate_db
from url_cleaner import UrlCleaner
from sferait_context import (
    SFERAIT_SYSTEM_PROMPT,
    get_recent_messages,
    search_archive,
    build_enhanced_prompt
)

# Pre-compiled regex patterns
_X_COM_PATTERN = re.compile(r'^https?://(?:www\.)?x\.com/(.+)$', re.IGNORECASE)

# Admin users che possono eseguire comandi privilegiati (stessa lista di flask_app.py)
ADMIN_USERS = [
    'U011PQ7RHRT',
    'U011MV24J2W',
    'U0129HFHRJ4',
    'U011N8WRRD0',
    'U011Z26G449',
    'U011CKQ7D71',
    'U011KE4BF0W',
    'U011PN35BHT'
]

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

CHANNEL_RECAP_MESSAGE_LIMIT = 1000

# Auto-engagement su canale #trash
TRASH_CHANNEL_NAMES = ["trash"]
AUTO_ENGAGE_REPLY_THRESHOLD = 3       # reply count nel thread che triggera la decisione di engage
AUTO_CLOWN_USER_REPLY_THRESHOLD = 8   # reply degli UTENTI nel thread engaged per valutare auto-clown
AUTO_ENGAGE_COOLDOWN_SECONDS = 15 * 60  # cooldown globale tra nuovi engage in #trash
AUTO_ENGAGE_DECISION_MODEL = "gpt-4o-mini"
STOP_HINT_SUFFIX_TEMPLATE = "\n\n_per fermarmi: `<@{bot_id}> stop`_"

# URL cleaner instance loading local rules
_url_cleaner = UrlCleaner(rules_file=os.path.join(os.path.dirname(__file__), "url_rules.json"))

# Save the bot user's user ID e display name (per identificare i propri messaggi nei thread)
app._bot_user_id = app.client.auth_test()["user_id"]
try:
    _bot_profile = app.client.users_info(user=app._bot_user_id)["user"]["profile"]
    app._bot_display_name = (
        _bot_profile.get("display_name")
        or _bot_profile.get("real_name")
        or "bot"
    )
except Exception as _e:
    logger.warning(f"Impossibile recuperare display_name del bot: {_e}")
    app._bot_display_name = "bot"


MENTION_HINT_PROMPT = (
    "\n\n## Menzionare gli utenti\n"
    "Per menzionare un utente nella tua risposta, scrivi `<@USER_ID>` usando "
    "l'ID che trovi tra parentesi accanto al nome (es. `<@U011PQ7RHRT>`). "
    "NON scrivere `@nome` o `@DisplayName`: non viene riconosciuto da Slack. "
    "Se non hai un ID disponibile per quella persona, evita la mention.\n"
)

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
            expiry_date = datetime.now() + timedelta(hours=24)
            add_clown_user(cursor.connection, cursor, nickname_lower, expiry_date)
            clean_expired_clown_users(cursor.connection, cursor)  # Pulisci utenti scaduti
            say(f"✅ Aggiunto {nickname} alla lista clown per 24 ore (scade il {expiry_date.strftime('%Y-%m-%d %H:%M:%S')})")
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

    # Gestisci comando /optout <user_id> (solo admin)
    if text.startswith("/optout "):
        target_user_id = text[8:].strip()  # Rimuovi "/optout " e spazi
        # Rimuovi eventuali caratteri di menzione Slack <@U...>
        if target_user_id.startswith("<@") and target_user_id.endswith(">"):
            target_user_id = target_user_id[2:-1]
            # Rimuovi eventuale |nome dopo l'ID
            if "|" in target_user_id:
                target_user_id = target_user_id.split("|")[0]

        logger.info(f"[OPTOUT] Processing /optout command from {user_id} for target: '{target_user_id}'")

        # Verifica che l'utente sia admin
        if user_id not in ADMIN_USERS:
            logger.warning(f"[OPTOUT] Non-admin user {user_id} attempted to use /optout command")
            say("❌ Solo gli amministratori possono eseguire l'opt-out per altri utenti.")
            return

        if not target_user_id:
            say("❌ Devi specificare un user ID. Uso: /optout <user_id> oppure /optout @utente")
            return

        # Verifica che l'utente target esista
        cursor.execute("SELECT id, name FROM users WHERE id = ?", (target_user_id,))
        target_user = cursor.fetchone()
        if not target_user:
            say(f"❌ Utente con ID {target_user_id} non trovato nel database.")
            return

        target_name = target_user[1] if target_user else target_user_id

        # Verifica se è già in opt-out
        cursor.execute("SELECT user FROM optout WHERE user = ?", (target_user_id,))
        already_opted_out = cursor.fetchone()
        if already_opted_out:
            say(f"ℹ️ L'utente {target_name} ({target_user_id}) è già in opt-out.")
            return

        # Esegui l'opt-out
        try:
            cursor.execute(
                "INSERT INTO optout (user, timestamp) VALUES (?, CURRENT_TIMESTAMP)",
                (target_user_id,)
            )
            cursor.execute(
                'UPDATE messages SET message = "User opted out of archiving. This message has been deleted", user = "USLACKBOT", permalink = "" WHERE user = ?',
                (target_user_id,)
            )
            cursor.connection.commit()
            logger.info(f"[OPTOUT] Admin {user_id} executed opt-out for user {target_user_id} ({target_name})")
            say(f"✅ Opt-out eseguito per l'utente {target_name} ({target_user_id}). Tutti i suoi messaggi sono stati anonimizzati.")
        except Exception as e:
            logger.error(f"[OPTOUT] Error executing opt-out for {target_user_id}: {e}")
            cursor.connection.rollback()
            say(f"❌ Errore durante l'opt-out: {e}")
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
                            result = say(text=response_text, thread_ts=message["thread_ts"])
                            parent_ts = message["thread_ts"]
                            logger.debug(f"Sent duplicate notification in existing thread: {message.get('thread_ts')}")
                        else:
                            # Se non è un thread, crea una risposta nel thread del messaggio originale
                            result = say(text=response_text, thread_ts=message["ts"])
                            parent_ts = message["ts"]
                            logger.debug(f"Sent duplicate notification in new thread: {message.get('ts')}")

                        # Salva il timestamp dell'alert per poterlo cancellare se il messaggio parent viene cancellato
                        alert_ts = result.get("ts") if result else None
                        if alert_ts:
                            cursor.execute(
                                "INSERT OR REPLACE INTO duplicate_alerts (parent_message_ts, alert_message_ts, channel) VALUES (?, ?, ?)",
                                (parent_ts, alert_ts, message["channel"])
                            )
                            conn.commit()
                            logger.debug(f"Saved duplicate alert reference: parent_ts={parent_ts}, alert_ts={alert_ts}")

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
        # Intercetta il comando "stop" in un thread engaged di #trash
        try:
            if _maybe_handle_trash_stop(message, say):
                return
        except Exception as e:
            logger.error(f"[TRASH] Errore intercept stop: {e}")
            logger.error(traceback.format_exc())
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

        # Auto-engagement su #trash (solo reply in thread, gestito internamente)
        try:
            maybe_auto_engage_trash(message, say)
        except Exception as e:
            logger.error(f"[TRASH] Eccezione non gestita in maybe_auto_engage_trash: {e}")
            logger.error(traceback.format_exc())

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

        return build_ai_context_messages(messages)

    except Exception as e:
        logger.error(f"Error getting thread messages: {e}")
        logger.error(traceback.format_exc())
        return []


def get_channel_messages(channel, latest_ts=None, limit=CHANNEL_RECAP_MESSAGE_LIMIT):
    """Recupera gli ultimi N messaggi visibili nel canale."""
    try:
        all_messages = []
        cursor = None

        response = app.client.conversations_history(
            channel=channel,
            inclusive=True,
            latest=latest_ts,
            limit=min(limit, 200),
        )
        all_messages.extend(response.get("messages", []))

        while response.get("has_more", False) and len(all_messages) < limit:
            cursor = response.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break

            response = app.client.conversations_history(
                channel=channel,
                cursor=cursor,
                limit=min(limit - len(all_messages), 200),
            )
            all_messages.extend(response.get("messages", []))

        # conversations_history restituisce i messaggi dal più recente al meno recente.
        all_messages = all_messages[:limit]
        all_messages.sort(key=lambda x: float(x.get("ts", 0)))

        return build_ai_context_messages(all_messages)

    except Exception as e:
        logger.error(f"Error getting channel messages: {e}")
        logger.error(traceback.format_exc())
        return []


def build_ai_context_messages(messages):
    """Converte i messaggi Slack in un formato compatto per il prompt AI."""
    conn = None

    try:
        conn, db_cursor = db_connect(database_path)
        user_ids = sorted({
            msg.get("user")
            for msg in messages
            if msg.get("user") and msg.get("user") != "USLACKBOT"
        })
        user_names = get_user_name_map(db_cursor, user_ids)

        formatted_messages = []
        for msg in messages:
            user_id = msg.get("user", "")
            text = (msg.get("text") or "").strip()

            if not user_id or user_id == "USLACKBOT" or not text:
                continue

            formatted_messages.append({
                "user": user_names.get(user_id, "Unknown"),
                "user_id": user_id,
                "text": text,
                "ts": msg.get("ts", "")
            })

        return formatted_messages

    finally:
        if conn is not None:
            conn.close()


def get_user_name_map(db_cursor, user_ids):
    """Restituisce una mappa user_id -> nome visualizzato."""
    if not user_ids:
        return {}

    placeholders = ",".join("?" for _ in user_ids)
    db_cursor.execute(
        f"SELECT id, name, display_name, real_name FROM users WHERE id IN ({placeholders})",
        tuple(user_ids),
    )

    user_names = {}
    for user_id, name, display_name, real_name in db_cursor.fetchall():
        user_names[user_id] = display_name or name or real_name or "Unknown"

    return user_names




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
        message_ts = event.get("ts")  # Timestamp del messaggio che menziona il bot
        text = event.get("text", "")
        user_id = event.get("user", "")
        context_scope = get_ai_context_scope(event)
        response_thread_ts = event.get("thread_ts") if context_scope == "thread" else message_ts

        logger.info(
            f"[AI] Bot mentioned by user {user_id} in channel {channel}, "
            f"message_ts: {message_ts}, scope: {context_scope}, text: '{text[:100]}...'"
        )
        
        # Controlla throttle
        conn, cursor = db_connect(database_path)
        allowed, throttle_message, throttle_info = check_ai_throttle(conn, cursor, user_id, channel)
        
        logger.info(f"[AI] Throttle status: {throttle_info}")
        
        if not allowed:
            say(throttle_message, thread_ts=response_thread_ts)
            conn.close()
            return
        
        conn.close()
        
        # Rimuovi la menzione del bot dal testo
        bot_user_id = app._bot_user_id
        text = re.sub(rf'<@{bot_user_id}>', '', text).strip()
        
        if not text:
            if context_scope == "thread":
                text = "Puoi aiutarmi con questa conversazione?"
            else:
                text = (
                    f"Puoi fare un recap di questo canale basandoti sugli ultimi "
                    f"{CHANNEL_RECAP_MESSAGE_LIMIT} messaggi?"
                )

        if context_scope == "thread":
            logger.info(f"[AI] Fetching thread messages for thread_ts: {response_thread_ts}")
            context_messages = get_thread_messages(channel, response_thread_ts)
            context_label = "questa conversazione Slack"
        else:
            logger.info(
                f"[AI] Fetching last {CHANNEL_RECAP_MESSAGE_LIMIT} channel messages "
                f"up to ts {message_ts}"
            )
            context_messages = get_channel_messages(
                channel,
                latest_ts=message_ts,
                limit=CHANNEL_RECAP_MESSAGE_LIMIT,
            )
            context_label = (
                f"gli ultimi {CHANNEL_RECAP_MESSAGE_LIMIT} messaggi visibili di questo canale Slack"
            )

        if not context_messages:
            say("Non ho trovato messaggi utili in questo contesto.", thread_ts=response_thread_ts)
            return
        
        logger.info(f"[AI] Found {len(context_messages)} messages for {context_scope} context")

        formatted_messages = format_messages_for_prompt(context_messages)
        
        # === CONTESTO POTENZIATO SFERAIT ===
        # 1. Recupera messaggi recenti per catturare lo "stile" della community
        conn_ctx, cursor_ctx = db_connect(database_path)
        recent_context = get_recent_messages(
            conn_ctx, cursor_ctx, 
            limit=30, 
            exclude_channel=channel, 
            hours=48
        )
        logger.info(f"[AI] Retrieved {len(recent_context)} recent messages for ambient context")

        # 2. Cerca nell'archivio messaggi rilevanti alla domanda
        archive_results = search_archive(conn_ctx, cursor_ctx, text, limit=5)
        logger.info(f"[AI] Found {len(archive_results)} relevant archive messages")
        conn_ctx.close()

        # 3. Usa il system prompt SferaIT (+ hint per le mention native)
        system_prompt = SFERAIT_SYSTEM_PROMPT + MENTION_HINT_PROMPT

        # 4. Costruisci prompt arricchito
        user_prompt = build_enhanced_prompt(
            thread_messages=f"Fonte contesto: {context_label}\n\n{formatted_messages}",
            user_question=text,
            recent_context=recent_context,
            archive_results=archive_results
        )
        
        # Chiama ChatGPT
        openai_api_key = os.environ.get("OPENAI_API_KEY")
        if not openai_api_key:
            logger.error("[AI] OPENAI_API_KEY not set")
            say("Errore: chiave API OpenAI non configurata.", thread_ts=response_thread_ts)
            return
        
        client = OpenAI(api_key=openai_api_key)
        
        logger.info(f"[AI] Sending request to OpenAI with {len(context_messages)} messages")
        
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
        say(final_response, thread_ts=response_thread_ts)
        
    except Exception as e:
        logger.error(f"[AI] Error handling app mention: {e}")
        logger.error(traceback.format_exc())
        try:
            say("Mi dispiace, c'è stato un errore nel processare la tua richiesta.", thread_ts=event.get("thread_ts", event.get("ts")))
        except:
            pass


def _is_trash_channel(channel_id, cursor):
    """True se il canale corrisponde a uno dei TRASH_CHANNEL_NAMES."""
    cursor.execute("SELECT name FROM channels WHERE id = ?", (channel_id,))
    row = cursor.fetchone()
    if not row or not row[0]:
        return False
    return row[0].lower() in TRASH_CHANNEL_NAMES


def _format_thread_for_llm(messages):
    """Formatta i messaggi del thread come 'Nome (<@USER_ID>): testo' per il prompt LLM.
    L'inclusione dell'ID consente al modello di generare mention Slack native."""
    lines = []
    for m in messages:
        user = m.get("user", "Unknown")
        uid = m.get("user_id", "")
        text = m.get("text", "")
        if uid:
            lines.append(f"{user} (<@{uid}>): {text}")
        else:
            lines.append(f"{user}: {text}")
    return "\n".join(lines)


def _engage_cooldown_active(cursor):
    """True se nell'ultimo AUTO_ENGAGE_COOLDOWN_SECONDS è già stato fatto un engage."""
    cutoff = datetime.now().timestamp() - AUTO_ENGAGE_COOLDOWN_SECONDS
    cursor.execute(
        "SELECT 1 FROM trash_engaged_threads WHERE engaged = 1 AND evaluated_at > ? LIMIT 1",
        (cutoff,),
    )
    return cursor.fetchone() is not None


def _decide_engage(thread_messages, openai_client):
    """LLM-call: decide se il bot deve inserirsi nel thread #trash. Ritorna (engage: bool, reply: str)."""
    thread_text = _format_thread_for_llm(thread_messages)
    system = (
        SFERAIT_SYSTEM_PROMPT
        + MENTION_HINT_PROMPT
        + "\n\n## Modalità AUTO-ENGAGE\n"
        "Stai osservando un thread su #trash a cui nessuno ti ha chiesto di partecipare. "
        "Hai tutta la libertà di stare zitto. Inserisciti solo se: "
        "(a) hai una battuta o un commento sarcastico che vale la pena leggere, "
        "(b) qualcuno sta dicendo una boiata che puoi smontare, "
        "(c) c'è una contraddizione o un inside-joke ovvio da rinfacciare. "
        "NON inserirti per riassumere o spiegare cose ovvie. "
        "Nel campo 'reply' NON prefissare con il tuo nome utente. "
        "Ritorna SOLO JSON valido: {\"engage\": bool, \"reply\": str}. "
        "Se engage=false, reply può essere stringa vuota."
    )
    user_msg = f"Thread fino ad ora:\n{thread_text}\n\nDecidi se inserirti."
    resp = openai_client.chat.completions.create(
        model=AUTO_ENGAGE_DECISION_MODEL,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user_msg},
        ],
        max_tokens=600,
        temperature=0.8,
        response_format={"type": "json_object"},
    )
    raw = resp.choices[0].message.content or "{}"
    data = json.loads(raw)
    return bool(data.get("engage")), _strip_bot_self_prefix((data.get("reply") or "").strip())


def _decide_clown(thread_messages, openai_client):
    """LLM-call: decide se qualcuno nel thread merita il clown. Ritorna (user_name: str|None, reason: str|None)."""
    thread_text = _format_thread_for_llm(thread_messages)
    system = (
        "Sei il giudice clown di SferaIT. Stai osservando un thread di #trash. "
        "Decidi se UN utente merita la reaction 🤡 per 24 ore.\n\n"
        "**DEFAULT: NESSUN CLOWN.** La maggior parte dei thread NON ha clown. "
        "Solo una piccola minoranza di casi merita il riconoscimento.\n\n"
        "Assegna il clown SOLO se è chiaramente evidente uno di questi: "
        "(a) contraddizione palese e dimostrabile (ha detto X e poi l'opposto), "
        "(b) autogol clamoroso (si è incastrato da solo, ha dimostrato di non capire ciò di cui parla), "
        "(c) idea oggettivamente idiota argomentata seriamente come geniale, "
        "(d) figura ridicola lampante che chiunque noterebbe.\n\n"
        "NON assegnare per: tono infantile, ripetizioni, domande banali, frasi normali, "
        "battute scemenze, scherzi, opinioni personali, lamentele, sfoghi. "
        "NON considerare MAI il bot stesso, USLACKBOT o utenti generici.\n\n"
        "Se hai anche solo un dubbio → clown_user=null. "
        "È meglio non dare il clown a qualcuno che lo merita, "
        "piuttosto che darlo a qualcuno che non lo merita.\n\n"
        "Ritorna SOLO JSON valido: {\"clown_user\": str|null, \"reason\": str|null}. "
        "Il campo clown_user, se non null, deve essere ESATTAMENTE il nome utente come appare nel thread."
    )
    user_msg = f"Thread:\n{thread_text}\n\nChi (se qualcuno) è clown?"
    resp = openai_client.chat.completions.create(
        model=AUTO_ENGAGE_DECISION_MODEL,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user_msg},
        ],
        max_tokens=300,
        temperature=0.5,
        response_format={"type": "json_object"},
    )
    raw = resp.choices[0].message.content or "{}"
    data = json.loads(raw)
    user = data.get("clown_user")
    reason = data.get("reason")
    if not user or not isinstance(user, str):
        return None, None
    return user.strip(), (reason or "").strip()


def _strip_bot_self_prefix(text):
    """Rimuove eventuali prefissi tipo 'slack-archive-bot:' che l'LLM aggiunge in testa.
    Funziona ricorsivamente in caso di prefissi multipli e gestisce sia il display
    name del bot che alias generici."""
    if not text:
        return text
    bot_name = (app._bot_display_name or "").strip().lower()
    pattern_parts = ["slack-archive-bot", "bot", "assistant"]
    if bot_name and bot_name not in pattern_parts:
        pattern_parts.append(re.escape(bot_name))
    pattern = r"^\s*(?:" + "|".join(pattern_parts) + r")\s*:\s*"
    for _ in range(5):
        new_text = re.sub(pattern, "", text, flags=re.IGNORECASE)
        if new_text == text:
            break
        text = new_text
    return text.strip()


def _reply_every_n(user_replies_count):
    """Tabella di decay: ogni quanti reply utente il bot deve rispondere.
    Thread brevi -> 1 (sempre). Thread lunghissimi -> 1 ogni 10."""
    if user_replies_count <= 25:
        return 1
    if user_replies_count <= 40:
        return 2
    if user_replies_count <= 60:
        return 3
    if user_replies_count <= 80:
        return 5
    if user_replies_count <= 120:
        return 7
    return 10


def _should_reply_now(thread_messages, bot_user_id):
    """Decide se il bot debba rispondere al nuovo reply utente.
    Risponde se sono passati >= N reply utente dall'ultimo intervento del bot,
    dove N cresce con la lunghezza del thread (vedi _reply_every_n)."""
    ordered = sorted(thread_messages, key=lambda m: float(m.get("ts", 0) or 0))

    user_replies_count = sum(
        1 for m in ordered if m.get("user_id") and m.get("user_id") != bot_user_id
    )

    last_bot_idx = -1
    for i, m in enumerate(ordered):
        if m.get("user_id") == bot_user_id:
            last_bot_idx = i

    tail = ordered[last_bot_idx + 1:] if last_bot_idx >= 0 else ordered
    replies_since_last_bot = sum(
        1 for m in tail if m.get("user_id") and m.get("user_id") != bot_user_id
    )

    n_required = _reply_every_n(user_replies_count)
    return replies_since_last_bot >= n_required, user_replies_count, replies_since_last_bot, n_required


def _auto_reply_in_thread(channel, thread_ts, thread_messages, openai_client, say):
    """Risposta del bot in un thread già engaged. Usa SFERAIT_SYSTEM_PROMPT.
    Costruisce la sequenza messaggi role-based (assistant per i propri reply)
    per evitare che il modello si auto-citi prefissando con il proprio nome."""
    bot_user_id = app._bot_user_id

    chat_messages = [
        {"role": "system", "content": SFERAIT_SYSTEM_PROMPT + MENTION_HINT_PROMPT}
    ]
    for m in thread_messages:
        text = m.get("text", "")
        if not text:
            continue
        if m.get("user_id") == bot_user_id:
            chat_messages.append({"role": "assistant", "content": text})
        else:
            user = m.get("user", "Unknown")
            uid = m.get("user_id", "")
            content = f"{user} (<@{uid}>): {text}" if uid else f"{user}: {text}"
            chat_messages.append({"role": "user", "content": content})

    chat_messages.append({
        "role": "user",
        "content": (
            "Continua la conversazione con UN solo messaggio, breve e in tono. "
            "Non prefissare la risposta con il tuo nome utente. "
            "Scrivi direttamente il contenuto come se stessi parlando in chat."
        ),
    })

    resp = openai_client.chat.completions.create(
        model="gpt-4o",
        messages=chat_messages,
        max_tokens=800,
        temperature=0.8,
    )
    reply = (resp.choices[0].message.content or "").strip()
    reply = _strip_bot_self_prefix(reply)
    if reply:
        reply += STOP_HINT_SUFFIX_TEMPLATE.format(bot_id=app._bot_user_id)
        say(reply, thread_ts=thread_ts)


_STOP_KEYWORD_RE = re.compile(
    r"^\s*(stop|basta|smettila|silenzio|zitto|shut\s*up)\s*[.!?]*\s*$",
    re.IGNORECASE,
)


def _maybe_handle_trash_stop(message, say):
    """Se il messaggio è una mention al bot in un thread engaged di #trash con
    testo 'stop' (o equivalente), marca il thread come stopped e ritorna True.
    Altrimenti ritorna False."""
    thread_ts = message.get("thread_ts")
    ts = message.get("ts")
    channel = message.get("channel")
    text = message.get("text", "") or ""
    bot_user_id = app._bot_user_id

    # Solo reply in thread (no root, no DM, no canali random)
    if not thread_ts or thread_ts == ts:
        return False

    # Strip della mention al bot
    stripped = re.sub(rf"<@{bot_user_id}>", "", text).strip()
    if not _STOP_KEYWORD_RE.match(stripped):
        return False

    conn, cursor = db_connect(database_path)
    try:
        if not _is_trash_channel(channel, cursor):
            return False

        cursor.execute(
            "SELECT engaged FROM trash_engaged_threads WHERE thread_ts = ? AND channel = ?",
            (thread_ts, channel),
        )
        row = cursor.fetchone()
        if not row or not row[0]:
            # Non c'e un engage attivo da fermare
            return False

        cursor.execute(
            "UPDATE trash_engaged_threads SET stopped = 1 "
            "WHERE thread_ts = ? AND channel = ?",
            (thread_ts, channel),
        )
        conn.commit()
        logger.info(f"[TRASH] Thread {thread_ts} stoppato su richiesta utente")
        try:
            app.client.reactions_add(channel=channel, timestamp=ts, name="zipper_mouth_face")
        except Exception as e:
            logger.warning(f"[TRASH] Impossibile aggiungere reaction stop: {e}")
        say("Ok, mi zitto su questo thread. :zipper_mouth_face:", thread_ts=thread_ts)
        return True
    finally:
        conn.close()


def maybe_auto_engage_trash(message, say):
    """Orchestra auto-engagement e auto-clown su thread di #trash.
    Chiamato da handle_message per ogni messaggio (solo i reply in thread fanno qualcosa)."""
    try:
        thread_ts = message.get("thread_ts")
        ts = message.get("ts")
        channel = message.get("channel")
        msg_user = message.get("user")

        # Skip i messaggi del bot stesso per evitare loop infiniti
        if msg_user == app._bot_user_id:
            return

        # Solo reply in thread, mai messaggi root
        if not thread_ts or thread_ts == ts:
            return

        conn, cursor = db_connect(database_path)
        try:
            if not _is_trash_channel(channel, cursor):
                return

            # Recupera tutti i messaggi del thread (in ordine)
            thread_messages = get_thread_messages(channel, thread_ts)
            if not thread_messages:
                return

            # Esclude messaggi del bot stesso e il root dal count "reply degli utenti"
            bot_user_id = app._bot_user_id
            user_reply_count = sum(
                1 for m in thread_messages
                if m.get("ts") != thread_ts and m.get("user_id") != bot_user_id
            )

            # Stato del thread nel DB
            cursor.execute(
                "SELECT decided, engaged, clown_assigned, stopped FROM trash_engaged_threads "
                "WHERE thread_ts = ? AND channel = ?",
                (thread_ts, channel),
            )
            row = cursor.fetchone()
            decided = bool(row[0]) if row else False
            engaged = bool(row[1]) if row else False
            clown_assigned = row[2] if row else None
            stopped = bool(row[3]) if row and len(row) > 3 else False

            # Se l'utente ha detto stop, il bot non interviene piu in questo thread
            if stopped:
                logger.info(f"[TRASH] Thread {thread_ts} stoppato dall'utente, skip")
                return

            openai_api_key = os.environ.get("OPENAI_API_KEY")
            if not openai_api_key:
                logger.warning("[TRASH] OPENAI_API_KEY non configurata, skip auto-engage")
                return
            client = OpenAI(api_key=openai_api_key)

            now_ts = datetime.now().timestamp()

            # CASO A: thread non ancora valutato e abbiamo raggiunto la soglia → decidi engage
            if not decided and user_reply_count >= AUTO_ENGAGE_REPLY_THRESHOLD:
                if _engage_cooldown_active(cursor):
                    logger.info(
                        f"[TRASH] Cooldown attivo, skip decisione engage per thread {thread_ts}"
                    )
                    # Marca decided=1 engaged=0 per non rivalutare ogni reply
                    cursor.execute(
                        "INSERT OR REPLACE INTO trash_engaged_threads "
                        "(thread_ts, channel, decided, engaged, evaluated_at, last_reply_ts) "
                        "VALUES (?, ?, 1, 0, ?, ?)",
                        (thread_ts, channel, now_ts, ts),
                    )
                    conn.commit()
                    return

                logger.info(f"[TRASH] Decisione engage per thread {thread_ts} ({user_reply_count} reply)")
                engage, reply = _decide_engage(thread_messages, client)
                cursor.execute(
                    "INSERT OR REPLACE INTO trash_engaged_threads "
                    "(thread_ts, channel, decided, engaged, evaluated_at, last_reply_ts) "
                    "VALUES (?, ?, 1, ?, ?, ?)",
                    (thread_ts, channel, 1 if engage else 0, now_ts, ts),
                )
                conn.commit()

                if engage and reply:
                    logger.info(f"[TRASH] Engaging thread {thread_ts}")
                    reply += STOP_HINT_SUFFIX_TEMPLATE.format(bot_id=bot_user_id)
                    say(reply, thread_ts=thread_ts)
                else:
                    logger.info(f"[TRASH] Pass su thread {thread_ts}")
                return

            # CASO B: thread già engaged → rispondi (con decay sulla lunghezza)
            if engaged and message.get("user") != bot_user_id:
                should_reply, total_user_replies, since_last, n_req = _should_reply_now(
                    thread_messages, bot_user_id
                )
                logger.info(
                    f"[TRASH] Thread {thread_ts} engaged - "
                    f"user_replies_total={total_user_replies}, "
                    f"since_last_bot={since_last}, n_required={n_req}, "
                    f"should_reply={should_reply}"
                )
                if should_reply:
                    _auto_reply_in_thread(channel, thread_ts, thread_messages, client, say)
                cursor.execute(
                    "UPDATE trash_engaged_threads SET last_reply_ts = ? "
                    "WHERE thread_ts = ? AND channel = ?",
                    (ts, thread_ts, channel),
                )
                conn.commit()

                # CASO C: thread engaged abbastanza lungo, clown non ancora assegnato → valuta
                # Conta SOLO i reply degli utenti (escludendo bot e root) per evitare di gonfiare
                # il count con le risposte automatiche del bot stesso
                user_reply_total = sum(
                    1 for m in thread_messages
                    if m.get("ts") != thread_ts and m.get("user_id") != bot_user_id
                )
                if not clown_assigned and user_reply_total >= AUTO_CLOWN_USER_REPLY_THRESHOLD:
                    logger.info(
                        f"[TRASH] Valuto clown su thread {thread_ts} "
                        f"({user_reply_total} reply utenti, {len(thread_messages)} msg totali)"
                    )
                    clown_name, reason = _decide_clown(thread_messages, client)
                    if clown_name:
                        nickname_lower = clown_name.lower()
                        expiry = datetime.now() + timedelta(hours=24)
                        add_clown_user(conn, cursor, nickname_lower, expiry)
                        cursor.execute(
                            "UPDATE trash_engaged_threads SET clown_assigned = ? "
                            "WHERE thread_ts = ? AND channel = ?",
                            (nickname_lower, thread_ts, channel),
                        )
                        conn.commit()
                        announce = f"🤡 {clown_name}, ti sei meritato il clown per 24h."
                        if reason:
                            announce += f" Motivo: {reason}"
                        say(announce, thread_ts=thread_ts)
                    else:
                        # Marca comunque come "valutato" per evitare rivalutazioni continue
                        cursor.execute(
                            "UPDATE trash_engaged_threads SET clown_assigned = ? "
                            "WHERE thread_ts = ? AND channel = ?",
                            ("__none__", thread_ts, channel),
                        )
                        conn.commit()
        finally:
            conn.close()

    except Exception as e:
        logger.error(f"[TRASH] Errore auto-engage: {e}")
        logger.error(traceback.format_exc())


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
    message = event.get("message", {})

    # Slack a volte invia message_changed quando un messaggio viene cancellato
    # In questo caso, il messaggio ha subtype "tombstone" o non ha "text"
    if message.get("subtype") == "tombstone" or "text" not in message:
        # Tratta come cancellazione
        deleted_ts = event.get("previous_message", {}).get("ts") or message.get("ts")
        if deleted_ts:
            logger.info(f"MESSAGE_CHANGED_AS_DELETED: Detected deletion via message_changed, ts={deleted_ts}")
            handle_message_deleted_logic(deleted_ts, event.get("channel"))
        return

    conn, cursor = db_connect(database_path)
    try:
        cursor.execute(
            "UPDATE messages SET message = ? WHERE user = ? AND channel = ? AND timestamp = ?",
            (message["text"], message["user"], event["channel"], message["ts"]),
        )
        conn.commit()
    finally:
        conn.close()


def handle_message_deleted_logic(deleted_ts, channel):
    """Logica comune per gestire la cancellazione di un messaggio."""
    if not deleted_ts:
        logger.warning("MESSAGE_DELETED: No deleted_ts provided, skipping cleanup")
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

        # Cerca e cancella eventuali alert di link duplicati associati a questo messaggio
        cursor.execute(
            "SELECT alert_message_ts, channel FROM duplicate_alerts WHERE parent_message_ts = ?",
            (deleted_ts,)
        )
        alert = cursor.fetchone()

        if alert:
            alert_ts, alert_channel = alert
            try:
                app.client.chat_delete(channel=alert_channel, ts=alert_ts)
                logger.info(f"DUPLICATE_ALERT_DELETED: Deleted orphaned duplicate alert: alert_ts='{alert_ts}' channel='{alert_channel}'")
            except Exception as e:
                logger.warning(f"Could not delete duplicate alert {alert_ts}: {e}")

            # Rimuovi dalla tabella duplicate_alerts
            cursor.execute("DELETE FROM duplicate_alerts WHERE parent_message_ts = ?", (deleted_ts,))
            conn.commit()

    except Exception as e:
        logger.error(f"Error handling message deletion for ts={deleted_ts}: {e}")
        conn.rollback()
    finally:
        conn.close()


@app.event({"type": "message", "subtype": "message_deleted"})
def handle_message_deleted(event):
    """Gestisce la cancellazione di un messaggio via evento message_deleted."""
    # deleted_ts può essere direttamente nell'evento o in previous_message.ts
    deleted_ts = event.get("deleted_ts") or event.get("previous_message", {}).get("ts")
    channel = event.get("channel")
    logger.info(f"MESSAGE_DELETED_EVENT: deleted_ts={deleted_ts}, channel={channel}")
    handle_message_deleted_logic(deleted_ts, channel)


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
