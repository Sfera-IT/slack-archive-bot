import importlib
import os
import sqlite3
import sys
from types import ModuleType, SimpleNamespace

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


def test_instagram_media_is_queued_before_mention_early_return(
    archivebot_module, monkeypatch
):
    bot = archivebot_module
    queued = []
    mentioned = []
    bot.app._bot_user_id = "UBOT"
    monkeypatch.setattr(bot, "_initialize_bot_identity", lambda: "UBOT")
    monkeypatch.setattr(bot, "_archive_optout_enabled", lambda _user: False)
    monkeypatch.setattr(bot, "route_link_message_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        bot,
        "queue_instagram_media_from_message",
        lambda message: queued.append(message.copy()),
        raising=False,
    )
    monkeypatch.setattr(bot, "_maybe_handle_engaged_stop", lambda *_args: False)
    monkeypatch.setattr(
        bot, "_maybe_handle_engage_command", lambda *_args: False
    )
    monkeypatch.setattr(
        bot,
        "handle_app_mention",
        lambda message, say: mentioned.append(message.copy()),
    )

    message = {
        "user": "U1",
        "channel": "C1",
        "channel_type": "channel",
        "ts": "100.1",
        "text": "<@UBOT> https://www.instagram.com/reel/ABC/",
    }
    bot.handle_message(message, lambda *_args, **_kwargs: None)

    assert queued == [message]
    assert mentioned == [message]


def test_instagram_media_queue_is_opt_in_and_public_channels_only(
    archivebot_module, monkeypatch
):
    bot = archivebot_module
    message = {
        "user": "U1",
        "channel": "C1",
        "channel_type": "channel",
        "ts": "100.1",
        "text": "https://www.instagram.com/p/PHOTO/",
    }

    monkeypatch.delenv("INSTAGRAM_MEDIA_ARCHIVE_ENABLED", raising=False)
    assert bot.queue_instagram_media_from_message(message) == 0

    monkeypatch.setenv("INSTAGRAM_MEDIA_ARCHIVE_ENABLED", "true")
    private_message = {**message, "channel_type": "group", "ts": "100.2"}
    assert bot.queue_instagram_media_from_message(private_message) == 0
    assert bot.queue_instagram_media_from_message(message) == 1

    conn = sqlite3.connect(bot.database_path)
    assert conn.execute(
        "SELECT channel, message_timestamp, shortcode FROM instagram_media_jobs"
    ).fetchall() == [("C1", "100.1", "PHOTO")]
    conn.close()


def test_instagram_media_queue_caps_links_per_message(
    archivebot_module, monkeypatch
):
    bot = archivebot_module
    monkeypatch.setenv("INSTAGRAM_MEDIA_ARCHIVE_ENABLED", "true")
    monkeypatch.setenv("INSTAGRAM_MEDIA_MAX_LINKS_PER_MESSAGE", "2")
    message = {
        "user": "U1",
        "channel": "C1",
        "channel_type": "channel",
        "ts": "100.1",
        "text": " ".join(
            f"https://www.instagram.com/p/POST{index}/" for index in range(4)
        ),
    }

    assert bot.queue_instagram_media_from_message(message) == 2
    conn = sqlite3.connect(bot.database_path)
    assert conn.execute(
        "SELECT shortcode FROM instagram_media_jobs ORDER BY rowid"
    ).fetchall() == [("POST0",), ("POST1",)]
    conn.close()


