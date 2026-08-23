import re

MAX_CONTEXT_MESSAGE_CHARS = 2000
MAX_CONTEXT_CHARS = 240_000


def strip_bot_mention(text, bot_user_id):
    """Remove Slack bot mentions from a message while preserving the rest."""
    if not text or not bot_user_id:
        return (text or "").strip()

    mention_re = rf"<@{re.escape(bot_user_id)}(?:\|[^>]+)?>"
    stripped = re.sub(mention_re, "", text)
    return re.sub(r"\s+", " ", stripped).strip()


def is_engage_request(text, bot_user_id):
    """True when the user explicitly asks the bot to stay engaged in a thread."""
    command = strip_bot_mention(text, bot_user_id)
    command = re.sub(r"[`*_~]+", "", command).strip()
    return command.lower() == "/engage"


def get_ai_context_scope(event):
    """Decide se usare il contesto del thread o quello del canale."""
    message_ts = event.get("ts")
    thread_ts = event.get("thread_ts")

    if thread_ts and thread_ts != message_ts:
        return "thread"

    return "channel"


def format_messages_for_prompt(messages):
    """Format messaggi per il prompt LLM. Include user_id (se disponibile)
    in modo che il modello possa generare mention Slack native `<@USER_ID>`.

    Il contesto è limitato e conserva i messaggi più recenti per evitare che un
    canale molto lungo saturi la finestra del modello prima delle evidenze di
    ricerca.
    """
    reversed_lines = []
    used_chars = 0
    for msg in reversed(messages):
        user = msg.get("user", "Unknown")
        uid = msg.get("user_id", "")
        text = str(msg.get("text", "") or "").strip()
        if len(text) > MAX_CONTEXT_MESSAGE_CHARS:
            text = text[: MAX_CONTEXT_MESSAGE_CHARS - 1].rstrip() + "…"
        if uid:
            line = f"{user} (<@{uid}>): {text}"
        else:
            line = f"{user}: {text}"
        projected = used_chars + len(line) + (1 if reversed_lines else 0)
        if projected > MAX_CONTEXT_CHARS:
            break
        reversed_lines.append(line)
        used_chars = projected
    return "\n".join(reversed(reversed_lines))
