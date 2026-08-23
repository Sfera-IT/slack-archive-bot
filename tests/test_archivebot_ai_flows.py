import importlib
import os
import sqlite3
import sys
from types import SimpleNamespace

import pytest
from slack_bolt import App as RealSlackApp
from slack_bolt.authorization import AuthorizeResult
from slack_bolt.request import BoltRequest


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
    def __init__(self, **kwargs):
        self.init_kwargs = kwargs
        self.client = FakeSlackClient()

    def _decorator(self, *_args, **_kwargs):
        return lambda function: function

    event = _decorator
    message = _decorator
    action = _decorator
    command = _decorator

    def error(self, function):
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
    module._initialize_bot_identity()
    module.database_path = str(tmp_path / "archive.sqlite")
    conn, cursor = module.db_connect(module.database_path)
    module.migrate_db(conn, cursor)
    conn.close()
    return module


def test_slack_token_is_verified_lazily_for_resilient_startup(archivebot_module):
    assert archivebot_module.app.init_kwargs["token_verification_enabled"] is False


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


def test_admin_optout_uses_full_transactional_privacy_purge(archivebot_module):
    bot = archivebot_module
    replies = []
    conn, cursor = bot.db_connect(bot.database_path)
    try:
        cursor.executemany(
            "INSERT INTO users(name, id, avatar) VALUES (?, ?, '')",
            [("target", "UTARGET"), ("Slackbot", "USLACKBOT")],
        )
        cursor.execute(
            "INSERT INTO channels(name, id, is_private) VALUES ('public', 'C1', 0)"
        )
        cursor.execute(
            """
            INSERT INTO messages
            (message, user, channel, timestamp, permalink, thread_ts, embeddings)
            VALUES ('private original text', 'UTARGET', 'C1', '100.1',
                    'https://slack.test/a', '100.1', X'0102')
            """
        )
        cursor.execute(
            """
            INSERT INTO message_links
            (channel, message_timestamp, thread_ts, normalized_url, original_url,
             permalink, posted_at, deterministic_checked_at, duplicate_checked_at)
            VALUES ('C1', '100.1', '100.1', 'https://example.test/',
                    'https://example.test/', 'https://slack.test/a', 100, 100, 100)
            """
        )
        cursor.execute(
            """
            INSERT INTO digests(timestamp, period, digest, posts, podcast_content)
            VALUES ('2026-01-01', 'period', 'digest', 'private original text', 'podcast')
            """
        )
        conn.commit()

        bot.handle_query(
            {"text": "/optout UTARGET", "user": bot.ADMIN_USERS[0]},
            cursor,
            replies.append,
        )

        message = cursor.execute(
            "SELECT message, user, permalink, embeddings FROM messages "
            "WHERE channel = 'C1' AND timestamp = '100.1'"
        ).fetchone()
        assert message == (
            "User opted out of archiving. This message has been deleted",
            "USLACKBOT",
            "",
            None,
        )
        assert cursor.execute("SELECT COUNT(*) FROM message_links").fetchone()[0] == 0
        assert cursor.execute("SELECT COUNT(*) FROM digests").fetchone()[0] == 0
        assert cursor.execute(
            "SELECT COUNT(*) FROM optout WHERE user = 'UTARGET'"
        ).fetchone()[0] == 1
        assert "Opt-out eseguito" in replies[-1]
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
    cursor.execute(
        "INSERT INTO channels(name, id, is_private) VALUES ('public', 'C1', 0)"
    )
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


def test_private_ai_debug_only_goes_to_admins_in_that_channel(archivebot_module):
    bot = archivebot_module
    member_admin, outsider_admin = bot.ADMIN_USERS[:2]
    conn, cursor = bot.db_connect(bot.database_path)
    try:
        bot.set_ai_debug_enabled(cursor, member_admin, True)
        bot.set_ai_debug_enabled(cursor, outsider_admin, True)
        cursor.execute(
            "INSERT INTO channels(name, id, is_private) VALUES ('private', 'CPRIVATE', 1)"
        )
        cursor.execute(
            "INSERT INTO members(channel, user) VALUES ('CPRIVATE', ?)",
            (member_admin,),
        )
        conn.commit()
    finally:
        conn.close()

    bot._report_ai_error(
        RuntimeError("private failure"),
        event={"user": "U1", "channel": "CPRIVATE", "ts": "100.1"},
        source="engaged_thread",
    )

    assert [message["channel"] for message in bot.app.client.posted] == [member_admin]


