def get_ai_context_scope(event):
    """Decide se usare il contesto del thread o quello del canale."""
    message_ts = event.get("ts")
    thread_ts = event.get("thread_ts")

    if thread_ts and thread_ts != message_ts:
        return "thread"

    return "channel"


def format_messages_for_prompt(messages):
    """Format messaggi per il prompt LLM. Include user_id (se disponibile)
    in modo che il modello possa generare mention Slack native `<@USER_ID>`."""
    lines = []
    for msg in messages:
        user = msg.get("user", "Unknown")
        uid = msg.get("user_id", "")
        text = msg.get("text", "")
        if uid:
            lines.append(f"{user} (<@{uid}>): {text}")
        else:
            lines.append(f"{user}: {text}")
    return "\n".join(lines)
