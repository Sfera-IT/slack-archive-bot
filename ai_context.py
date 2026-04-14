def get_ai_context_scope(event):
    """Decide se usare il contesto del thread o quello del canale."""
    message_ts = event.get("ts")
    thread_ts = event.get("thread_ts")

    if thread_ts and thread_ts != message_ts:
        return "thread"

    return "channel"


def format_messages_for_prompt(messages):
    return "\n".join(f"{msg['user']}: {msg['text']}" for msg in messages)
