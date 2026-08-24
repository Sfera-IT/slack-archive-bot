import argparse
import json
import logging
import os
import traceback
from sentence_transformers import SentenceTransformer
import re
import threading
from datetime import datetime, timedelta

from slack_bolt import App
from openai import OpenAI

from ai_agent import DEFAULT_AI_MODEL, generate_text_response, run_archive_agent
from ai_context import format_messages_for_prompt, get_ai_context_scope, is_engage_request
from ai_diagnostics import (
    get_ai_debug_recipients,
    is_ai_debug_enabled,
    new_ai_error_id,
    send_private_ai_error,
    set_ai_debug_enabled,
)
from archive_search import ArchiveSearchEngine, EvidenceRegistry
from utils import claim_xcancel_alert, db_connect, finalize_xcancel_alert, migrate_db
from url_cleaner import UrlCleaner
from xcancel import build_xcancel_response_text
from link_enrichment import LinkEnrichmentWorker
from link_duplicates import (
    collect_deleted_message_alerts,
    deliver_duplicate_alert,
    extract_external_links,
    finalize_stored_alert_cleanup,
    prepare_exact_duplicate_alert,
    prepare_enriched_duplicate_alerts,
    reconcile_edited_message_links,
    route_link_message_event,
)
from sferait_context import SFERAIT_SYSTEM_PROMPT

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
_sentence_transformer_lock = threading.Lock()
_link_enrichment_worker = None


def _get_sentence_transformer():
    """Get or initialize the SentenceTransformer model (lazy loading)."""
    global _sentence_transformer_model
    if _sentence_transformer_model is None:
        with _sentence_transformer_lock:
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
    # Importing the Flask application must not require a successful Slack API
    # round-trip.  Identity is refreshed lazily when messages are processed.
    token_verification_enabled=False,
)

CHANNEL_RECAP_MESSAGE_LIMIT = 1000

# Auto-engagement storico su #trash: lasciato nel codice per compatibilità, ma non viene più chiamato.
TRASH_CHANNEL_NAMES = ["trash"]
AUTO_ENGAGE_REPLY_THRESHOLD = 3       # reply count nel thread che triggera la decisione di engage
AUTO_CLOWN_USER_REPLY_THRESHOLD = 8   # reply degli UTENTI nel thread engaged per valutare auto-clown
AUTO_ENGAGE_COOLDOWN_SECONDS = 15 * 60  # cooldown globale tra nuovi engage nei canali auto-engage
AI_RESPONSE_MODEL = os.getenv("OPENAI_MODEL", DEFAULT_AI_MODEL)
AI_REASONING_EFFORT = os.getenv("OPENAI_REASONING_EFFORT", "medium")
AUTO_ENGAGE_DECISION_MODEL = os.getenv("OPENAI_DECISION_MODEL", AI_RESPONSE_MODEL)
STOP_HINT_SUFFIX_TEMPLATE = "\n\n_per fermarmi: `<@{bot_id}> stop`_"
ENGAGED_THREAD_STOP_ACTION_ID = "trash_stop_thread"  # legacy action_id: non cambiarlo, i bottoni esistenti lo usano.

# URL cleaner instance loading local rules
_url_cleaner = UrlCleaner(rules_file=os.path.join(os.path.dirname(__file__), "url_rules.json"))

# Slack identity is stable for this app and can be overridden at runtime.  A
# transient Slack outage must not prevent Gunicorn from importing the app.
app._bot_user_id = os.getenv("SLACK_BOT_USER_ID", "U02V2KN5JKS")
app._bot_display_name = "bot"
app._bot_identity_verified = bool(app._bot_user_id)
_bot_identity_lock = threading.Lock()


def _initialize_bot_identity():
    """Refresh bot identity without making message handling depend on Slack."""
    with _bot_identity_lock:
        if app._bot_identity_verified:
            return app._bot_user_id
        try:
            authenticated_user_id = app.client.auth_test()["user_id"]
            app._bot_user_id = authenticated_user_id
            app._bot_identity_verified = True
            try:
                bot_profile = app.client.users_info(user=authenticated_user_id)["user"][
                    "profile"
                ]
                app._bot_display_name = (
                    bot_profile.get("display_name")
                    or bot_profile.get("real_name")
                    or "bot"
                )
            except Exception as error:
                logger.warning("Could not resolve bot display name: %s", error)
        except Exception as error:
            logger.warning(
                "Could not refresh Slack bot identity; using configured ID %s: %s",
                app._bot_user_id,
                error,
            )
        return app._bot_user_id


MENTION_HINT_PROMPT = (
    "\n\n## Menzionare gli utenti\n"
    "Per menzionare un utente nella tua risposta, scrivi `<@USER_ID>` usando "
    "l'ID che trovi tra parentesi accanto al nome (es. `<@U011PQ7RHRT>`). "
    "NON scrivere `@nome` o `@DisplayName`: non viene riconosciuto da Slack. "
    "Se non hai un ID disponibile per quella persona, evita la mention.\n"
)


def _report_ai_error(exception, *, event, source, say=None, thread_ts=None):
    """Log one AI failure, notify opted-in admins, and post a safe reference."""
    error_id = new_ai_error_id()
    logger.error(
        "[AI][%s][%s] Request failed: %s",
        error_id,
        source,
        exception,
        exc_info=True,
    )

    recipients = []
    try:
        conn, cursor = db_connect(database_path)
        try:
            recipients = get_ai_debug_recipients(cursor)
        finally:
            conn.close()
    except Exception as subscription_error:
        logger.exception(
            "[AI][%s] Failed to load private debug subscribers: %s",
            error_id,
            subscription_error,
        )

    for recipient_user_id in recipients:
        try:
            send_private_ai_error(
                app.client,
                exception,
                event=event,
                model=AI_RESPONSE_MODEL,
                reasoning_effort=AI_REASONING_EFFORT,
                error_id=error_id,
                recipient_user_id=recipient_user_id,
                source=source,
            )
        except Exception as debug_error:
            logger.exception(
                "[AI][%s] Failed to send private debug to subscriber %s: %s",
                error_id,
                recipient_user_id,
                debug_error,
            )

    if say is not None:
        try:
            say(
                "Mi dispiace, c'è stato un errore nel processare la tua richiesta. "
                f"Riferimento: `{error_id}`.",
                thread_ts=thread_ts or event.get("thread_ts") or event.get("ts"),
            )
        except Exception:
            logger.exception("[AI][%s] Failed to post public error reference", error_id)
    return error_id

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
    refreshed_channel_ids = []
    for channel in channels:
        if channel["is_member"]:
            channel_id, channel_name, channel_is_private, members = get_channel_info(
                channel["id"]
            )

            channel_args.append((channel_name, channel_id, channel_is_private))
            refreshed_channel_ids.append((channel_id,))
            member_args.extend(members)

    cursor.executemany(
        """
        INSERT INTO channels(name, id, is_private) VALUES(?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            name = excluded.name,
            is_private = excluded.is_private
        """,
        channel_args,
    )
    # Refresh each channel atomically instead of appending the same members on
    # every boot. Production databases may enforce UNIQUE(channel, user), while
    # older databases otherwise accumulate duplicate rows indefinitely.
    cursor.executemany(
        "DELETE FROM members WHERE channel = ?", refreshed_channel_ids
    )
    cursor.executemany(
        "INSERT INTO members(channel, user) VALUES(?, ?)",
        sorted(set(member_args)),
    )
    conn.commit()


def _parse_clown_expiry(expiry_date):
    """Parsing robusto della scadenza clown.

    Non ci fidiamo del confronto lessicografico su TEXT: se in futuro il formato
    cambia leggermente, un clown può restare attivo per sempre. Qui convertiamo
    sempre a datetime e, se il valore è illeggibile, lo consideriamo scaduto.
    """
    if not expiry_date:
        return None
    try:
        return datetime.fromisoformat(str(expiry_date))
    except Exception:
        try:
            return datetime.strptime(str(expiry_date), "%Y-%m-%d %H:%M:%S")
        except Exception:
            logger.warning(f"[CLOWN] expiry_date non parsabile, considero scaduto: {expiry_date}")
            return None


def _is_clown_expired(expiry_date, now=None):
    expiry = _parse_clown_expiry(expiry_date)
    if expiry is None:
        return True
    return expiry <= (now or datetime.now())


