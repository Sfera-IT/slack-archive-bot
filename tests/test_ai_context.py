import os
import sys

# Ensure project root is importable
ROOT_DIR = os.path.dirname(os.path.dirname(__file__))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from ai_context import (
    MAX_CONTEXT_CHARS,
    MAX_CONTEXT_MESSAGE_CHARS,
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

    assert (
        format_messages_for_prompt(messages) == "Alice: Prima riga\nBob: Seconda riga"
    )


def test_format_messages_for_prompt_bounds_each_message():
    result = format_messages_for_prompt(
        [{"user": "Alice", "text": "x" * (MAX_CONTEXT_MESSAGE_CHARS + 100)}]
    )

    assert result.endswith("…")
    assert len(result.split(": ", 1)[1]) == MAX_CONTEXT_MESSAGE_CHARS


def test_format_messages_for_prompt_keeps_recent_messages_within_total_budget():
    messages = [
        {"user": f"User-{index}", "text": "x" * MAX_CONTEXT_MESSAGE_CHARS}
        for index in range((MAX_CONTEXT_CHARS // MAX_CONTEXT_MESSAGE_CHARS) + 20)
    ]

    result = format_messages_for_prompt(messages)

    assert len(result) <= MAX_CONTEXT_CHARS
    assert "User-0:" not in result
    assert f"User-{len(messages) - 1}:" in result


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
