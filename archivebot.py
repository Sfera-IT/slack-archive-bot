import argparse
import logging
import os
import traceback
import shlex
from sentence_transformers import SentenceTransformer
import numpy as np
import re
from urllib.parse import urlparse, urlunparse
from datetime import datetime, timedelta

from slack_bolt import App

from utils import db_connect, migrate_db
from url_cleaner import UrlCleaner

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
        model = SentenceTransformer('paraphrase-MiniLM-L6-v2')
        embeddings = model.encode(message)
    except:
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


def handle_query(event, cursor, say):
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


def check_and_store_links(message, permalink_dict, say):
    """Controlla se ci sono link nel messaggio e verifica duplicati."""
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
            normalized_url = normalize_url(original_url)
            
            # Controlla se esiste già un link normalizzato simile negli ultimi 30 giorni
            # Escludi il messaggio corrente dalla ricerca per evitare di trovare il link appena salvato
            thirty_days_ago = datetime.now() - timedelta(days=30)
            cursor.execute(
                """
                SELECT normalized_url, permalink, posted_date 
                FROM posted_links 
                WHERE normalized_url = ? 
                AND posted_date >= ?
                AND message_timestamp != ?
                ORDER BY posted_date DESC
                LIMIT 1
                """,
                (normalized_url, thirty_days_ago.isoformat(), message.get("ts", ""))
            )
            
            existing_link = cursor.fetchone()
            
            if existing_link:
                # Link duplicato trovato, rispondi al messaggio
                # existing_link è una tuple: (normalized_url, permalink, posted_date)
                original_permalink = existing_link[1] if len(existing_link) > 1 else ""
                response_text = f"Ciao {user_display_name}, questo link è stato già postato e lo trovi qui: {original_permalink}"
                
                try:
                    # Rispondi nel thread se il messaggio è parte di un thread, altrimenti come risposta normale
                    if "thread_ts" in message:
                        # Se è già un thread, rispondi nello stesso thread
                        say(text=response_text, thread_ts=message["thread_ts"])
                    else:
                        # Se non è un thread, crea una risposta nel thread del messaggio originale
                        say(text=response_text, thread_ts=message["ts"])
                except Exception as e:
                    logger.error(f"Error sending duplicate link notification: {e}")
            
            # Salva il link nella tabella (anche se è duplicato, vogliamo tracciarlo)
            # Controlla prima se non è già stato salvato per evitare duplicati nello stesso messaggio
            try:
                timestamp = float(message.get("ts", 0))
                posted_date = datetime.fromtimestamp(timestamp).isoformat()
                
                cursor.execute(
                    """
                    INSERT OR IGNORE INTO posted_links 
                    (normalized_url, original_url, message_timestamp, channel, permalink, posted_date)
                    VALUES (?, ?, ?, ?, ?, ?)
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


@app.event("member_left_channel")
def handle_left(event):
    conn, cursor = db_connect(database_path)
    cursor.execute(
        "DELETE FROM members WHERE channel = ? AND user = ?",
        (event["channel"], event["user"]),
    )
    conn.commit()


def handle_rename(event):
    channel = event["channel"]
    conn, cursor = db_connect(database_path)
    cursor.execute(
        "UPDATE channels SET name = ? WHERE id = ?", (channel["name"], channel["id"])
    )
    conn.commit()


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
    cursor.execute("UPDATE users SET name = ? WHERE id = ?", (new_username, user_id))
    conn.commit()


def handle_message(message, say):
    logger.debug(message)
    if "text" not in message or message["user"] == "USLACKBOT":
        return

    conn, cursor = db_connect(database_path)

    # If it's a DM, treat it as a search query
    if message["channel_type"] == "im":
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

        # Ensure that the user exists in the DB
        conn, cursor = db_connect(database_path)
        cursor.execute("SELECT * FROM users WHERE id = ?", (message["user"],))
        row = cursor.fetchone()
        if row is None:
            update_users(conn, cursor)
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


@app.event({"type": "message", "subtype": "thread_broadcast"})
def handle_message_thread_broadcast(event, say):
    handle_message(event, say)


@app.event({"type": "message", "subtype": "message_changed"})
def handle_message_changed(event):
    message = event["message"]
    conn, cursor = db_connect(database_path)
    cursor.execute(
        "UPDATE messages SET message = ? WHERE user = ? AND channel = ? AND timestamp = ?",
        (message["text"], message["user"], event["channel"], message["ts"]),
    )
    conn.commit()


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
        
        
def main():
    init()

    # Start the development server
    app.start(port=port)


if __name__ == "__main__":
    main()

# Make sure this function is accessible when imported
__all__ = ['update_users', 'app']