def clean_expired_clown_users(conn, cursor):
    """Rimuove gli utenti scaduti dalla lista clown nel database."""
    now = datetime.now()
    cursor.execute("SELECT nickname, expiry_date FROM clown_users")
    current_users = cursor.fetchall()
    expired = [nickname for nickname, expiry in current_users if _is_clown_expired(expiry, now)]
    
    if expired:
        logger.info(f"[CLOWN] Cleaning {len(expired)} expired users: {expired}")
        cursor.executemany("DELETE FROM clown_users WHERE nickname = ?", [(nickname,) for nickname in expired])
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
    cursor.execute("SELECT expiry_date FROM clown_users WHERE nickname = ?", (nickname_lower,))
    result = cursor.fetchone()
    if result is None:
        return False

    expiry_date = result[0]
    if _is_clown_expired(expiry_date):
        logger.info(f"[CLOWN] {nickname_lower} expired at {expiry_date}; removing before reaction")
        cursor.execute("DELETE FROM clown_users WHERE nickname = ?", (nickname_lower,))
        conn.commit()
        return False

    return True


def add_clown_user(conn, cursor, nickname_lower, expiry_date, *,
                   source="manual", assigned_by=None, reason=None,
                   thread_ts=None, channel=None):
    """Aggiunge un utente alla lista clown nel database.

    Metadata di tracking:
    - source: 'manual' (comando /clown) o 'auto' (auto-clown del bot)
    - assigned_by: user_id Slack di chi ha invocato (o 'auto')
    - reason: motivo, popolato dall'auto-clown
    - thread_ts, channel: contesto del clown auto
    """
    expiry_str = expiry_date.isoformat()
    assigned_at = datetime.now().timestamp()
    cursor.execute(
        "INSERT OR REPLACE INTO clown_users "
        "(nickname, expiry_date, source, assigned_by, assigned_at, reason, thread_ts, channel) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (nickname_lower, expiry_str, source, assigned_by, assigned_at, reason, thread_ts, channel),
    )
    conn.commit()
    logger.info(
        f"[CLOWN] Added {nickname_lower} (source={source}, by={assigned_by}) expires: {expiry_date}"
    )


def remove_clown_user(conn, cursor, nickname_lower):
    """Rimuove un utente dalla lista clown nel database."""
    cursor.execute("DELETE FROM clown_users WHERE nickname = ?", (nickname_lower,))
    conn.commit()
    logger.info(f"[CLOWN] Removed {nickname_lower} from clown list in DB")


def _handle_ai_debug_command(text, user_id, cursor, reply):
    """Handle admin-only AI debug opt-in from a DM or native slash command."""
    debug_match = re.fullmatch(
        r"/?debug(?:\s+(on|off|status))?",
        (text or "").strip(),
        flags=re.IGNORECASE,
    )
    if not debug_match:
        return False

    if user_id not in ADMIN_USERS:
        logger.warning("[AI][DEBUG] Non-admin user %s attempted debug opt-in", user_id)
        reply("❌ Solo gli amministratori possono ricevere il debug AI privato.")
        return True

    action = (debug_match.group(1) or "toggle").lower()
    currently_enabled = is_ai_debug_enabled(cursor, user_id)
    if action == "status":
        state = "attivo" if currently_enabled else "disattivato"
        reply(f"🛠️ Debug AI privato: *{state}*.")
        return True

    enabled = not currently_enabled if action == "toggle" else action == "on"
    set_ai_debug_enabled(cursor, user_id, enabled)
    cursor.connection.commit()
    if enabled:
        reply(
            "🛠️ Debug AI privato attivato. Riceverai gli errori sanitizzati "
            "di menzioni, ricerche ed engage. Invia `debug off` in DM per disattivarlo."
        )
    else:
        reply("🛠️ Debug AI privato disattivato.")
    return True


@app.command("/debug")
def handle_ai_debug_slash_command(ack, command, respond):
    """Handle the optional native Slack /debug command when configured in the app."""
    ack()
    user_id = command.get("user_id", "unknown")
    command_text = "debug"
    if (arguments := (command.get("text") or "").strip()):
        command_text += f" {arguments}"

    conn, cursor = db_connect(database_path)
    try:
        if not _handle_ai_debug_command(command_text, user_id, cursor, respond):
            respond("Uso: `/debug [on|off|status]`.")
    finally:
        conn.close()


