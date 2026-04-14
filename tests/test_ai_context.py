import os
import sys

# Ensure project root is importable
ROOT_DIR = os.path.dirname(os.path.dirname(__file__))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from ai_context import format_messages_for_prompt, get_ai_context_scope


def test_get_ai_context_scope_uses_thread_for_thread_replies():
    event = {
        "ts": "1710000000.200000",
        "thread_ts": "1710000000.100000",
    }

    assert get_ai_context_scope(event) == "thread"


def test_get_ai_context_scope_uses_channel_for_root_messages():
    event = {
        "ts": "1710000000.100000",
    }

    assert get_ai_context_scope(event) == "channel"


def test_get_ai_context_scope_uses_channel_for_thread_root_posts():
    event = {
        "ts": "1710000000.100000",
        "thread_ts": "1710000000.100000",
    }

    assert get_ai_context_scope(event) == "channel"


def test_format_messages_for_prompt_preserves_order():
    messages = [
        {"user": "Alice", "text": "Prima riga"},
        {"user": "Bob", "text": "Seconda riga"},
    ]

    assert format_messages_for_prompt(messages) == "Alice: Prima riga\nBob: Seconda riga"
