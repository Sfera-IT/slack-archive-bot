import os
import sys

# Ensure project root is importable
ROOT_DIR = os.path.dirname(os.path.dirname(__file__))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from ai_context import (
    format_messages_for_prompt,
    get_ai_context_scope,
    is_engage_request,
    strip_bot_mention,
)


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


def test_strip_bot_mention_removes_native_slack_mention():
    assert strip_bot_mention("<@U123BOT> /engage", "U123BOT") == "/engage"


def test_strip_bot_mention_removes_native_slack_mention_with_label():
    assert strip_bot_mention("<@U123BOT|archivebot> /engage", "U123BOT") == "/engage"


def test_is_engage_request_accepts_explicit_command():
    assert is_engage_request("<@U123BOT> /engage", "U123BOT")


def test_is_engage_request_accepts_markdown_wrapped_command():
    assert is_engage_request("<@U123BOT> `/engage`", "U123BOT")


def test_is_engage_request_rejects_plain_mention():
    assert not is_engage_request("<@U123BOT>", "U123BOT")


def test_is_engage_request_rejects_other_requests():
    assert not is_engage_request("<@U123BOT> riassumi questo thread", "U123BOT")