def handle_query(event, cursor, say):
    text = event.get("text", "").strip()
    user_id = event.get("user", "unknown")
    
    logger.info(f"[CLOWN] Received DM from user {user_id}, text: '{text}'")

    # Il testo senza slash funziona sempre in DM. /debug resta supportato quando
    # Slack lo consegna come messaggio invece che come Slash Command nativo.
    if _handle_ai_debug_command(text, user_id, cursor, say):
        return
    
    # Gestisci comando /clown
    if text.startswith("/clown "):
        nickname = text[7:].strip()  # Rimuovi "/clown " e spazi
        logger.info(f"[CLOWN] Processing /clown command with nickname: '{nickname}'")
        if nickname:
            nickname_lower = nickname.lower()
            expiry_date = datetime.now() + timedelta(hours=24)
            add_clown_user(
                cursor.connection, cursor, nickname_lower, expiry_date,
                source="manual", assigned_by=user_id,
            )
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

    # Gestisci comando /clowns — lista clown attivi con motivi/origine
    if text.strip() in ("/clowns", "/clownlist"):
        logger.info(f"[CLOWN] Processing /clowns command from user {user_id}")
        clean_expired_clown_users(cursor.connection, cursor)
        cursor.execute(
            "SELECT nickname, expiry_date, source, assigned_by, reason, thread_ts, channel "
            "FROM clown_users ORDER BY expiry_date"
        )
        rows = cursor.fetchall()
        if not rows:
            say("🤡 Nessun clown attivo. La community è momentaneamente lucida.")
            return

        # Risolvi assigned_by user_id → display name
        assignor_ids = sorted({r[3] for r in rows if r[3] and r[3] != "auto"})
        assignor_names = get_user_name_map(cursor, assignor_ids) if assignor_ids else {}

        lines = [f"🤡 *Clown attivi ({len(rows)}):*\n"]
        now = datetime.now()
        for nick, expiry, source, by, reason, t_ts, ch in rows:
            # Parsing expiry (ISO format)
            try:
                exp_dt = datetime.fromisoformat(expiry)
                delta = exp_dt - now
                if delta.total_seconds() < 0:
                    when = "scaduto (in pulizia)"
                else:
                    hours = int(delta.total_seconds() // 3600)
                    minutes = int((delta.total_seconds() % 3600) // 60)
                    when = f"scade tra {hours}h{minutes:02d}m"
            except Exception:
                when = f"scade il {expiry}"

            line = f"• *{nick}* — {when}"
            if source == "auto":
                line += "\n   _auto_"
                if ch:
                    line += f" in <#{ch}>"
                if t_ts:
                    line += f", thread `{t_ts}`"
                if reason:
                    line += f"\n   motivo: _{reason}_"
            elif source == "manual" and by:
                assignor = assignor_names.get(by, by)
                line += f"\n   _manuale_ da <@{by}> ({assignor})"
            else:
                line += "\n   _origine non tracciata (legacy)_"
            lines.append(line)

        say("\n".join(lines))
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


def normalize_url(url):
    """Normalizza un URL applicando le regole ClearURLs (provider-aware)."""
    try:
        return _url_cleaner.clean(url)
    except Exception as e:
        logger.warning(f"Error normalizing URL {url}: {e}")
        return url


def save_xcancel_alert(parent_ts, alert_ts, channel, alert_text):
    """Salva il riferimento all'alert xcancel per cancellarlo quando serve."""
    if not parent_ts or not alert_ts or not channel:
        return

    conn, cursor = db_connect(database_path)
    try:
        cursor.execute(
            """
            INSERT OR REPLACE INTO xcancel_alerts
            (parent_message_ts, alert_message_ts, channel, alert_text)
            VALUES (?, ?, ?, ?)
            """,
            (parent_ts, alert_ts, channel, alert_text),
        )
        conn.commit()
        logger.debug(f"Saved xcancel alert reference: parent_ts={parent_ts}, alert_ts={alert_ts}")
    except Exception as e:
        logger.error(f"Error saving xcancel alert reference: {e}")
        conn.rollback()
    finally:
        conn.close()


def delete_xcancel_alert(parent_ts, channel=None):
    """Cancella da Slack e dal DB l'alert xcancel associato a un messaggio."""
    if not parent_ts:
        return False

    conn, cursor = db_connect(database_path)
    deleted_any = False
    try:
        if channel:
            cursor.execute(
                """
                SELECT alert_message_ts, channel
                FROM xcancel_alerts
                WHERE parent_message_ts = ? AND channel = ?
                """,
                (parent_ts, channel),
            )
        else:
            cursor.execute(
                """
                SELECT alert_message_ts, channel
                FROM xcancel_alerts
                WHERE parent_message_ts = ?
                """,
                (parent_ts,),
            )

        alerts = cursor.fetchall()

        for alert_ts, alert_channel in alerts:
            if not alert_ts:
                # Riserva senza messaggio ancora postato: nulla da cancellare su Slack.
                continue
            try:
                app.client.chat_delete(channel=alert_channel, ts=alert_ts)
                deleted_any = True
                logger.info(
                    f"XCANCEL_ALERT_DELETED: Deleted orphaned xcancel alert: "
                    f"alert_ts='{alert_ts}' channel='{alert_channel}' parent_ts='{parent_ts}'"
                )
            except Exception as e:
                logger.warning(f"Could not delete xcancel alert {alert_ts}: {e}")

        if alerts:
            if channel:
                cursor.execute(
                    "DELETE FROM xcancel_alerts WHERE parent_message_ts = ? AND channel = ?",
                    (parent_ts, channel),
                )
            else:
                cursor.execute("DELETE FROM xcancel_alerts WHERE parent_message_ts = ?", (parent_ts,))
            conn.commit()

    except Exception as e:
        logger.error(f"Error deleting xcancel alert for parent_ts={parent_ts}: {e}")
        conn.rollback()
    finally:
        conn.close()

    return deleted_any


def get_xcancel_alert_text(parent_ts, channel):
    """Restituisce il testo dell'alert xcancel tracciato, se presente."""
    if not parent_ts or not channel:
        return None

    conn, cursor = db_connect(database_path)
    try:
        cursor.execute(
            """
            SELECT alert_text
            FROM xcancel_alerts
            WHERE parent_message_ts = ? AND channel = ?
            """,
            (parent_ts, channel),
        )
        row = cursor.fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def claim_xcancel_alert_slot(parent_ts, channel, alert_text):
    """Riserva lo slot dell'alert xcancel.

    Returns:
        True se la riserva è acquisita, False se un altro handler l'ha già
        presa, None se la riserva è fallita per un errore DB.
    """
    if not parent_ts or not channel:
        return False

    conn, cursor = db_connect(database_path)
    try:
        claimed = claim_xcancel_alert(cursor, parent_ts, channel, alert_text)
        conn.commit()
        return claimed
    except Exception as e:
        logger.error(f"Error claiming xcancel alert slot: {e}")
        conn.rollback()
        return None
    finally:
        conn.close()


def finalize_xcancel_alert_claim(parent_ts, alert_ts, channel, alert_text):
    """Completa la riserva con il ts dell'alert postato.

    Returns:
        True se la riserva era ancora presente, False se è stata rimossa o
        sostituita nel frattempo (parent cancellato o testo modificato).
    """
    conn, cursor = db_connect(database_path)
    try:
        finalized = finalize_xcancel_alert(cursor, parent_ts, alert_ts, channel, alert_text)
        conn.commit()
        return finalized
    except Exception as e:
        logger.error(f"Error finalizing xcancel alert claim: {e}")
        conn.rollback()
        # In dubbio meglio tenere l'alert postato che cancellarlo per un
        # errore di bookkeeping.
        return True
    finally:
        conn.close()


def release_xcancel_alert_claim(parent_ts, channel, alert_text):
    """Rilascia la riserva dell'alert xcancel se il post su Slack non è riuscito.

    Rimuove solo la riserva con lo stesso testo, per non toccare una riserva
    più recente acquisita nel frattempo da un altro handler.
    """
    conn, cursor = db_connect(database_path)
    try:
        cursor.execute(
            """
            DELETE FROM xcancel_alerts
            WHERE parent_message_ts = ? AND channel = ?
              AND alert_message_ts = '' AND alert_text = ?
            """,
            (parent_ts, channel, alert_text),
        )
        conn.commit()
    except Exception as e:
        logger.error(f"Error releasing xcancel alert claim: {e}")
        conn.rollback()
    finally:
        conn.close()


def post_xcancel_alternatives(message, say):
    """Se il messaggio contiene link a x.com, posta le alternative xcancel.com nel thread."""
    response_text = build_xcancel_response_text(message.get("text", ""))
    if not response_text:
        return

    parent_ts = message.get("ts")
    channel = message.get("channel")
    thread_ts = message.get("thread_ts", parent_ts)

    # L'evento message e il message_changed generato dall'unfurl del link (o un
    # retry di Slack) possono processare lo stesso messaggio in parallelo: solo
    # chi riserva lo slot posta l'alert, gli altri escono senza duplicare.
    claimed = claim_xcancel_alert_slot(parent_ts, channel, response_text)
    if claimed is False:
        logger.debug(
            f"XCANCEL_ALERT_ALREADY_CLAIMED: parent_ts={parent_ts} channel={channel}"
        )
        return
    if claimed is None:
        # Riserva fallita per errore DB: meglio rischiare un raro duplicato
        # che sopprimere l'alert in silenzio.
        logger.warning(
            f"XCANCEL_CLAIM_FAILED: posting without claim, parent_ts={parent_ts} channel={channel}"
        )

    try:
        result = say(text=response_text, thread_ts=thread_ts)
        alert_ts = result.get("ts") if result else None
        if not alert_ts:
            if claimed:
                release_xcancel_alert_claim(parent_ts, channel, response_text)
            return

        if claimed:
            if not finalize_xcancel_alert_claim(parent_ts, alert_ts, channel, response_text):
                # Il parent è stato cancellato (o il testo modificato) mentre
                # postavamo: l'alert appena creato non serve più.
                try:
                    app.client.chat_delete(channel=channel, ts=alert_ts)
                    logger.info(
                        f"XCANCEL_ALERT_SUPERSEDED: removed stale alert {alert_ts} in {channel}"
                    )
                except Exception as e:
                    logger.warning(f"Could not delete stale xcancel alert {alert_ts}: {e}")
                return
        else:
            save_xcancel_alert(parent_ts, alert_ts, channel, response_text)
        logger.info("Posted xcancel alternatives")
    except Exception as e:
        if claimed:
            release_xcancel_alert_claim(parent_ts, channel, response_text)
        logger.error(f"Error posting xcancel alternative: {e}")


def sync_xcancel_alternatives_for_message(message, say):
    """Allinea l'alert xcancel quando un messaggio viene modificato."""
    parent_ts = message.get("ts")
    channel = message.get("channel")
    expected_text = build_xcancel_response_text(message.get("text", ""))
    existing_text = get_xcancel_alert_text(parent_ts, channel)

    if not expected_text:
        delete_xcancel_alert(parent_ts, channel)
        return

    if existing_text == expected_text:
        logger.debug(f"XCANCEL_ALERT_UNCHANGED: parent_ts={parent_ts} channel={channel}")
        return

    delete_xcancel_alert(parent_ts, channel)
    post_xcancel_alternatives(message, say)


def check_and_store_links(message, permalink_dict, say, *, links=None):
    """Record every external link and post at most one deterministic alert."""
    links = links or extract_external_links(message.get("text", ""), normalize_url)
    if not links:
        return

    channel = message.get("channel")
    message_ts = message.get("ts")
    if not channel or not message_ts:
        logger.warning("Link-bearing message missing channel or timestamp")
        return

    current_permalink = permalink_dict.get("permalink", "")
    if not current_permalink:
        try:
            current_permalink = app.client.chat_getPermalink(
                channel=channel,
                message_ts=message_ts,
            )["permalink"]
        except Exception as e:
            logger.warning(f"Could not get permalink for message: {e}")

    user_display_name = "utente"
    try:
        profile = app.client.users_info(user=message.get("user", ""))["user"]["profile"]
        user_display_name = profile.get("display_name") or profile.get("real_name") or "utente"
    except Exception:
        logger.debug("Could not resolve display name for duplicate alert", exc_info=True)

    conn, _ = db_connect(database_path)
    claim = None
    try:
        claim = prepare_exact_duplicate_alert(
            conn,
            channel=channel,
            message_timestamp=message_ts,
            thread_ts=message.get("thread_ts") or message_ts,
            permalink=current_permalink,
            posted_at=float(message_ts),
            links=links,
            user_display_name=user_display_name,
        )
        if claim is None:
            return

        try:
            posted = deliver_duplicate_alert(
                conn,
                claim,
                post=lambda text, thread_ts: say(text=text, thread_ts=thread_ts),
                delete=lambda alert_channel, alert_ts: app.client.chat_delete(
                    channel=alert_channel,
                    ts=alert_ts,
                ),
            )
            if not posted:
                return
            logger.info(
                "EXACT_LINK_DUPLICATE_ALERT: current=%s source=%s",
                message_ts,
                claim.source_message_ts,
            )
        except Exception:
            raise
    except Exception as e:
        logger.error(f"Error in check_and_store_links: {e}")
        logger.error(traceback.format_exc())
    finally:
        conn.close()


def _cleanup_stored_duplicate_alerts(alerts):
    if not alerts:
        return
    conn, _ = db_connect(database_path)
    try:
        for alert in alerts:
            deleted = False
            if alert.alert_message_ts:
                try:
                    app.client.chat_delete(
                        channel=alert.current_channel,
                        ts=alert.alert_message_ts,
                    )
                    deleted = True
                except Exception as e:
                    logger.warning(
                        "Could not delete obsolete duplicate alert %s: %s",
                        alert.alert_message_ts,
                        e,
                    )
            finalize_stored_alert_cleanup(conn, alert, deleted=deleted)
    finally:
        conn.close()


def process_ready_link_document(_normalized_url):
    """Evaluate all ready unchecked public links after a document completes."""
    try:
        threshold = float(os.getenv("LINK_TOPIC_SIMILARITY_THRESHOLD", "0.92"))
    except ValueError:
        logger.warning("Invalid LINK_TOPIC_SIMILARITY_THRESHOLD; using 0.92")
        threshold = 0.92
    if not 0.0 <= threshold <= 1.0:
        logger.warning("Out-of-range LINK_TOPIC_SIMILARITY_THRESHOLD; using 0.92")
        threshold = 0.92

    conn, _ = db_connect(database_path)
    try:
        claims = prepare_enriched_duplicate_alerts(
            conn,
            similarity_threshold=threshold,
        )
        for claim in claims:
            try:
                deliver_duplicate_alert(
                    conn,
                    claim,
                    post=lambda text, thread_ts, alert_claim=claim: app.client.chat_postMessage(
                        channel=alert_claim.current_channel,
                        text=text,
                        thread_ts=thread_ts,
                    ),
                    delete=lambda alert_channel, alert_ts: app.client.chat_delete(
                        channel=alert_channel,
                        ts=alert_ts,
                    ),
                )
            except Exception:
                logger.error(
                    "Failed to deliver enriched duplicate alert for %s",
                    claim.current_message_ts,
                    exc_info=True,
                )
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
    _initialize_bot_identity()
    logger.debug(message)
    user_id = message.get("user", "unknown")
    channel_type = message.get("channel_type", "unknown")
    text_preview = message.get("text", "")[:50] if message.get("text") else "(no text)"
    
    logger.info(f"[CLOWN] handle_message called - user: {user_id}, channel_type: {channel_type}, text_preview: '{text_preview}...'")
    
    message_user = message.get("user")
    if (
        "text" not in message
        or not message_user
        or message_user == "USLACKBOT"
        or message_user == app._bot_user_id
    ):
        logger.debug("[CLOWN] Skipping message: no user/text or from a bot")
        return

    # Route links before engage/mention/stop early returns. app_mention and
    # message events can overlap; message-link and alert claims are idempotent.
    route_link_message_event(
        message,
        normalize_url,
        lambda links: check_and_store_links(
            message,
            {"permalink": ""},
            say,
            links=links,
        ),
    )

    # Controlla se il bot è menzionato nel messaggio
    bot_user_id = app._bot_user_id
    text = message.get("text", "")
    # Intercetta anche stop testuali copiati male dal suggerimento Slack
    # (es. _`@slack-archive-bot stop`_), che non generano una mention nativa.
    try:
        if _maybe_handle_engaged_stop(message, say):
            return
    except Exception as e:
        logger.error(f"[ENGAGE] Errore intercept stop: {e}")
        logger.error(traceback.format_exc())

    if bot_user_id and f"<@{bot_user_id}>" in text:
        logger.info(f"[AI] Bot mentioned in message (via handle_message) by user {user_id}")
        # @bot /engage ingaggia il thread; una mention normale resta one-shot.
        try:
            if _maybe_handle_engage_command(message, say):
                return
            handle_app_mention(message, say)
            return
        except Exception as e:
            _report_ai_error(
                e,
                event=message,
                source="message_mention_router",
                say=say,
                thread_ts=message.get("thread_ts") or message.get("ts"),
            )
            return

    conn, cursor = db_connect(database_path)

    # If it's a DM, treat it as a search query
    if message.get("channel_type") == "im":
        logger.info(f"[CLOWN] Message is a DM, routing to handle_query")
        try:
            handle_query(message, cursor, say)
        finally:
            conn.close()
    elif "user" not in message:
        logger.warning("No valid user. Previous event not saved")
    else:  # Otherwise save the message to the archive.
        # Duplicate alerts need a citation for roots and replies alike.
        try:
            permalink = app.client.chat_getPermalink(
                channel=message["channel"], message_ts=message["ts"]
            )
        except Exception as e:
            logger.warning(f"Could not get permalink while archiving message: {e}")
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

        # Engage is a core reply path: run it immediately after persistence so
        # unrelated link/reaction/user enrichment failures cannot silence it.
        maybe_reply_to_engaged_thread(message, say)

        # Keep original message data for link-adjacent behaviors.
        original_message = message.copy()
        original_message["text"] = original_text
        original_message["user"] = original_user
        
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


def _message_replied_identity(event):
    """Extract the newest reply identity from Slack's message_replied wrapper."""
    root_message = event.get("message")
    if not isinstance(root_message, dict):
        return "", "", ""
    channel = str(event.get("channel") or "")
    thread_ts = str(root_message.get("thread_ts") or root_message.get("ts") or "")
    candidates = [str(root_message.get("latest_reply") or "")]
    candidates.extend(
        str(reply.get("ts") or "")
        for reply in (root_message.get("replies") or [])
        if isinstance(reply, dict)
    )
    reply_timestamps = [value for value in candidates if value and value != thread_ts]
    if not channel or not thread_ts or not reply_timestamps:
        return "", "", ""
    try:
        reply_ts = max(reply_timestamps, key=float)
    except (TypeError, ValueError):
        reply_ts = max(reply_timestamps)
    return channel, thread_ts, reply_ts


def _active_engagement_exists(channel, thread_ts):
    conn, cursor = db_connect(database_path)
    try:
        row = cursor.execute(
            "SELECT engaged, stopped FROM engaged_threads "
            "WHERE channel = ? AND thread_ts = ?",
            (channel, thread_ts),
        ).fetchone()
        return bool(row and row[0] and not row[1])
    finally:
        conn.close()


def _fetch_raw_thread_reply(channel, thread_ts, reply_ts):
    """Fetch a reply that Slack represented only as message_replied metadata."""
    cursor = None
    while True:
        response = app.client.conversations_replies(
            channel=channel,
            ts=thread_ts,
            cursor=cursor,
            limit=200,
        )
        for reply in response.get("messages", []):
            if str(reply.get("ts") or "") == reply_ts:
                return reply
        cursor = (response.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            return None


@app.event({"type": "message", "subtype": "message_replied"})
def handle_message_replied(event, say):
    """Route message_replied wrappers through the normal engaged-thread path."""
    channel, thread_ts, reply_ts = _message_replied_identity(event)
    if not channel or not thread_ts or not reply_ts:
        return
    try:
        if not _active_engagement_exists(channel, thread_ts):
            return
        reply = _fetch_raw_thread_reply(channel, thread_ts, reply_ts)
        if not reply:
            raise RuntimeError("reply Slack non disponibile per message_replied")
        if (
            reply.get("bot_id")
            or reply.get("bot_profile")
            or reply.get("user") == app._bot_user_id
        ):
            return
        if not reply.get("user") or not reply.get("text"):
            raise RuntimeError("reply Slack incompleta per message_replied")
        handle_message(
            {
                **reply,
                "channel": channel,
                "thread_ts": thread_ts,
                "channel_type": str(event.get("channel_type") or "channel"),
            },
            say,
        )
    except Exception as error:
        _report_ai_error(
            error,
            event={
                "user": "",
                "channel": channel,
                "ts": reply_ts,
                "thread_ts": thread_ts,
            },
            source="message_replied_router",
            say=say,
            thread_ts=thread_ts,
        )


@app.message("")
def handle_message_default(message, say):
    handle_message(message, say)


def handle_bolt_error(error, body):
    """Deliver otherwise-unhandled Slack event failures to opted-in debug DMs."""
    event = body.get("event", body) if isinstance(body, dict) else {}
    _report_ai_error(error, event=event, source="bolt_event")


if hasattr(app, "error"):
    app.error(handle_bolt_error)


def get_archived_thread_messages(channel, thread_ts):
    """Read a thread from the local archive when Slack history is unavailable."""
    conn, cursor = db_connect(database_path)
    try:
        cursor.execute(
            """
            SELECT message, user, timestamp
            FROM messages
            WHERE channel = ? AND thread_ts = ?
            ORDER BY CAST(timestamp AS REAL), timestamp
            """,
            (channel, thread_ts),
        )
        messages = [
            {"text": text, "user": user_id, "ts": ts}
            for text, user_id, ts in cursor.fetchall()
        ]
    finally:
        conn.close()
    return build_ai_context_messages(messages)


def get_thread_messages(
    channel,
    thread_ts,
    *,
    fallback_to_archive=False,
    raise_errors=False,
):
    """Recupera tutti i messaggi di un thread, con fallback locale opzionale."""
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

        formatted = build_ai_context_messages(messages)
        if formatted or not fallback_to_archive:
            return formatted
        return get_archived_thread_messages(channel, thread_ts)

    except Exception as e:
        logger.error(f"Error getting thread messages: {e}")
        logger.error(traceback.format_exc())
        if fallback_to_archive:
            archived = get_archived_thread_messages(channel, thread_ts)
            if archived:
                logger.warning(
                    "Using archived fallback for thread %s in channel %s",
                    thread_ts,
                    channel,
                )
                return archived
        if raise_errors:
            raise
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
        db_cursor.execute("SELECT user FROM optout UNION SELECT user FROM optout_ai")
        ai_opted_out_users = {row[0] for row in db_cursor.fetchall()}

        formatted_messages = []
        for msg in messages:
            user_id = msg.get("user", "")
            text = (msg.get("text") or "").strip()

            if (
                not user_id
                or user_id == "USLACKBOT"
                or user_id in ai_opted_out_users
                or not text
            ):
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




def format_ai_rate_limit_footer(throttle_info):
    """Render the user-visible usage counters returned by check_ai_throttle."""
    return (
        "📊 Rate limit per user: "
        f"{throttle_info['requests_last_minute']}/{throttle_info['limit_per_minute']} "
        "al minuto, "
        f"{throttle_info['requests_last_hour']}/{throttle_info['limit_per_hour']} "
        "all'ora"
    )


def _next_ai_request_time(cursor, user_id, cutoff, window_seconds):
    cursor.execute(
        "SELECT MIN(timestamp) FROM ai_requests WHERE timestamp > ? AND user_id = ?",
        (cutoff, user_id),
    )
    row = cursor.fetchone()
    oldest = row[0] if row else None
    if oldest is None:
        return datetime.now()
    return datetime.fromtimestamp(float(oldest) + window_seconds)


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

    # Serializza count+insert tra worker Gunicorn per non superare il limite in gara.
    cursor.execute("BEGIN IMMEDIATE")
    
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
        next_available = _next_ai_request_time(
            cursor, user_id, one_minute_ago_timestamp, 60
        ).strftime("%H:%M:%S")
        message = (
            f"⏱️ Limite raggiunto: hai già fatto {requests_last_minute} richieste "
            f"nell'ultimo minuto. Riprova alle {next_available}.\n\n"
            + format_ai_rate_limit_footer(throttle_info)
        )
        logger.warning(f"[AI] Throttle exceeded: {requests_last_minute} requests in last minute (limit: 2)")
        conn.commit()
        return False, message, throttle_info
    
    if requests_last_hour >= 10:
        next_available = _next_ai_request_time(
            cursor, user_id, one_hour_ago_timestamp, 60 * 60
        ).strftime("%H:%M:%S")
        message = (
            f"⏱️ Limite raggiunto: hai già fatto {requests_last_hour} richieste "
            f"nell'ultima ora. Riprova alle {next_available}.\n\n"
            + format_ai_rate_limit_footer(throttle_info)
        )
        logger.warning(f"[AI] Throttle exceeded: {requests_last_hour} requests in last hour (limit: 10)")
        conn.commit()
        return False, message, throttle_info
    
    # Registra la richiesta con timestamp Unix
    cursor.execute(
        "INSERT INTO ai_requests (timestamp, user_id, channel) VALUES (?, ?, ?)",
        (now_timestamp, user_id, channel)
    )
    conn.commit()

    # I contatori ritornati e mostrati includono la richiesta appena accettata.
    throttle_info["requests_last_minute"] += 1
    throttle_info["requests_last_hour"] += 1
    
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
        try:
            allowed, throttle_message, throttle_info = check_ai_throttle(
                conn, cursor, user_id, channel
            )
        finally:
            conn.close()
        
        logger.info(f"[AI] Throttle status: {throttle_info}")
        
        if not allowed:
            say(throttle_message, thread_ts=response_thread_ts)
            return
        
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

        logger.info(f"[AI] Found {len(context_messages)} messages for {context_scope} context")

        formatted_messages = format_messages_for_prompt(context_messages) or (
            "(Nessun messaggio corrente disponibile; usa l'archivio se pertinente.)"
        )

        openai_api_key = os.environ.get("OPENAI_API_KEY")
        if not openai_api_key:
            logger.error("[AI] OPENAI_API_KEY not set")
            raise RuntimeError("OPENAI_API_KEY non configurata")
        
        client = OpenAI(api_key=openai_api_key)
        
        logger.info(
            f"[AI] Starting bounded archive agent with model {AI_RESPONSE_MODEL} "
            f"and {len(context_messages)} current-context messages"
        )
        conn_ctx, _ = db_connect(database_path)
        try:
            evidence = EvidenceRegistry()
            search_engine = ArchiveSearchEngine(
                conn_ctx,
                requester_user_id=user_id,
                current_channel_id=channel,
                before_timestamp=message_ts,
                evidence=evidence,
            )
            final_response = run_archive_agent(
                client,
                question=text,
                current_context=(
                    f"Fonte contesto corrente: {context_label}\n\n{formatted_messages}"
                    + MENTION_HINT_PROMPT
                ),
                search_engine=search_engine,
                model=AI_RESPONSE_MODEL,
                reasoning_effort=AI_REASONING_EFFORT,
            )
        finally:
            conn_ctx.close()

        logger.info(f"[AI] Received grounded response, length: {len(final_response)}")

        final_response = (
            final_response.rstrip()
            + "\n\n"
            + format_ai_rate_limit_footer(throttle_info)
        )
        
        # Rispondi nel thread
        say(final_response, thread_ts=response_thread_ts)
        
    except Exception as e:
        _report_ai_error(
            e,
            event=event,
            source="app_mention",
            say=say,
            thread_ts=event.get("thread_ts") or event.get("ts"),
        )


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


def _looks_like_legacy_cooldown_deferred(cursor, evaluated_at):
    """Compat per righe create prima del flag cooldown_deferred."""
    if evaluated_at is None:
        return False
    try:
        evaluated_at = float(evaluated_at)
    except (TypeError, ValueError):
        return False
    cursor.execute(
        """
        SELECT 1 FROM trash_engaged_threads
        WHERE engaged = 1
          AND evaluated_at <= ?
          AND evaluated_at > ?
        LIMIT 1
        """,
        (evaluated_at, evaluated_at - AUTO_ENGAGE_COOLDOWN_SECONDS),
    )
    return cursor.fetchone() is not None


def _save_trash_engage_decision(
    cursor,
    thread_ts,
    channel,
    decided,
    engaged,
    evaluated_at,
    last_reply_ts,
    cooldown_deferred=0,
):
    """Upsert dello stato auto-engage senza perdere stop/clown gia salvati."""
    cursor.execute(
        """
        UPDATE trash_engaged_threads
        SET decided = ?, engaged = ?, evaluated_at = ?, last_reply_ts = ?, cooldown_deferred = ?
        WHERE thread_ts = ? AND channel = ?
        """,
        (
            1 if decided else 0,
            1 if engaged else 0,
            evaluated_at,
            last_reply_ts,
            1 if cooldown_deferred else 0,
            thread_ts,
            channel,
        ),
    )
    if cursor.rowcount == 0:
        cursor.execute(
            """
            INSERT INTO trash_engaged_threads
            (thread_ts, channel, decided, engaged, evaluated_at, last_reply_ts, cooldown_deferred)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                thread_ts,
                channel,
                1 if decided else 0,
                1 if engaged else 0,
                evaluated_at,
                last_reply_ts,
                1 if cooldown_deferred else 0,
            ),
        )


def _decide_engage(thread_messages, openai_client):
    """LLM-call: decide se il bot deve inserirsi nel thread #trash. Ritorna (engage: bool, reply: str)."""
    thread_text = _format_thread_for_llm(thread_messages)
    system = (
        SFERAIT_SYSTEM_PROMPT
        + MENTION_HINT_PROMPT
        + "\n\n## Modalità AUTO-ENGAGE\n"
        "Stai osservando un thread in un canale informale della community a cui nessuno ti ha chiesto di partecipare. "
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
    raw = generate_text_response(
        openai_client,
        model=AUTO_ENGAGE_DECISION_MODEL,
        instructions=system,
        input_text=user_msg,
        max_output_tokens=600,
        reasoning_effort="low",
        text_format={"type": "json_object"},
    )
    data = json.loads(raw or "{}")
    return bool(data.get("engage")), _strip_bot_self_prefix((data.get("reply") or "").strip())


def _decide_clown(thread_messages, openai_client):
    """LLM-call: decide se qualcuno nel thread merita il clown. Ritorna (user_name: str|None, reason: str|None)."""
    thread_text = _format_thread_for_llm(thread_messages)
    system = (
        "Sei il giudice clown di SferaIT. Stai osservando un thread in un canale informale della community. "
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
    raw = generate_text_response(
        openai_client,
        model=AUTO_ENGAGE_DECISION_MODEL,
        instructions=system,
        input_text=user_msg,
        max_output_tokens=300,
        reasoning_effort="low",
        text_format={"type": "json_object"},
    )
    data = json.loads(raw or "{}")
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


def _engaged_stop_text_fallback():
    return STOP_HINT_SUFFIX_TEMPLATE.format(bot_id=app._bot_user_id)


def _engaged_stop_ack_text(user_id=None):
    actor = f" grazie a <@{user_id}>" if user_id else ""
    return f"Ok, mi zittisco su questo thread{actor}. :zipper_mouth_face:"


def _engaged_stop_button_blocks(reply, channel, thread_ts):
    """Crea i blocchi Slack per le risposte dei thread ingaggiati con bottone stop."""
    payload = json.dumps({"channel": channel, "thread_ts": thread_ts})
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": reply[:3000],
            },
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": "Zitto",
                        "emoji": True,
                    },
                    "style": "danger",
                    "action_id": ENGAGED_THREAD_STOP_ACTION_ID,
                    "value": payload,
                }
            ],
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": _engaged_stop_text_fallback().strip(),
                }
            ],
        },
    ]


