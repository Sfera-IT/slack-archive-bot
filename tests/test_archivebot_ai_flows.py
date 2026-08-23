import importlib
import os
import sqlite3
import sys
from types import SimpleNamespace

import pytest


ROOT_DIR = os.path.dirname(os.path.dirname(__file__))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)


class FakeSlackClient:
    def __init__(self):
        self.posted = []

    def auth_test(self):
        return {"user_id": "UBOT"}

    def users_info(self, user):
        return {"user": {"profile": {"display_name": "archivebot"}}}

    def chat_postMessage(self, **kwargs):
        self.posted.append(kwargs)
        return {"ok": True, "ts": "999.1"}


class FakeSlackApp:
    def __init__(self, **_kwargs):
        self.client = FakeSlackClient()

    def _decorator(self, *_args, **_kwargs):
        return lambda function: function

    event = _decorator
    message = _decorator
    action = _decorator


@pytest.fixture
def archivebot_module(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "slack_bolt", SimpleNamespace(App=FakeSlackApp))
    monkeypatch.setitem(
        sys.modules,
        "sentence_transformers",
        SimpleNamespace(SentenceTransformer=object),
    )
    sys.modules.pop("archivebot", None)
    module = importlib.import_module("archivebot")
    module.database_path = str(tmp_path / "archive.sqlite")
    conn, cursor = module.db_connect(module.database_path)
    module.migrate_db(conn, cursor)
    conn.close()
    return module


def test_private_debug_command_is_admin_only_and_toggles_from_disabled(
    archivebot_module,
):
    bot = archivebot_module
    replies = []
    conn, cursor = bot.db_connect(bot.database_path)
    try:
        bot.handle_query(
            {"text": "/debug", "user": bot.ADMIN_USERS[0]},
            cursor,
            replies.append,
        )
        assert bot.is_ai_debug_enabled(cursor, bot.ADMIN_USERS[0])
        assert "attivato" in replies[-1]

        bot.handle_query(
            {"text": "/debug", "user": bot.ADMIN_USERS[0]},
            cursor,
            replies.append,
        )
        assert not bot.is_ai_debug_enabled(cursor, bot.ADMIN_USERS[0])
        assert "disattivato" in replies[-1]

        bot.handle_query(
            {"text": "/debug", "user": "UNOTADMIN"},
            cursor,
            replies.append,
        )
        assert "Solo gli amministratori" in replies[-1]
    finally:
        conn.close()


def test_ai_error_report_goes_only_to_opted_in_admin_and_covers_engage(
    archivebot_module,
):
    bot = archivebot_module
    conn, cursor = bot.db_connect(bot.database_path)
    bot.set_ai_debug_enabled(cursor, bot.ADMIN_USERS[0], True)
    conn.commit()
    conn.close()
    public_replies = []

    bot._report_ai_error(
        RuntimeError("engage exploded"),
        event={"user": "UTRIGGER", "channel": "C1", "ts": "100.2", "thread_ts": "100.1"},
        source="engaged_thread",
        say=lambda text, **kwargs: public_replies.append((text, kwargs)),
        thread_ts="100.1",
    )

    assert len(bot.app.client.posted) == 1
    private = bot.app.client.posted[0]
    assert private["channel"] == bot.ADMIN_USERS[0]
    assert "engaged_thread" in private["text"]
    assert "engage exploded" in private["text"]
    assert "UTRIGGER" in private["text"]
    assert public_replies[0][1]["thread_ts"] == "100.1"
    assert "Riferimento" in public_replies[0][0]


def test_rate_limit_footer_is_visible_and_counts_the_accepted_request(
    archivebot_module,
):
    bot = archivebot_module
    conn, cursor = bot.db_connect(bot.database_path)
    try:
        allowed, _, first = bot.check_ai_throttle(conn, cursor, "U1", "C1")
        assert allowed
        assert bot.format_ai_rate_limit_footer(first) == (
            "📊 Rate limit per user: 1/2 al minuto, 1/10 all'ora"
        )

        allowed, _, second = bot.check_ai_throttle(conn, cursor, "U1", "C1")
        assert allowed
        assert "2/2 al minuto, 2/10 all'ora" in bot.format_ai_rate_limit_footer(second)

        allowed, message, third = bot.check_ai_throttle(conn, cursor, "U1", "C1")
        assert not allowed
        assert "Riprova alle" in message
        assert bot.format_ai_rate_limit_footer(third) in message
    finally:
        conn.close()


def _seed_engaged_thread(bot):
    conn = sqlite3.connect(bot.database_path)
    conn.execute(
        """
        INSERT INTO engaged_threads
        (thread_ts, channel, engaged, stopped, engaged_at, engaged_by, last_reply_ts)
        VALUES ('100.1', 'C1', 1, 0, 1, 'UOWNER', '100.1')
        """
    )
    conn.commit()
    conn.close()


