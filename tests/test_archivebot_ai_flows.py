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
        self.auth_calls = 0

    def auth_test(self):
        self.auth_calls += 1
        return {"user_id": "UBOT"}

    def users_info(self, user):
        return {"user": {"profile": {"display_name": "archivebot"}}}

    def chat_postMessage(self, **kwargs):
        self.posted.append(kwargs)
        return {"ok": True, "ts": "999.1"}


class FakeSlackApp:
    def __init__(self, **_kwargs):
        self.client = FakeSlackClient()
        self.error_handler = None

    def _decorator(self, *_args, **_kwargs):
        return lambda function: function

    event = _decorator
    message = _decorator
    action = _decorator
    command = _decorator

    def error(self, function):
        self.error_handler = function
        return function


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


def test_import_does_not_call_slack_api(archivebot_module):
    assert archivebot_module.app.client.auth_calls == 0
    assert archivebot_module.app._bot_user_id


def test_update_channels_replaces_existing_memberships(archivebot_module, monkeypatch):
    bot = archivebot_module
    conn, cursor = bot.db_connect(bot.database_path)
    cursor.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_members_channel_user "
        "ON members(channel, user)"
    )
    cursor.execute(
        "INSERT INTO channels(name, id, is_private) VALUES (?, ?, ?)",
        ("old-name", "C1", False),
    )
    cursor.execute(
        "INSERT INTO members(channel, user) VALUES (?, ?)",
        ("C1", "U1"),
    )
    conn.commit()

    bot.app.client.conversations_list = lambda **_kwargs: {
        "channels": [{"id": "C1", "is_member": True}]
    }
    snapshots = [
        ("C1", "new-name", False, [("C1", "U1"), ("C1", "U2")]),
        ("C1", "newest-name", False, [("C1", "U2"), ("C1", "U3")]),
    ]
    monkeypatch.setattr(bot, "get_channel_info", lambda _channel_id: snapshots.pop(0))

    bot.update_channels(conn, cursor)
    bot.update_channels(conn, cursor)

    assert cursor.execute(
        "SELECT name FROM channels WHERE id = 'C1'"
    ).fetchone() == ("newest-name",)
    assert cursor.execute(
        "SELECT channel, user FROM members ORDER BY channel, user"
    ).fetchall() == [("C1", "U2"), ("C1", "U3")]
    conn.close()


def test_init_rolls_back_and_closes_failed_refresh_connection(
    archivebot_module, monkeypatch
):
    bot = archivebot_module
    original_db_connect = bot.db_connect
    opened_connections = []

    def tracked_db_connect(database_path):
        conn, cursor = original_db_connect(database_path)
        opened_connections.append(conn)
        return conn, cursor

    def failing_channel_refresh(_conn, cursor):
        cursor.execute(
            "INSERT INTO channels(name, id, is_private) VALUES (?, ?, ?)",
            ("temporary", "C-LOCK", False),
        )
        raise RuntimeError("channel refresh failed")

    monkeypatch.setattr(bot, "db_connect", tracked_db_connect)
    monkeypatch.setattr(bot, "update_users", lambda _conn, _cursor: None)
    monkeypatch.setattr(bot, "update_channels", failing_channel_refresh)

    bot.init()

    with sqlite3.connect(bot.database_path, timeout=0.05) as second_conn:
        second_conn.execute("BEGIN IMMEDIATE")
        assert second_conn.execute(
            "SELECT COUNT(*) FROM channels WHERE id = 'C-LOCK'"
        ).fetchone() == (0,)
        second_conn.rollback()

    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        opened_connections[0].execute("SELECT 1")


def test_private_debug_command_is_admin_only_and_toggles_from_disabled(
    archivebot_module,
):
    bot = archivebot_module
    replies = []
    conn, cursor = bot.db_connect(bot.database_path)
    try:
        bot.handle_query(
            {"text": "debug", "user": bot.ADMIN_USERS[0]},
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


def test_native_debug_slash_command_acks_and_uses_same_opt_in_state(
    archivebot_module,
):
    bot = archivebot_module
    acknowledgements = []
    replies = []

    bot.handle_ai_debug_slash_command(
        lambda: acknowledgements.append(True),
        {"user_id": bot.ADMIN_USERS[0], "text": "on"},
        replies.append,
    )

    assert acknowledgements == [True]
    assert "attivato" in replies[-1]
    conn, cursor = bot.db_connect(bot.database_path)
    try:
        assert bot.is_ai_debug_enabled(cursor, bot.ADMIN_USERS[0])
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


def test_message_replied_wrapper_routes_reply_to_engage(
    archivebot_module,
    monkeypatch,
):
    bot = archivebot_module
    _seed_engaged_thread(bot)
    routed = []
    monkeypatch.setattr(
        bot.app.client,
        "conversations_replies",
        lambda **_kwargs: {
            "messages": [
                {"user": "UOWNER", "text": "root", "ts": "100.1"},
                {"user": "U1", "text": "mi ricevi?", "ts": "100.2"},
            ],
            "response_metadata": {"next_cursor": ""},
        },
        raising=False,
    )
    monkeypatch.setattr(
        bot,
        "handle_message",
        lambda message, say: routed.append((message, say)),
    )
    say = lambda *_args, **_kwargs: None

    bot.handle_message_replied(
        {
            "channel": "C1",
            "channel_type": "channel",
            "message": {
                "ts": "100.1",
                "latest_reply": "100.2",
                "replies": [{"user": "U1", "ts": "100.2"}],
            },
        },
        say,
    )

    assert len(routed) == 1
    assert routed[0][0]["text"] == "mi ricevi?"
    assert routed[0][0]["thread_ts"] == "100.1"
    assert routed[0][1] is say


def test_message_replied_failure_reaches_private_debug_pipeline(
    archivebot_module,
    monkeypatch,
):
    bot = archivebot_module
    _seed_engaged_thread(bot)
    reported = []
    monkeypatch.setattr(
        bot.app.client,
        "conversations_replies",
        lambda **_kwargs: {
            "messages": [],
            "response_metadata": {"next_cursor": ""},
        },
        raising=False,
    )
    monkeypatch.setattr(
        bot,
        "_report_ai_error",
        lambda exception, **kwargs: reported.append((exception, kwargs)),
    )

    bot.handle_message_replied(
        {
            "channel": "C1",
            "message": {"ts": "100.1", "latest_reply": "100.2"},
        },
        lambda *_args, **_kwargs: None,
    )

    assert len(reported) == 1
    assert reported[0][1]["source"] == "message_replied_router"
    assert reported[0][1]["thread_ts"] == "100.1"


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