def _say_engaged_thread_reply(say, reply, channel, thread_ts):
    """Posta una risposta in un thread ingaggiato con fallback testuale e bottone stop."""
    say(
        text=reply + _engaged_stop_text_fallback(),
        blocks=_engaged_stop_button_blocks(reply, channel, thread_ts),
        thread_ts=thread_ts,
    )


def _trash_stop_text_fallback():
    return _engaged_stop_text_fallback()


def _trash_stop_button_blocks(reply, channel, thread_ts):
    return _engaged_stop_button_blocks(reply, channel, thread_ts)


def _say_trash_auto_reply(say, reply, channel, thread_ts):
    _say_engaged_thread_reply(say, reply, channel, thread_ts)


def _normalize_trash_stop_text(text):
    """Rende tollerante il comando stop copiato da Slack con markup residuo."""
    if not text:
        return ""

    normalized = text.strip()
    normalized = normalized.replace("\u200b", "")
    normalized = re.sub(r"[`*_~]+", "", normalized)
    normalized = re.sub(r"&lt;", "<", normalized)
    normalized = re.sub(r"&gt;", ">", normalized)
    normalized = re.sub(r"&amp;", "&", normalized)
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized.strip()


def _bot_text_aliases():
    aliases = ["slack-archive-bot"]
    bot_name = (app._bot_display_name or "").strip()
    if bot_name and bot_name.lower() not in {a.lower() for a in aliases}:
        aliases.append(bot_name)
    return aliases