def test_engage_claims_each_event_once_and_runs_the_ai_reply(
    archivebot_module,
    monkeypatch,
):
    bot = archivebot_module
    _seed_engaged_thread(bot)
    calls = []
    monkeypatch.setenv("OPENAI_API_KEY", "dummy")
    monkeypatch.setattr(
        bot,
        "get_thread_messages",
        lambda *_args, **_kwargs: [
            {"user": "Alice", "user_id": "U1", "text": "ciao", "ts": "100.2"}
        ],
    )
    monkeypatch.setattr(bot, "OpenAI", lambda **_kwargs: object())
    monkeypatch.setattr(
        bot,
        "_auto_reply_in_thread",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    message = {
        "user": "U1",
        "channel": "C1",
        "channel_type": "channel",
        "thread_ts": "100.1",
        "ts": "100.2",
        "text": "ciao",
    }

    bot.maybe_reply_to_engaged_thread(message, lambda *_args, **_kwargs: None)
    bot.maybe_reply_to_engaged_thread(message, lambda *_args, **_kwargs: None)

    assert len(calls) == 1
    assert "1/2 al minuto, 1/10 all'ora" in calls[0][1]["response_suffix"]
    conn = sqlite3.connect(bot.database_path)
    row = conn.execute(
        "SELECT last_reply_ts FROM engaged_threads WHERE thread_ts = '100.1' AND channel = 'C1'"
    ).fetchone()
    conn.close()
    assert row == ("100.2",)


def test_engage_ignores_out_of_order_slack_events(archivebot_module, monkeypatch):
    bot = archivebot_module
    _seed_engaged_thread(bot)
    conn = sqlite3.connect(bot.database_path)
    conn.execute(
        "UPDATE engaged_threads SET last_reply_ts = '100.3' "
        "WHERE thread_ts = '100.1' AND channel = 'C1'"
    )
    conn.commit()
    conn.close()
    fetched = []
    monkeypatch.setattr(
        bot,
        "get_thread_messages",
        lambda *_args, **_kwargs: fetched.append(True),
    )

    bot.maybe_reply_to_engaged_thread(
        {
            "user": "U1",
            "channel": "C1",
            "thread_ts": "100.1",
            "ts": "100.2",
            "text": "evento vecchio",
        },
        lambda *_args, **_kwargs: None,
    )

    assert fetched == []


def test_engage_failure_releases_claim_and_routes_private_debug(
    archivebot_module,
    monkeypatch,
):
    bot = archivebot_module
    _seed_engaged_thread(bot)
    monkeypatch.setenv("OPENAI_API_KEY", "dummy")
    monkeypatch.setattr(
        bot,
        "get_thread_messages",
        lambda *_args, **_kwargs: [
            {"user": "Alice", "user_id": "U1", "text": "ciao", "ts": "100.2"}
        ],
    )
    monkeypatch.setattr(bot, "OpenAI", lambda **_kwargs: object())
    monkeypatch.setattr(
        bot,
        "_auto_reply_in_thread",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("responses failure")),
    )
    reported = []
    monkeypatch.setattr(
        bot,
        "_report_ai_error",
        lambda exception, **kwargs: reported.append((exception, kwargs)),
    )
    message = {
        "user": "U1",
        "channel": "C1",
        "thread_ts": "100.1",
        "ts": "100.2",
        "text": "ciao",
    }

    bot.maybe_reply_to_engaged_thread(message, lambda *_args, **_kwargs: None)

    assert len(reported) == 1
    assert reported[0][1]["source"] == "engaged_thread"
    conn = sqlite3.connect(bot.database_path)
    row = conn.execute(
        "SELECT last_reply_ts FROM engaged_threads WHERE thread_ts = '100.1' AND channel = 'C1'"
    ).fetchone()
    conn.close()
    assert row == ("100.1",)


def test_engage_uses_archive_fallback_when_slack_thread_read_fails(
    archivebot_module,
    monkeypatch,
):
    bot = archivebot_module
    conn = sqlite3.connect(bot.database_path)
    conn.execute("INSERT INTO users(name, id, avatar) VALUES ('Alice', 'U1', '')")
    conn.execute("INSERT INTO channels(name, id, is_private) VALUES ('dev', 'C1', 0)")
    conn.execute(
        """
        INSERT INTO messages(message, user, channel, timestamp, permalink, thread_ts)
        VALUES ('messaggio archiviato', 'U1', 'C1', '100.2', '', '100.1')
        """
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(
        bot.app.client,
        "conversations_replies",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("missing_scope")),
        raising=False,
    )

    messages = bot.get_thread_messages(
        "C1",
        "100.1",
        fallback_to_archive=True,
        raise_errors=True,
    )

    assert [message["text"] for message in messages] == ["messaggio archiviato"]