def test_users_and_channels_refresh_all_pages_and_replace_stale_members(
    archivebot_module, monkeypatch
):
    bot = archivebot_module

    def profile(user_id):
        return {
            "id": user_id,
            "deleted": False,
            "profile": {
                "display_name": user_id.lower(),
                "real_name": user_id,
                "image_72": "",
                "email": "",
            },
        }

    user_pages = {
        None: {"members": [profile("U1")], "response_metadata": {"next_cursor": "p2"}},
        "p2": {"members": [profile("U2")], "response_metadata": {"next_cursor": ""}},
    }
    monkeypatch.setattr(
        bot.app.client,
        "users_list",
        lambda **kwargs: user_pages[kwargs.get("cursor")],
        raising=False,
    )
    monkeypatch.setattr(
        bot.app.client,
        "conversations_list",
        lambda **_kwargs: {
            "channels": [{"id": "CPRIVATE", "is_member": True}],
            "response_metadata": {"next_cursor": ""},
        },
        raising=False,
    )
    monkeypatch.setattr(
        bot,
        "get_channel_info",
        lambda _channel: (
            "CPRIVATE",
            "private",
            True,
            [("CPRIVATE", "U2"), ("CPRIVATE", "U2")],
        ),
    )

    conn, cursor = bot.db_connect(bot.database_path)
    try:
        cursor.execute(
            "INSERT INTO channels(name, id, is_private) VALUES ('private', 'CPRIVATE', 1)"
        )
        cursor.execute("INSERT INTO members(channel, user) VALUES ('CPRIVATE', 'USTALE')")
        conn.commit()
        bot.update_users(conn, cursor)
        bot.update_channels(conn, cursor)

        assert {row[0] for row in cursor.execute("SELECT id FROM users")} >= {"U1", "U2"}
        assert cursor.execute(
            "SELECT channel, user FROM members ORDER BY user"
        ).fetchall() == [("CPRIVATE", "U2")]
    finally:
        conn.close()


def test_configured_bot_identity_must_match_slack(archivebot_module):
    bot = archivebot_module
    bot.app._bot_user_id = "UCONFIGURED"
    with pytest.raises(RuntimeError, match="does not match"):
        bot._initialize_bot_identity()
    bot.app._bot_user_id = "UBOT"


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
        assert "Riprova tra" in message
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