def _is_trash_stop_text(text, bot_user_id):
    """True se il testo chiede al bot di fermarsi nel thread.

    Accetta sia mention Slack native (`<@U...> stop`) sia copie testuali tipo
    `@slack-archive-bot stop`, anche se avvolte in corsivo/backtick.
    """
    normalized = _normalize_trash_stop_text(text)
    if not normalized:
        return False

    mention_re = rf"<@{re.escape(bot_user_id)}(?:\|[^>]+)?>"
    without_mention = re.sub(mention_re, "", normalized).strip()
    if without_mention != normalized and _STOP_KEYWORD_RE.match(without_mention):
        return True

    for alias in _bot_text_aliases():
        alias_re = re.escape(alias)
        if re.match(rf"^\s*@?{alias_re}\b[:,]?\s+", normalized, re.IGNORECASE):
            without_alias = re.sub(
                rf"^\s*@?{alias_re}\b[:,]?\s+",
                "",
                normalized,
                count=1,
                flags=re.IGNORECASE,
            ).strip()
            if _STOP_KEYWORD_RE.match(without_alias):
                return True

    return False


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


def _auto_reply_in_thread(
    channel,
    thread_ts,
    thread_messages,
    openai_client,
    say,
    *,
    response_suffix="",
):
    """Reply in an engaged thread through the same grounded archive agent."""
    bot_user_id = app._bot_user_id
    latest_user_message = next(
        (
            message
            for message in reversed(thread_messages)
            if message.get("user_id") and message.get("user_id") != bot_user_id
        ),
        None,
    )
    if latest_user_message is None:
        return

    formatted_messages = format_messages_for_prompt(thread_messages)
    evidence = EvidenceRegistry()
    conn, _ = db_connect(database_path)
    try:
        search_engine = ArchiveSearchEngine(
            conn,
            requester_user_id=latest_user_message.get("user_id", ""),
            current_channel_id=channel,
            before_timestamp=latest_user_message.get("ts"),
            evidence=evidence,
        )
        reply = run_archive_agent(
            openai_client,
            question=latest_user_message.get("text", ""),
            current_context=(
                "Fonte contesto corrente: thread Slack ingaggiato\n\n"
                + formatted_messages
                + MENTION_HINT_PROMPT
            ),
            search_engine=search_engine,
            model=AI_RESPONSE_MODEL,
            reasoning_effort=AI_REASONING_EFFORT,
        )
    finally:
        conn.close()

    reply = _strip_bot_self_prefix(reply)
    if reply:
        if response_suffix:
            reply = reply.rstrip() + "\n\n" + response_suffix.strip()
        _say_engaged_thread_reply(say, reply, channel, thread_ts)