def test_message_changed_reconciles_instagram_media_jobs(
    archivebot_module, monkeypatch
):
    bot = archivebot_module
    monkeypatch.setenv("INSTAGRAM_MEDIA_ARCHIVE_ENABLED", "true")
    original = {
        "user": "U1",
        "channel": "C1",
        "channel_type": "channel",
        "ts": "100.1",
        "text": "https://www.instagram.com/p/OLD/",
    }
    assert bot.queue_instagram_media_from_message(original) == 1
    monkeypatch.setattr(bot, "create_embeddings", lambda _text: b"")
    monkeypatch.setattr(bot, "extract_external_links", lambda *_args: [])
    monkeypatch.setattr(bot, "reconcile_edited_message_links", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(bot, "_cleanup_stored_duplicate_alerts", lambda *_args: None)
    monkeypatch.setattr(bot, "check_and_store_links", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(bot, "sync_xcancel_alternatives_for_message", lambda *_args: None)

    bot.handle_message_changed(
        {
            "channel": "C1",
            "channel_type": "channel",
            "message": {
                "user": "U1",
                "ts": "100.1",
                "text": "https://www.instagram.com/reel/NEW/",
            },
        },
        lambda *_args, **_kwargs: None,
    )

    conn = sqlite3.connect(bot.database_path)
    assert dict(
        conn.execute("SELECT shortcode, status FROM instagram_media_jobs").fetchall()
    ) == {"OLD": "cancelled", "NEW": "pending"}
    conn.close()


def test_message_changed_removing_all_instagram_links_cancels_pending_job(
    archivebot_module, monkeypatch
):
    bot = archivebot_module
    monkeypatch.setenv("INSTAGRAM_MEDIA_ARCHIVE_ENABLED", "true")
    original = {
        "user": "U1",
        "channel": "C1",
        "channel_type": "channel",
        "ts": "100.1",
        "text": "https://www.instagram.com/p/REMOVE/",
    }
    assert bot.queue_instagram_media_from_message(original) == 1
    monkeypatch.setattr(bot, "create_embeddings", lambda _text: b"")
    monkeypatch.setattr(bot, "extract_external_links", lambda *_args: [])
    monkeypatch.setattr(bot, "reconcile_edited_message_links", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(bot, "_cleanup_stored_duplicate_alerts", lambda *_args: None)
    monkeypatch.setattr(bot, "check_and_store_links", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(bot, "sync_xcancel_alternatives_for_message", lambda *_args: None)

    bot.handle_message_changed(
        {
            "channel": "C1",
            "channel_type": "channel",
            "message": {
                "user": "U1",
                "ts": "100.1",
                "text": "link removed",
            },
        },
        lambda *_args, **_kwargs: None,
    )

    conn = sqlite3.connect(bot.database_path)
    assert conn.execute(
        "SELECT status FROM instagram_media_jobs WHERE shortcode = 'REMOVE'"
    ).fetchone()[0] == "cancelled"
    conn.close()


def test_message_deleted_cancels_instagram_media_jobs(
    archivebot_module, monkeypatch
):
    bot = archivebot_module
    monkeypatch.setenv("INSTAGRAM_MEDIA_ARCHIVE_ENABLED", "true")
    message = {
        "user": "U1",
        "channel": "C1",
        "channel_type": "channel",
        "ts": "100.1",
        "text": "https://www.instagram.com/p/DELETE/",
    }
    assert bot.queue_instagram_media_from_message(message) == 1
    monkeypatch.setattr(bot, "collect_deleted_message_alerts", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(bot, "_cleanup_stored_duplicate_alerts", lambda *_args: None)
    monkeypatch.setattr(bot, "delete_xcancel_alert", lambda *_args: None)

    bot.handle_message_deleted({"channel": "C1", "deleted_ts": "100.1"})

    conn = sqlite3.connect(bot.database_path)
    assert conn.execute(
        "SELECT status FROM instagram_media_jobs"
    ).fetchone()[0] == "cancelled"
    conn.close()


def test_instagram_worker_nonfinite_delays_use_defaults(archivebot_module, monkeypatch):
    bot = archivebot_module
    captured = {}

    class CapturingWorker:
        def __init__(self, _database_path, _client, **kwargs):
            captured.update(kwargs)

        def start(self):
            return None

    monkeypatch.setenv("INSTAGRAM_MEDIA_ARCHIVE_ENABLED", "true")
    monkeypatch.setenv("INSTAGRAM_MEDIA_POLL_SECONDS", "inf")
    monkeypatch.setenv("INSTAGRAM_MEDIA_ERROR_BACKOFF_SECONDS", "nan")
    monkeypatch.setattr(bot, "InstagramMediaWorker", CapturingWorker)
    bot._instagram_media_worker = None

    bot.start_instagram_media_worker()

    assert captured["poll_interval"] == 2.0
    assert captured["error_backoff"] == 5.0


def test_stop_instagram_worker_retains_live_worker_reference(archivebot_module):
    bot = archivebot_module

    class WorkerStillStopping:
        def stop(self):
            return False

    worker = WorkerStillStopping()
    bot._instagram_media_worker = worker

    assert bot.stop_instagram_media_worker() is False
    assert bot._instagram_media_worker is worker


def test_healthz_is_public_and_does_not_require_runtime_dependencies(
    archivebot_module, monkeypatch
):
    flask_adapter = ModuleType("slack_bolt.adapter.flask")

    class FakeSlackRequestHandler:
        def __init__(self, app):
            self.app = app

        def handle(self, _request):
            return "", 200

    flask_adapter.SlackRequestHandler = FakeSlackRequestHandler
    monkeypatch.setitem(sys.modules, "slack_bolt.adapter", ModuleType("slack_bolt.adapter"))
    monkeypatch.setitem(sys.modules, "slack_bolt.adapter.flask", flask_adapter)
    monkeypatch.setitem(sys.modules, "archivebot", archivebot_module)
    sys.modules.pop("flask_app", None)

    web = importlib.import_module("flask_app")
    try:
        response = web.flask_app.test_client().get("/healthz")

        assert response.status_code == 200
        assert response.get_json() == {"status": "ok"}
        assert response.headers["Cache-Control"] == "no-store"
    finally:
        sys.modules.pop("flask_app", None)


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


def test_member_joined_channel_retry_is_idempotent(archivebot_module):
    bot = archivebot_module
    event = {"channel": "C1", "user": "U1"}

    bot.handle_join(event)
    bot.handle_join(event)

    conn = sqlite3.connect(bot.database_path)
    try:
        assert conn.execute(
            "SELECT channel, user FROM members"
        ).fetchall() == [("C1", "U1")]
    finally:
        conn.close()


def test_bot_join_refreshes_channel_membership_snapshot(
    archivebot_module,
    monkeypatch,
):
    bot = archivebot_module
    conn = sqlite3.connect(bot.database_path)
    conn.execute(
        "INSERT INTO channels(name, id, is_private) VALUES ('old', 'C1', 0)"
    )
    conn.execute("INSERT INTO members(channel, user) VALUES ('C1', 'USTALE')")
    conn.commit()
    conn.close()
    monkeypatch.setattr(
        bot,
        "get_channel_info",
        lambda _channel: (
            "C1",
            "new",
            False,
            [("C1", "U1"), ("C1", "U1"), ("C1", "U2")],
        ),
    )

    event = {"channel": "C1", "user": bot.app._bot_user_id}
    bot.handle_join(event)
    bot.handle_join(event)

    conn = sqlite3.connect(bot.database_path)
    try:
        assert conn.execute(
            "SELECT name FROM channels WHERE id = 'C1'"
        ).fetchone() == ("new",)
        assert conn.execute(
            "SELECT channel, user FROM members ORDER BY user"
        ).fetchall() == [("C1", "U1"), ("C1", "U2")]
    finally:
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


def test_auto_engage_decision_uses_responses_helper_contract(
    archivebot_module,
    monkeypatch,
):
    bot = archivebot_module
    calls = []

    def fake_generate(client, **kwargs):
        calls.append((client, kwargs))
        return '{"engage": true, "reply": "bot: risposta"}'

    monkeypatch.setattr(bot, "generate_text_response", fake_generate)
    client = object()

    engage, reply = bot._decide_engage(
        [{"user": "Alice", "user_id": "U1", "text": "ciao", "ts": "1"}],
        client,
    )

    assert engage is True
    assert reply == "risposta"
    assert calls[0][0] is client
    assert calls[0][1]["model"] == bot.AUTO_ENGAGE_DECISION_MODEL
    assert calls[0][1]["reasoning_effort"] == "low"
    assert calls[0][1]["max_output_tokens"] == 600
    assert calls[0][1]["text_format"] == {"type": "json_object"}


def test_auto_clown_decision_uses_responses_helper_contract(
    archivebot_module,
    monkeypatch,
):
    bot = archivebot_module
    calls = []

    def fake_generate(client, **kwargs):
        calls.append((client, kwargs))
        return '{"clown_user": "Alice", "reason": "autogol"}'

    monkeypatch.setattr(bot, "generate_text_response", fake_generate)
    client = object()

    user, reason = bot._decide_clown(
        [{"user": "Alice", "user_id": "U1", "text": "ciao", "ts": "1"}],
        client,
    )

    assert (user, reason) == ("Alice", "autogol")
    assert calls[0][0] is client
    assert calls[0][1]["model"] == bot.AUTO_ENGAGE_DECISION_MODEL
    assert calls[0][1]["reasoning_effort"] == "low"
    assert calls[0][1]["max_output_tokens"] == 300
    assert calls[0][1]["text_format"] == {"type": "json_object"}


def test_direct_mention_keeps_responses_agent_request_and_rate_footer(
    archivebot_module,
    monkeypatch,
):
    bot = archivebot_module
    captured = {}
    monkeypatch.setenv("OPENAI_API_KEY", "dummy")
    monkeypatch.setattr(
        bot,
        "get_channel_messages",
        lambda *_args, **_kwargs: [
            {"user": "Alice", "user_id": "U1", "text": "contesto", "ts": "100.1"}
        ],
    )
    monkeypatch.setattr(bot, "OpenAI", lambda **_kwargs: object())

    def fake_run_archive_agent(_client, **kwargs):
        captured.update(kwargs)
        return "risposta grounded"

    monkeypatch.setattr(bot, "run_archive_agent", fake_run_archive_agent)
    replies = []

    bot.handle_app_mention(
        {
            "user": "U1",
            "channel": "C1",
            "ts": "100.2",
            "text": f"<@{bot.app._bot_user_id}> cosa abbiamo deciso?",
        },
        lambda text, **kwargs: replies.append((text, kwargs)),
    )

    assert captured["question"] == "cosa abbiamo deciso?"
    assert captured["search_engine"].requester_user_id == "U1"
    assert captured["search_engine"].before_timestamp == "100.2"
    assert captured["model"] == bot.AI_RESPONSE_MODEL
    assert captured["reasoning_effort"] == bot.AI_REASONING_EFFORT
    assert replies[0][1]["thread_ts"] == "100.2"
    assert "risposta grounded" in replies[0][0]
    assert "1/2 al minuto, 1/10 all'ora" in replies[0][0]


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


def test_engage_passes_live_trigger_when_archive_optout_removes_all_context(
    archivebot_module,
    monkeypatch,
):
    bot = archivebot_module
    _seed_engaged_thread(bot)
    conn = sqlite3.connect(bot.database_path)
    conn.execute("INSERT INTO optout(user, timestamp) VALUES ('U1', 'now')")
    conn.commit()
    conn.close()
    calls = []
    monkeypatch.setenv("OPENAI_API_KEY", "dummy")
    monkeypatch.setattr(bot, "get_thread_messages", lambda *_args, **_kwargs: [])
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
        "text": "questa richiesta non deve essere archiviata",
    }

    bot.maybe_reply_to_engaged_thread(message, lambda *_args, **_kwargs: None)

    assert len(calls) == 1
    assert calls[0][1]["trigger_message"] is message
    conn = sqlite3.connect(bot.database_path)
    try:
        assert conn.execute(
            "SELECT user_id FROM ai_requests"
        ).fetchall() == [("U1",)]
        assert conn.execute(
            "SELECT last_reply_ts FROM engaged_threads "
            "WHERE thread_ts = '100.1' AND channel = 'C1'"
        ).fetchone() == ("100.2",)
    finally:
        conn.close()


def test_auto_reply_uses_trigger_instead_of_latest_visible_context_author(
    archivebot_module,
    monkeypatch,
):
    bot = archivebot_module
    captured = {}
    conn = sqlite3.connect(bot.database_path)
    conn.execute("INSERT INTO users(name, id, avatar) VALUES ('Alice', 'U1', '')")
    conn.execute("INSERT INTO users(name, id, avatar) VALUES ('Bob', 'U2', '')")
    conn.execute("INSERT INTO channels(name, id, is_private) VALUES ('dev', 'C1', 0)")
    conn.commit()
    conn.close()

    def fake_run_archive_agent(_client, **kwargs):
        captured.update(kwargs)
        return "risposta"

    monkeypatch.setattr(bot, "run_archive_agent", fake_run_archive_agent)
    replies = []
    trigger = {
        "user": "U1",
        "text": "domanda corrente",
        "ts": "100.3",
    }

    bot._auto_reply_in_thread(
        "C1",
        "100.1",
        [{"user": "Bob", "user_id": "U2", "text": "testo precedente", "ts": "100.2"}],
        object(),
        lambda *args, **kwargs: replies.append((args, kwargs)),
        trigger_message=trigger,
    )

    assert captured["question"] == "domanda corrente"
    assert captured["search_engine"].requester_user_id == "U1"
    assert captured["search_engine"].before_timestamp == "100.3"
    assert replies


def test_engage_ai_optout_is_explicit_and_consumes_no_claim_or_quota(
    archivebot_module,
    monkeypatch,
):
    bot = archivebot_module
    _seed_engaged_thread(bot)
    conn = sqlite3.connect(bot.database_path)
    conn.execute("INSERT INTO optout_ai(user, timestamp) VALUES ('U1', 'now')")
    conn.commit()
    conn.close()
    monkeypatch.setenv("OPENAI_API_KEY", "dummy")
    generated = []
    monkeypatch.setattr(
        bot,
        "_auto_reply_in_thread",
        lambda *_args, **_kwargs: generated.append(True),
    )
    replies = []

    bot.maybe_reply_to_engaged_thread(
        {
            "user": "U1",
            "channel": "C1",
            "thread_ts": "100.1",
            "ts": "100.2",
            "text": "non usare le funzioni AI",
        },
        lambda text, **kwargs: replies.append((text, kwargs)),
    )

    assert generated == []
    assert len(replies) == 1
    assert "opt-out AI" in replies[0][0]
    assert replies[0][1]["thread_ts"] == "100.1"
    conn = sqlite3.connect(bot.database_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM ai_requests").fetchone() == (0,)
        assert conn.execute(
            "SELECT last_reply_ts FROM engaged_threads "
            "WHERE thread_ts = '100.1' AND channel = 'C1'"
        ).fetchone() == ("100.1",)
    finally:
        conn.close()


def test_archive_optout_redacts_storage_but_preserves_live_engage_trigger(
    archivebot_module,
    monkeypatch,
):
    bot = archivebot_module
    conn = sqlite3.connect(bot.database_path)
    conn.execute("INSERT INTO users(name, id, avatar) VALUES ('Alice', 'U1', '')")
    conn.execute("INSERT INTO channels(name, id, is_private) VALUES ('dev', 'C1', 0)")
    conn.execute("INSERT INTO optout(user, timestamp) VALUES ('U1', 'now')")
    conn.commit()
    conn.close()
    routed = []
    routed_links = []
    monkeypatch.setattr(
        bot,
        "route_link_message_event",
        lambda *_args, **_kwargs: routed_links.append(True),
    )
    monkeypatch.setattr(
        bot.app.client,
        "chat_getPermalink",
        lambda **_kwargs: {"permalink": "https://slack.example/private-pointer"},
        raising=False,
    )
    monkeypatch.setattr(bot, "create_embeddings", lambda _text: None)
    monkeypatch.setattr(
        bot,
        "maybe_reply_to_engaged_thread",
        lambda message, say: routed.append(message.copy()),
    )
    monkeypatch.setattr(bot, "post_xcancel_alternatives", lambda *_args, **_kwargs: None)

    bot.handle_message(
        {
            "user": "U1",
            "channel": "C1",
            "channel_type": "channel",
            "thread_ts": "100.1",
            "ts": "100.2",
            "text": "contenuto live",
        },
        lambda *_args, **_kwargs: None,
    )

    assert routed == [
        {
            "user": "U1",
            "channel": "C1",
            "channel_type": "channel",
            "thread_ts": "100.1",
            "ts": "100.2",
            "text": "contenuto live",
        }
    ]
    assert routed_links == []
    conn = sqlite3.connect(bot.database_path)
    try:
        assert conn.execute(
            "SELECT message, user, permalink FROM messages WHERE timestamp = '100.2'"
        ).fetchone() == (
            "User opted out of archiving. This message has been deleted",
            "USLACKBOT",
            "",
        )
    finally:
        conn.close()


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