def _seed_user(bot, user_id="U1", name="Alice"):
    conn = sqlite3.connect(bot.database_path)
    conn.execute(
        "INSERT INTO users(name, id, avatar) VALUES (?, ?, '')",
        (name, user_id),
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
        lambda *args, **kwargs: calls.append((args, kwargs)) or True,
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


def test_real_bolt_message_callback_reaches_engage_before_legacy_pipeline(
    archivebot_module,
    monkeypatch,
):
    bot = archivebot_module
    _seed_engaged_thread(bot)
    _seed_user(bot)
    monkeypatch.setenv("OPENAI_API_KEY", "dummy")
    monkeypatch.setattr(
        bot,
        "get_thread_messages",
        lambda *_args, **_kwargs: [
            {"user": "Old", "user_id": "U2", "text": "prima", "ts": "100.1"},
            {"user": "Alice", "user_id": "U1", "text": "adesso", "ts": "100.2"},
            {"user": "Future", "user_id": "U3", "text": "dopo", "ts": "100.3"},
        ],
    )
    monkeypatch.setattr(bot, "OpenAI", lambda **_kwargs: object())
    monkeypatch.setattr(bot, "create_embeddings", lambda _text: b"embedding")
    calls = []

    def capture_reply(*args, **kwargs):
        calls.append((args, kwargs))
        return True

    monkeypatch.setattr(bot, "_auto_reply_in_thread", capture_reply)

    real_app = RealSlackApp(
        signing_secret="test",
        process_before_response=True,
        authorize=lambda **_kwargs: AuthorizeResult(
            enterprise_id=None,
            team_id="T1",
            bot_token="xoxb-test",
            bot_user_id="UBOT",
        ),
    )
    real_app.message("")(bot.handle_message_default)
    event = {
        "type": "message",
        "text": "adesso",
        "user": "U1",
        "channel": "C1",
        "channel_type": "channel",
        "ts": "100.2",
        "thread_ts": "100.1",
        "team": "T1",
    }
    request = BoltRequest(
        body={"type": "event_callback", "team_id": "T1", "event": event},
        mode="socket_mode",
    )

    response = real_app.dispatch(request)

    assert response.status == 200
    assert len(calls) == 1
    assert [item["ts"] for item in calls[0][0][2]] == ["100.1", "100.2"]
    trigger = calls[0][1]["trigger"]
    assert trigger.user_id == "U1"
    assert trigger.text == "adesso"
    assert trigger.message_ts == "100.2"


def test_engage_reply_survives_legacy_archiving_failure_and_reports_it(
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
    sequence = []
    monkeypatch.setattr(
        bot,
        "_auto_reply_in_thread",
        lambda *_args, **_kwargs: sequence.append("engage_reply") or True,
    )
    monkeypatch.setattr(
        bot,
        "create_embeddings",
        lambda _text: (_ for _ in ()).throw(RuntimeError("embedding exploded")),
    )
    monkeypatch.setattr(
        bot,
        "_report_ai_error",
        lambda _error, **kwargs: sequence.append(kwargs["source"]),
    )

    bot.handle_message(
        {
            "type": "message",
            "text": "ciao",
            "user": "U1",
            "channel": "C1",
            "channel_type": "channel",
            "ts": "100.2",
            "thread_ts": "100.1",
        },
        lambda *_args, **_kwargs: None,
    )

    assert sequence == ["engage_reply", "message_processing"]


def test_engage_agent_always_uses_immutable_trigger_as_requester(
    archivebot_module,
    monkeypatch,
):
    bot = archivebot_module
    captured = {}

    class CapturingSearchEngine:
        def __init__(
            self,
            _conn,
            *,
            requester_user_id,
            current_channel_id,
            before_timestamp,
            evidence,
        ):
            captured.update(
                requester=requester_user_id,
                channel=current_channel_id,
                before=before_timestamp,
            )
            self.evidence = evidence

    def capture_agent(_client, **kwargs):
        captured["question"] = kwargs["question"]
        return "risposta"

    monkeypatch.setattr(bot, "ArchiveSearchEngine", CapturingSearchEngine)
    monkeypatch.setattr(bot, "run_archive_agent", capture_agent)
    trigger = bot.EngagedThreadTrigger(
        user_id="UTRIGGER",
        text="domanda corrente",
        message_ts="100.2",
        channel="C1",
        thread_ts="100.1",
    )
    sent = []

    replied = bot._auto_reply_in_thread(
        "C1",
        "100.1",
        [
            {"user": "Other", "user_id": "UOTHER", "text": "testo altrui", "ts": "100.1"}
        ],
        object(),
        lambda **kwargs: sent.append(kwargs),
        trigger=trigger,
    )

    assert replied is True
    assert captured == {
        "requester": "UTRIGGER",
        "channel": "C1",
        "before": "100.2",
        "question": "domanda corrente",
    }
    assert sent[0]["thread_ts"] == "100.1"


def test_archive_agent_kill_switch_uses_current_context_only(
    archivebot_module,
    monkeypatch,
):
    bot = archivebot_module
    captured = {}
    monkeypatch.setattr(bot, "ARCHIVE_AGENT_ENABLED", False)
    monkeypatch.setattr(
        bot,
        "run_archive_agent",
        lambda *_args, **_kwargs: pytest.fail("archive tools must stay disabled"),
    )

    def generate(_client, **kwargs):
        captured.update(kwargs)
        return "risposta safe mode"

    monkeypatch.setattr(bot, "generate_text_response", generate)
    sent = []
    assert bot._auto_reply_in_thread(
        "C1",
        "100.1",
        [{"user": "Alice", "user_id": "U1", "text": "domanda", "ts": "100.2"}],
        object(),
        lambda **kwargs: sent.append(kwargs),
    )

    assert "thread corrente" in captured["instructions"]
    assert "Domanda: domanda" in captured["input_text"]
    assert sent[0]["text"].startswith("risposta safe mode")


def test_engage_skips_third_party_bots_and_opted_out_trigger(
    archivebot_module,
    monkeypatch,
    caplog,
):
    bot = archivebot_module
    _seed_engaged_thread(bot)
    caplog.set_level("DEBUG")
    fetched = []
    monkeypatch.setattr(
        bot,
        "get_thread_messages",
        lambda *_args, **_kwargs: fetched.append(True),
    )

    bot.maybe_reply_to_engaged_thread(
        {
            "user": "UBOT2",
            "bot_id": "B2",
            "channel": "C1",
            "thread_ts": "100.1",
            "ts": "100.2",
            "text": "automazione",
        },
        lambda *_args, **_kwargs: None,
    )
    assert fetched == []
    assert "gate=automated_message" in caplog.text

    conn = sqlite3.connect(bot.database_path)
    conn.execute("INSERT INTO optout_ai(user, timestamp) VALUES ('U1', 'now')")
    conn.commit()
    conn.close()
    monkeypatch.setenv("OPENAI_API_KEY", "dummy")
    bot.maybe_reply_to_engaged_thread(
        {
            "user": "U1",
            "channel": "C1",
            "thread_ts": "100.1",
            "ts": "100.2",
            "text": "testo privato",
        },
        lambda *_args, **_kwargs: None,
    )
    assert fetched == []
    assert "gate=trigger_opted_out" in caplog.text


def test_engage_mention_preserves_link_routing_before_early_return(
    archivebot_module,
    monkeypatch,
):
    bot = archivebot_module
    sequence = []
    monkeypatch.setattr(
        bot,
        "_route_message_links_safely",
        lambda *_args, **_kwargs: sequence.append("links"),
    )
    monkeypatch.setattr(
        bot,
        "_maybe_handle_engage_command",
        lambda *_args, **_kwargs: sequence.append("engage") or True,
    )

    bot.handle_message(
        {
            "type": "message",
            "text": "<@UBOT> /engage",
            "user": "U1",
            "channel": "C1",
            "channel_type": "channel",
            "ts": "100.2",
            "thread_ts": "100.1",
        },
        lambda *_args, **_kwargs: None,
    )

    assert sequence == ["links", "engage"]


def test_engage_kill_switch_disables_commands_and_existing_threads(
    archivebot_module,
    monkeypatch,
):
    bot = archivebot_module
    monkeypatch.setattr(bot, "THREAD_ENGAGEMENT_ENABLED", False)
    replies = []

    assert bot._maybe_handle_engage_command(
        {
            "text": "<@UBOT> /engage",
            "user": "U1",
            "channel": "C1",
            "ts": "100.1",
        },
        lambda text, **kwargs: replies.append((text, kwargs)),
    )
    bot.maybe_reply_to_engaged_thread(
        {
            "text": "nuovo messaggio",
            "user": "U1",
            "channel": "C1",
            "ts": "100.2",
            "thread_ts": "100.1",
        },
        lambda *_args, **_kwargs: replies.append(("unexpected", {})),
    )

    assert len(replies) == 1
    assert "temporaneamente disabilitato" in replies[0][0]


def test_global_bolt_error_handler_routes_sanitized_diagnostics(
    archivebot_module,
    monkeypatch,
):
    bot = archivebot_module
    reported = []
    monkeypatch.setattr(
        bot,
        "_report_ai_error",
        lambda error, **kwargs: reported.append((error, kwargs)),
    )
    fallback_logs = []

    bot.handle_bolt_error(
        RuntimeError("listener failure"),
        {
            "event": {
                "user": "U1",
                "channel": "C1",
                "ts": "100.2",
                "text": "non deve finire nel report di routing",
            }
        },
        SimpleNamespace(exception=lambda *args, **kwargs: fallback_logs.append(args)),
    )

    assert len(reported) == 1
    assert reported[0][1]["source"] == "bolt_unhandled"
    assert reported[0][1]["event"]["user"] == "U1"
    assert fallback_logs == []


def test_message_edit_from_opted_out_user_cannot_restore_content_or_links(
    archivebot_module,
    monkeypatch,
):
    bot = archivebot_module
    conn = sqlite3.connect(bot.database_path)
    conn.execute("INSERT INTO optout(user, timestamp) VALUES ('U1', 'now')")
    conn.execute(
        """
        INSERT INTO messages(message, user, channel, timestamp, permalink, thread_ts, embeddings)
        VALUES ('redacted', 'USLACKBOT', 'C1', '100.2', '', '100.1', NULL)
        """
    )
    conn.commit()
    conn.close()
    attempted = []
    monkeypatch.setattr(
        bot,
        "create_embeddings",
        lambda _text: attempted.append("embedding"),
    )
    monkeypatch.setattr(
        bot,
        "check_and_store_links",
        lambda *_args, **_kwargs: attempted.append("links"),
    )

    bot.handle_message_changed(
        {
            "channel": "C1",
            "message": {
                "user": "U1",
                "text": "segreto https://example.com/private",
                "ts": "100.2",
            },
        },
        lambda *_args, **_kwargs: None,
    )

    conn = sqlite3.connect(bot.database_path)
    row = conn.execute(
        "SELECT message, user, permalink, embeddings FROM messages "
        "WHERE channel = 'C1' AND timestamp = '100.2'"
    ).fetchone()
    conn.close()
    assert row == (
        "User opted out of archiving. This message has been deleted",
        "USLACKBOT",
        "",
        None,
    )
    assert attempted == []


def test_new_message_from_opted_out_user_never_processes_content(
    archivebot_module,
    monkeypatch,
):
    bot = archivebot_module
    conn = sqlite3.connect(bot.database_path)
    conn.execute("INSERT INTO optout(user, timestamp) VALUES ('U1', 'now')")
    conn.commit()
    conn.close()
    attempted = []
    monkeypatch.setattr(
        bot,
        "route_link_message_event",
        lambda *_args, **_kwargs: attempted.append("links"),
    )
    monkeypatch.setattr(
        bot,
        "create_embeddings",
        lambda _text: attempted.append("embedding"),
    )

    bot.handle_message(
        {
            "type": "message",
            "text": "segreto https://example.com/private",
            "user": "U1",
            "channel": "C1",
            "channel_type": "channel",
            "ts": "101.2",
        },
        lambda *_args, **_kwargs: None,
    )

    conn = sqlite3.connect(bot.database_path)
    row = conn.execute(
        "SELECT message, user, permalink, embeddings FROM messages "
        "WHERE channel = 'C1' AND timestamp = '101.2'"
    ).fetchone()
    conn.close()
    assert row == (
        "User opted out of archiving. This message has been deleted",
        "USLACKBOT",
        "",
        None,
    )
    assert attempted == []


def test_ai_optout_archives_text_but_skips_all_derived_processing(
    archivebot_module,
    monkeypatch,
):
    bot = archivebot_module
    conn = sqlite3.connect(bot.database_path)
    conn.execute("INSERT INTO optout_ai(user, timestamp) VALUES ('U1', 'now')")
    conn.execute("INSERT INTO users(name, id, avatar) VALUES ('Alice', 'U1', '')")
    conn.commit()
    conn.close()
    attempted = []
    monkeypatch.setattr(
        bot,
        "route_link_message_event",
        lambda *_args, **_kwargs: attempted.append("links"),
    )
    monkeypatch.setattr(
        bot,
        "create_embeddings",
        lambda _text: attempted.append("embedding"),
    )
    monkeypatch.setattr(
        bot,
        "post_xcancel_alternatives",
        lambda *_args, **_kwargs: attempted.append("xcancel"),
    )
    monkeypatch.setattr(
        bot.app.client,
        "chat_getPermalink",
        lambda **_kwargs: {"permalink": "https://slack.test/message"},
        raising=False,
    )

    bot.handle_message(
        {
            "type": "message",
            "text": "testo https://x.com/example/status/1",
            "user": "U1",
            "channel": "C1",
            "channel_type": "channel",
            "ts": "102.2",
        },
        lambda *_args, **_kwargs: None,
    )

    conn = sqlite3.connect(bot.database_path)
    row = conn.execute(
        "SELECT message, user, embeddings FROM messages "
        "WHERE channel = 'C1' AND timestamp = '102.2'"
    ).fetchone()
    conn.close()
    assert row == ("testo https://x.com/example/status/1", "U1", None)
    assert attempted == []


def test_ai_optout_edit_removes_derived_links_and_skips_regeneration(
    archivebot_module,
    monkeypatch,
):
    bot = archivebot_module
    conn = sqlite3.connect(bot.database_path)
    conn.execute("INSERT INTO optout_ai(user, timestamp) VALUES ('U1', 'now')")
    conn.execute(
        """
        INSERT INTO messages(message, user, channel, timestamp, permalink, thread_ts, embeddings)
        VALUES ('old', 'U1', 'C1', '103.2', '', '103.1', X'0102')
        """
    )
    conn.execute(
        """
        INSERT INTO message_links
        (channel, message_timestamp, thread_ts, normalized_url, original_url,
         permalink, posted_at, deterministic_checked_at, duplicate_checked_at)
        VALUES ('C1', '103.2', '103.1', 'https://old.example/',
                'https://old.example/', '', 103, 103, 103)
        """
    )
    conn.commit()
    conn.close()
    attempted = []
    monkeypatch.setattr(
        bot,
        "create_embeddings",
        lambda _text: attempted.append("embedding"),
    )
    monkeypatch.setattr(
        bot,
        "check_and_store_links",
        lambda *_args, **_kwargs: attempted.append("links"),
    )
    monkeypatch.setattr(
        bot,
        "sync_xcancel_alternatives_for_message",
        lambda *_args, **_kwargs: attempted.append("xcancel"),
    )

    bot.handle_message_changed(
        {
            "channel": "C1",
            "message": {
                "user": "U1",
                "text": "new https://new.example/",
                "ts": "103.2",
            },
        },
        lambda *_args, **_kwargs: None,
    )

    conn = sqlite3.connect(bot.database_path)
    assert conn.execute(
        "SELECT message, embeddings FROM messages "
        "WHERE channel = 'C1' AND timestamp = '103.2'"
    ).fetchone() == ("new https://new.example/", None)
    assert conn.execute(
        "SELECT COUNT(*) FROM message_links WHERE channel = 'C1' AND message_timestamp = '103.2'"
    ).fetchone()[0] == 0
    conn.close()
    assert attempted == []


def test_user_change_cannot_repopulate_opted_out_profile(archivebot_module):
    bot = archivebot_module
    conn = sqlite3.connect(bot.database_path)
    conn.execute(
        "INSERT INTO users(name, id, avatar) VALUES ('Opted-out user', 'U1', '')"
    )
    conn.execute("INSERT INTO optout(user, timestamp) VALUES ('U1', 'now')")
    conn.commit()
    conn.close()

    bot.handle_user_change(
        {
            "user": {
                "id": "U1",
                "profile": {"display_name": "Secret", "real_name": "Secret Real"},
            }
        }
    )

    conn = sqlite3.connect(bot.database_path)
    assert conn.execute("SELECT name FROM users WHERE id = 'U1'").fetchone() == (
        "Opted-out user",
    )
    conn.close()


def test_engaged_block_sections_preserve_long_sources_and_rate_footer(
    archivebot_module,
):
    bot = archivebot_module
    reply = (
        "A" * 3500
        + "\n\n*Fonti*\n"
        + "• <https://sferait-ws.slack.com/archives/C0BSUCGHU8G/p1|Slack>"
        + " · <https://sferaarchive-client.vercel.app/?channel=C0BSUCGHU8G&thread_ts=1787395457.104349&message_ts=1787395460.204349|SferaArchive>"
        + "\n\n📊 Rate limit per user: 1/2 al minuto, 1/10 all'ora"
    )

    blocks = bot._engaged_stop_button_blocks(reply, "C1", "100.1")
    sections = [block["text"]["text"] for block in blocks if block["type"] == "section"]
    rendered = "\n".join(sections)

    assert len(sections) >= 2
    assert all(len(section) <= 3000 for section in sections)
    assert "SferaArchive" in rendered
    assert "Rate limit per user" in rendered