_STOP_KEYWORD_RE = re.compile(
    r"^\s*(stop|basta|smettila|silenzio|zitto|shut\s*up)\s*[.!?]*\s*$",
    re.IGNORECASE,
)


def _thread_ts_for_engagement(message):
    """Restituisce il thread target per /engage: thread esistente o root message."""
    return message.get("thread_ts") or message.get("ts")


def _maybe_handle_engage_command(message, say):
    """Gestisce @bot /engage e attiva/riattiva il bot sul thread corrente."""
    bot_user_id = app._bot_user_id
    text = message.get("text", "") or ""
    channel = message.get("channel")
    message_ts = message.get("ts")
    thread_ts = _thread_ts_for_engagement(message)
    user_id = message.get("user", "")

    if not channel or not message_ts or not thread_ts:
        return False

    if not is_engage_request(text, bot_user_id):
        return False

    conn, cursor = db_connect(database_path)
    try:
        cursor.execute(
            """
            SELECT engaged, stopped, last_reply_ts
            FROM engaged_threads
            WHERE thread_ts = ? AND channel = ?
            """,
            (thread_ts, channel),
        )
        row = cursor.fetchone()
        if row and row[2] == message_ts:
            logger.info(f"[ENGAGE] Duplicate /engage event ignored for thread {thread_ts}")
            return True

        was_stopped = bool(row[1]) if row else False
        now_ts = datetime.now().timestamp()
        cursor.execute(
            """
            INSERT INTO engaged_threads
            (thread_ts, channel, engaged, stopped, engaged_at, engaged_by, last_reply_ts)
            VALUES (?, ?, 1, 0, ?, ?, ?)
            ON CONFLICT(thread_ts, channel) DO UPDATE SET
                engaged = 1,
                stopped = 0,
                engaged_at = excluded.engaged_at,
                engaged_by = excluded.engaged_by,
                last_reply_ts = excluded.last_reply_ts
            """,
            (thread_ts, channel, now_ts, user_id, message_ts),
        )
        conn.commit()

        logger.info(
            f"[ENGAGE] Thread {thread_ts} engaged in channel {channel} by user {user_id}"
        )
        if was_stopped:
            say("Riattivato. Da ora rispondo a ogni nuovo messaggio in questo thread.", thread_ts=thread_ts)
        else:
            say(
                "Ingaggiato su questo thread: risponderò ai nuovi messaggi scritti "
                "qui dentro, non ai nuovi messaggi fuori dal thread. "
                "Per fermarmi: `<@{}> stop`".format(bot_user_id),
                thread_ts=thread_ts,
            )
        return True
    finally:
        conn.close()


def _maybe_handle_engaged_stop(message, say):
    """Se il messaggio chiede stop in un thread engaged, marca il thread come stopped.
    Altrimenti ritorna False."""
    thread_ts = message.get("thread_ts")
    ts = message.get("ts")
    channel = message.get("channel")
    text = message.get("text", "") or ""
    bot_user_id = app._bot_user_id
    user_id = message.get("user")

    # Solo reply in thread: lo stop agisce sul thread già ingaggiato.
    if not thread_ts or thread_ts == ts:
        return False

    if not _is_trash_stop_text(text, bot_user_id):
        return False

    conn, cursor = db_connect(database_path)
    try:
        cursor.execute(
            "SELECT engaged FROM engaged_threads WHERE thread_ts = ? AND channel = ?",
            (thread_ts, channel),
        )
        row = cursor.fetchone()
        if not row or not row[0]:
            # Non c'e un engage attivo da fermare
            return False

        cursor.execute(
            "UPDATE engaged_threads SET stopped = 1 "
            "WHERE thread_ts = ? AND channel = ?",
            (thread_ts, channel),
        )
        conn.commit()
        logger.info(f"[ENGAGE] Thread {thread_ts} stopped by user request")
        try:
            app.client.reactions_add(channel=channel, timestamp=ts, name="zipper_mouth_face")
        except Exception as e:
            logger.warning(f"[ENGAGE] Impossibile aggiungere reaction stop: {e}")
        say(_engaged_stop_ack_text(user_id), thread_ts=thread_ts)
        return True
    finally:
        conn.close()


@app.action(ENGAGED_THREAD_STOP_ACTION_ID)
def handle_engaged_stop_button(ack, body, client):
    """Gestisce il bottone Block Kit 'Zitto' nei thread ingaggiati."""
    ack()

    action = (body.get("actions") or [{}])[0]
    try:
        value = json.loads(action.get("value") or "{}")
    except Exception:
        value = {}

    channel = value.get("channel") or body.get("channel", {}).get("id")
    thread_ts = value.get("thread_ts") or body.get("message", {}).get("thread_ts")
    message_ts = body.get("message", {}).get("ts")
    user_id = body.get("user", {}).get("id")

    if not channel or not thread_ts:
        logger.warning("[ENGAGE] Stop button senza channel/thread_ts")
        return

    conn, cursor = db_connect(database_path)
    try:
        cursor.execute(
            "SELECT engaged, stopped FROM engaged_threads WHERE thread_ts = ? AND channel = ?",
            (thread_ts, channel),
        )
        row = cursor.fetchone()
        if not row or not row[0]:
            logger.info(f"[ENGAGE] Stop button ignored: thread {thread_ts} not engaged")
            return

        if row[1]:
            logger.info(f"[ENGAGE] Stop button on already stopped thread: {thread_ts}")
            return

        cursor.execute(
            "UPDATE engaged_threads SET stopped = 1 "
            "WHERE thread_ts = ? AND channel = ?",
            (thread_ts, channel),
        )
        conn.commit()
        logger.info(f"[ENGAGE] Thread {thread_ts} stopped by button from user {user_id}")

        if message_ts:
            try:
                client.reactions_add(
                    channel=channel,
                    timestamp=message_ts,
                    name="zipper_mouth_face",
                )
            except Exception as e:
                logger.warning(f"[ENGAGE] Impossibile aggiungere reaction stop button: {e}")

        client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=_engaged_stop_ack_text(user_id),
        )
    except Exception as e:
        logger.error(f"[ENGAGE] Errore stop button: {e}")
        logger.error(traceback.format_exc())
    finally:
        conn.close()


def maybe_reply_to_engaged_thread(message, say):
    """Risponde a ogni nuovo messaggio utente nei thread ingaggiati con @bot /engage."""
    thread_ts = message.get("thread_ts")
    ts = message.get("ts")
    channel = message.get("channel")
    msg_user = message.get("user")
    previous_last_reply_ts = None
    claimed = False

    try:
        if msg_user == app._bot_user_id:
            return

        if not thread_ts or thread_ts == ts or not channel or not ts:
            return

        conn, cursor = db_connect(database_path)
        try:
            cursor.execute("BEGIN IMMEDIATE")
            cursor.execute(
                "SELECT engaged, stopped, last_reply_ts FROM engaged_threads "
                "WHERE thread_ts = ? AND channel = ?",
                (thread_ts, channel),
            )
            row = cursor.fetchone()
            if not row or not row[0] or row[1]:
                return
            previous_last_reply_ts = row[2]
            if previous_last_reply_ts:
                try:
                    stale_or_duplicate = float(ts) <= float(previous_last_reply_ts)
                except (TypeError, ValueError):
                    stale_or_duplicate = ts == previous_last_reply_ts
                if stale_or_duplicate:
                    logger.info(
                        "[ENGAGE] Stale or duplicate event %s ignored (last=%s)",
                        ts,
                        previous_last_reply_ts,
                    )
                    return

            # Claim atomico: eventi Slack duplicati o concorrenti producono una sola risposta.
            cursor.execute(
                """
                UPDATE engaged_threads
                SET last_reply_ts = ?
                WHERE thread_ts = ? AND channel = ? AND engaged = 1 AND stopped = 0
                  AND ((last_reply_ts IS NULL AND ? IS NULL) OR last_reply_ts = ?)
                """,
                (
                    ts,
                    thread_ts,
                    channel,
                    previous_last_reply_ts,
                    previous_last_reply_ts,
                ),
            )
            claimed = cursor.rowcount == 1
            conn.commit()
        finally:
            conn.close()
        if not claimed:
            logger.info("[ENGAGE] Event %s already claimed by another worker", ts)
            return

        openai_api_key = os.environ.get("OPENAI_API_KEY")
        if not openai_api_key:
            raise RuntimeError("OPENAI_API_KEY non configurata")

        thread_messages = get_thread_messages(
            channel,
            thread_ts,
            fallback_to_archive=True,
            raise_errors=True,
        )
        if not thread_messages:
            raise RuntimeError("thread Slack e fallback archivio non disponibili")

        throttle_conn, throttle_cursor = db_connect(database_path)
        try:
            allowed, throttle_message, throttle_info = check_ai_throttle(
                throttle_conn,
                throttle_cursor,
                msg_user,
                channel,
            )
        finally:
            throttle_conn.close()
        if not allowed:
            say(throttle_message, thread_ts=thread_ts)
            return

        client = OpenAI(api_key=openai_api_key)
        _auto_reply_in_thread(
            channel,
            thread_ts,
            thread_messages,
            client,
            say,
            response_suffix=format_ai_rate_limit_footer(throttle_info),
        )
    except Exception as e:
        # Rilascia il claim solo se nessun evento successivo lo ha già sostituito.
        if claimed:
            try:
                conn, cursor = db_connect(database_path)
                try:
                    cursor.execute(
                        "UPDATE engaged_threads SET last_reply_ts = ? "
                        "WHERE thread_ts = ? AND channel = ? AND last_reply_ts = ?",
                        (previous_last_reply_ts, thread_ts, channel, ts),
                    )
                    conn.commit()
                finally:
                    conn.close()
            except Exception:
                logger.exception("[ENGAGE] Failed to release event claim %s", ts)
        _report_ai_error(
            e,
            event=message,
            source="engaged_thread",
            say=say,
            thread_ts=thread_ts,
        )


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
                "SELECT decided, engaged, clown_assigned, stopped, cooldown_deferred, evaluated_at FROM trash_engaged_threads "
                "WHERE thread_ts = ? AND channel = ?",
                (thread_ts, channel),
            )
            row = cursor.fetchone()
            decided = bool(row[0]) if row else False
            engaged = bool(row[1]) if row else False
            clown_assigned = row[2] if row else None
            stopped = bool(row[3]) if row and len(row) > 3 else False
            cooldown_deferred = bool(row[4]) if row and len(row) > 4 else False
            evaluated_at = row[5] if row and len(row) > 5 else None

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
            if (
                decided
                and not engaged
                and not cooldown_deferred
                and _looks_like_legacy_cooldown_deferred(cursor, evaluated_at)
            ):
                logger.info(f"[TRASH] Thread {thread_ts} sembra rimandato da cooldown pre-migrazione")
                cooldown_deferred = True

            if decided and not engaged and cooldown_deferred and not _engage_cooldown_active(cursor):
                logger.info(f"[TRASH] Cooldown scaduto, rivaluto thread rimandato {thread_ts}")
                decided = False

            if not decided and user_reply_count >= AUTO_ENGAGE_REPLY_THRESHOLD:
                if _engage_cooldown_active(cursor):
                    logger.info(
                        f"[TRASH] Cooldown attivo, rimando decisione engage per thread {thread_ts}"
                    )
                    _save_trash_engage_decision(
                        cursor,
                        thread_ts,
                        channel,
                        decided=True,
                        engaged=False,
                        evaluated_at=now_ts,
                        last_reply_ts=ts,
                        cooldown_deferred=True,
                    )
                    conn.commit()
                    return

                logger.info(f"[TRASH] Decisione engage per thread {thread_ts} ({user_reply_count} reply)")
                engage, reply = _decide_engage(thread_messages, client)
                _save_trash_engage_decision(
                    cursor,
                    thread_ts,
                    channel,
                    decided=True,
                    engaged=engage,
                    evaluated_at=now_ts,
                    last_reply_ts=ts,
                    cooldown_deferred=False,
                )
                conn.commit()

                if engage and reply:
                    logger.info(f"[TRASH] Engaging thread {thread_ts}")
                    _say_trash_auto_reply(say, reply, channel, thread_ts)
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
                        add_clown_user(
                            conn, cursor, nickname_lower, expiry,
                            source="auto", assigned_by="auto",
                            reason=reason, thread_ts=thread_ts, channel=channel,
                        )
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
    route_link_message_event(
        event,
        normalize_url,
        lambda links: check_and_store_links(
            event,
            {"permalink": ""},
            say,
            links=links,
        ),
    )
    try:
        if _maybe_handle_engage_command(event, say):
            return
        handle_app_mention(event, say)
    except Exception as e:
        _report_ai_error(
            e,
            event=event,
            source="app_mention_router",
            say=say,
            thread_ts=event.get("thread_ts") or event.get("ts"),
        )


@app.event({"type": "message", "subtype": "thread_broadcast"})
def handle_message_thread_broadcast(event, say):
    handle_message(event, say)


@app.event({"type": "message", "subtype": "message_changed"})
def handle_message_changed(event, say):
    message = event.get("message", {})
    if "channel" not in message and event.get("channel"):
        message["channel"] = event["channel"]

    # Slack a volte invia message_changed quando un messaggio viene cancellato
    # In questo caso, il messaggio ha subtype "tombstone" o non ha "text"
    if message.get("subtype") == "tombstone" or "text" not in message:
        # Tratta come cancellazione
        deleted_ts = event.get("previous_message", {}).get("ts") or message.get("ts")
        if deleted_ts:
            logger.info(f"MESSAGE_CHANGED_AS_DELETED: Detected deletion via message_changed, ts={deleted_ts}")
            handle_message_deleted_logic(deleted_ts, event.get("channel"))
        return

    links = extract_external_links(message.get("text", ""), normalize_url)
    conn, cursor = db_connect(database_path)
    try:
        cursor.execute(
            """
            UPDATE messages SET message = ?, embeddings = ?
            WHERE user = ? AND channel = ? AND timestamp = ?
            """,
            (
                message["text"],
                create_embeddings(message["text"]),
                message["user"],
                event["channel"],
                message["ts"],
            ),
        )
        obsolete_alerts = reconcile_edited_message_links(
            conn,
            channel=event["channel"],
            message_timestamp=message["ts"],
            active_normalized_urls={link.normalized_url for link in links},
        )
        conn.commit()
    finally:
        conn.close()

    _cleanup_stored_duplicate_alerts(obsolete_alerts)
    permalink = {"permalink": ""}
    try:
        permalink = app.client.chat_getPermalink(
            channel=event["channel"],
            message_ts=message["ts"],
        )
    except Exception as e:
        logger.warning(f"Could not get permalink for edited message: {e}")
    check_and_store_links(message, permalink, say)
    sync_xcancel_alternatives_for_message(message, say)


def handle_message_deleted_logic(deleted_ts, channel):
    """Logica comune per gestire la cancellazione di un messaggio."""
    if not deleted_ts:
        logger.warning("MESSAGE_DELETED: No deleted_ts provided, skipping cleanup")
        return

    link_conn, _ = db_connect(database_path)
    try:
        obsolete_alerts = collect_deleted_message_alerts(
            link_conn,
            channel=channel,
            message_timestamp=deleted_ts,
        )
    finally:
        link_conn.close()
    _cleanup_stored_duplicate_alerts(obsolete_alerts)

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

        delete_xcancel_alert(deleted_ts, channel)

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
    try:
        migrate_db(conn, cursor)
        logger.info("Database migrated")

        # Update the users and channels in the DB and in the local memory mapping
        try:
            update_users(conn, cursor)
            update_channels(conn, cursor)
        except Exception:
            # A failed executemany leaves SQLite inside a write transaction.
            # Roll it back before Gunicorn forks, then always close the master
            # connection so workers cannot inherit a writer lock.
            conn.rollback()
            logger.exception("Error updating users and channels")
    finally:
        conn.close()

    # Log stato iniziale della lista clown
    logger.info(f"[CLOWN] Bot initialized. Clown list is empty (will be populated via DM commands)")


def start_link_enrichment_worker():
    """Start one daemon worker in the current development/Gunicorn process."""
    global _link_enrichment_worker
    enabled = os.getenv("LINK_ENRICHMENT_ENABLED", "true").strip().lower()
    if enabled not in {"1", "true", "yes", "on"}:
        logger.info("Link enrichment worker disabled")
        return None

    def positive_env_float(name, default):
        try:
            value = float(os.getenv(name, str(default)))
            if value <= 0:
                raise ValueError
            return value
        except ValueError:
            logger.warning("Invalid %s; using %s", name, default)
            return default

    if _link_enrichment_worker is None:
        _link_enrichment_worker = LinkEnrichmentWorker(
            database_path,
            create_embeddings,
            on_document_ready=process_ready_link_document,
            poll_interval=positive_env_float("LINK_ENRICHMENT_POLL_SECONDS", 2.0),
            error_backoff=positive_env_float(
                "LINK_ENRICHMENT_ERROR_BACKOFF_SECONDS", 5.0
            ),
        )
    _link_enrichment_worker.start()
    return _link_enrichment_worker


def stop_link_enrichment_worker():
    global _link_enrichment_worker
    if _link_enrichment_worker is not None:
        _link_enrichment_worker.stop()
        _link_enrichment_worker = None
        
        
def main():
    init()
    start_link_enrichment_worker()

    # Start the development server
    app.start(port=port)


if __name__ == "__main__":
    main()

# Make sure this function is accessible when imported
__all__ = [
    'update_users',
    'app',
    'start_link_enrichment_worker',
    'stop_link_enrichment_worker',
]
