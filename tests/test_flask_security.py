import datetime
import hashlib
import hmac
import importlib
import os
import sqlite3
import sys
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import jwt
import pytest


ROOT_DIR = os.path.dirname(os.path.dirname(__file__))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from utils import migrate_db


class FakeSlackClient:
    def chat_postMessage(self, **_kwargs):
        return {"ok": True}


class FakeSlackRequestHandler:
    def __init__(self, _app):
        pass

    def handle(self, _request):
        return "ok", 200


@pytest.fixture
def web_module(monkeypatch, tmp_path):
    database_path = tmp_path / "archive.sqlite"
    monkeypatch.setenv("DB_PATH", str(database_path))
    monkeypatch.delenv("ARCHIVE_BOT_DATABASE_PATH", raising=False)
    monkeypatch.delenv("SLACK_BOT_USER_ID", raising=False)
    monkeypatch.setenv("SECRET_KEY", "test-secret-key-with-enough-entropy")
    monkeypatch.setenv("CLIENT_ID", "client-id")
    monkeypatch.setenv("CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("OAUTH_SCOPE", "channels:read")
    monkeypatch.setenv("EXPECTED_TEAM_ID", "TEXPECTED")
    monkeypatch.setenv("CLIENT_URL", "https://frontend.example/app")
    monkeypatch.setenv(
        "OAUTH_REDIRECT_URI", "https://archive.example/oauth_callback"
    )
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-server-side-only")
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "test-signing-secret")
    monkeypatch.setitem(
        sys.modules,
        "archivebot",
        SimpleNamespace(
            app=SimpleNamespace(client=FakeSlackClient()),
            update_users=lambda *_args, **_kwargs: None,
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "slack_bolt.adapter.flask",
        SimpleNamespace(SlackRequestHandler=FakeSlackRequestHandler),
    )
    monkeypatch.setitem(
        sys.modules,
        "sentence_transformers",
        SimpleNamespace(SentenceTransformer=object),
    )
    sys.modules.pop("flask_app", None)

    conn = sqlite3.connect(database_path)
    cursor = conn.cursor()
    migrate_db(conn, cursor)
    cursor.executemany(
        """
        INSERT INTO users(name, id, avatar, real_name, display_name, email, is_deleted)
        VALUES (?, ?, ?, ?, ?, ?, 0)
        """,
        [
            ("member", "U1", "a1", "Member One", "member", "u1@example.test"),
            ("outsider", "U2", "a2", "User Two", "outsider", "u2@example.test"),
            (
                "admin",
                "U011PQ7RHRT",
                "aa",
                "Admin User",
                "admin",
                "admin@example.test",
            ),
        ],
    )
    cursor.executemany(
        "INSERT INTO channels(name, id, is_private) VALUES (?, ?, ?)",
        [("public", "CPUBLIC", 0), ("private", "CPRIVATE", 1)],
    )
    cursor.execute("INSERT INTO members(channel, user) VALUES ('CPRIVATE', 'U1')")
    cursor.executemany(
        """
        INSERT INTO messages
        (message, user, channel, timestamp, permalink, thread_ts, embeddings)
        VALUES (?, ?, ?, ?, ?, ?, NULL)
        """,
        [
            (
                "public needle",
                "U1",
                "CPUBLIC",
                "100.1",
                "https://slack.test/public",
                "100.1",
            ),
            (
                "private needle",
                "U1",
                "CPRIVATE",
                "200.1",
                "https://slack.test/private",
                "200.1",
            ),
            (
                "private reply",
                "U1",
                "CPRIVATE",
                "200.2",
                "https://slack.test/private-reply",
                "200.1",
            ),
        ],
    )
    conn.commit()
    conn.close()

    module = importlib.import_module("flask_app")
    module.flask_app.config.update(TESTING=True)
    try:
        yield module
    finally:
        sys.modules.pop("flask_app", None)


def _auth_headers(module, user_id, **extra_claims):
    payload = {
        "user_id": user_id,
        "exp": datetime.datetime.now(datetime.timezone.utc)
        + datetime.timedelta(minutes=10),
        **extra_claims,
    }
    token = jwt.encode(payload, module.flask_app.secret_key, algorithm="HS256")
    return {"Authorization": f"Bearer {token}"}


class FakeOAuthResponse:
    def __init__(self, *, team_id="TEXPECTED"):
        self.team_id = team_id

    def raise_for_status(self):
        return None

    def json(self):
        return {
            "ok": True,
            "access_token": "xoxb-must-never-leave-the-server",
            "team": {"id": self.team_id},
            "authed_user": {"id": "U1"},
        }


def test_oauth_state_is_one_time_and_jwt_contains_no_slack_token(
    web_module, monkeypatch
):
    module = web_module
    calls = []
    monkeypatch.setattr(
        module.requests,
        "post",
        lambda *args, **kwargs: calls.append((args, kwargs)) or FakeOAuthResponse(),
    )
    client = module.flask_app.test_client()

    login_response = client.get(
        "/login?return_to=https://frontend.example/app/history?view=all"
    )
    state = parse_qs(urlsplit(login_response.location).query)["state"][0]
    original_session_cookie = client.get_cookie("session")
    callback = client.get(f"/oauth_callback?code=abc&state={state}")

    assert callback.status_code == 302
    redirect_target = urlsplit(callback.location)
    assert redirect_target.netloc == "frontend.example"
    assert parse_qs(redirect_target.query) == {"view": ["all"]}
    jwt_token = parse_qs(redirect_target.fragment)["token"][0]
    decoded = jwt.decode(
        jwt_token, module.flask_app.secret_key, algorithms=["HS256"]
    )
    assert set(decoded) == {"user_id", "exp"}
    assert decoded["user_id"] == "U1"
    assert "xoxb-must-never-leave-the-server" not in callback.location
    assert callback.headers["Cache-Control"] == "no-store"
    assert calls[0][1]["timeout"] == 10

    # Even replaying the original signed Flask cookie cannot revive the
    # server-side nonce after its atomic consumption.
    client.set_cookie("session", original_session_cookie.value, domain="localhost")
    replay = client.get(f"/oauth_callback?code=abc&state={state}")
    assert replay.status_code == 400
    assert len(calls) == 1


def test_oauth_state_is_persisted_as_keyed_digest(web_module):
    module = web_module
    state = "high-entropy-oauth-state"
    module._store_oauth_state(state, "/")

    conn = sqlite3.connect(os.environ["DB_PATH"])
    stored = conn.execute("SELECT state_hash FROM oauth_states").fetchone()[0]
    conn.close()

    expected = hmac.new(
        str(module.flask_app.secret_key).encode("utf-8"),
        state.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    assert stored == expected
    assert stored != hashlib.sha256(state.encode("utf-8")).hexdigest()


def test_oauth_rejects_unexpected_workspace_and_external_return_to(
    web_module, monkeypatch
):
    module = web_module
    monkeypatch.setattr(
        module.requests,
        "post",
        lambda *_args, **_kwargs: FakeOAuthResponse(team_id="TOTHER"),
    )
    client = module.flask_app.test_client()
    login_response = client.get("/login?return_to=https://evil.example/steal")
    state = parse_qs(urlsplit(login_response.location).query)["state"][0]

    callback = client.get(f"/oauth_callback?code=abc&state={state}")
    assert callback.status_code == 403


def test_cors_is_allowlisted_and_security_headers_are_always_present(web_module):
    client = web_module.flask_app.test_client()
    allowed = client.get("/login", headers={"Origin": "https://frontend.example"})
    denied = client.get("/login", headers={"Origin": "https://evil.example"})

    assert allowed.headers["Access-Control-Allow-Origin"] == "https://frontend.example"
    assert "Access-Control-Allow-Origin" not in denied.headers
    assert allowed.headers["Strict-Transport-Security"].startswith("max-age=")
    assert allowed.headers["X-Content-Type-Options"] == "nosniff"
    assert allowed.headers["X-Frame-Options"] == "DENY"
    assert "frame-ancestors 'none'" in allowed.headers["Content-Security-Policy"]
    authenticated = client.get(
        "/whoami", headers=_auth_headers(web_module, "U1")
    )
    assert authenticated.headers["Cache-Control"] == "private, no-store"


def test_health_and_readiness_expose_revision_without_secrets(web_module):
    module = web_module
    client = module.flask_app.test_client()

    health = client.get("/healthz")
    assert health.status_code == 200
    assert health.get_json() == {
        "status": "degraded",
        "version": "2.2.0",
        "revision": "unknown",
    }
    assert health.headers["Cache-Control"] == "no-store"
    assert client.get("/readyz").status_code == 503

    module.app._bot_identity_verified = True
    ready = client.get("/readyz")
    assert ready.status_code == 200
    assert ready.get_json()["status"] == "ready"


def test_private_channel_acl_applies_to_messages_threads_and_search(web_module):
    module = web_module
    client = module.flask_app.test_client()
    outsider = _auth_headers(module, "U2")
    member = _auth_headers(module, "U1")

    assert client.get("/messages/CPUBLIC", headers=outsider).status_code == 200
    assert client.get("/messages/CPRIVATE", headers=outsider).status_code == 404
    assert (
        client.get("/thread/CPRIVATE/200.1", headers=outsider).status_code == 404
    )
    assert client.get("/thread/200.1", headers=outsider).status_code == 404
    assert client.get("/searchV2?query=private", headers=outsider).get_json() == []

    private_messages = client.get("/messages/CPRIVATE", headers=member)
    assert private_messages.status_code == 200
    exact_thread = client.get("/thread/CPRIVATE/200.1", headers=member)
    assert [row["message"] for row in exact_thread.get_json()] == [
        "private needle",
        "private reply",
    ]
    search = client.get("/searchV2?query=private", headers=member).get_json()
    assert {row["channel"] for row in search} == {"CPRIVATE"}


def test_search_rejects_oversized_or_pathological_filters(web_module):
    module = web_module
    client = module.flask_app.test_client()
    headers = _auth_headers(module, "U1")

    assert client.get(
        "/searchV2?query=" + "x" * (module.MAX_QUERY_CHARS + 1),
        headers=headers,
    ).status_code == 400
    assert client.get(
        "/searchV2?query=" + "+".join(["x"] * (module.MAX_QUERY_TERMS + 1)),
        headers=headers,
    ).status_code == 400
    assert client.get(
        "/searchV2?start_time=not-a-date", headers=headers
    ).status_code == 400


def test_users_omits_email_and_legacy_token_claims_are_rejected(web_module):
    module = web_module
    client = module.flask_app.test_client()
    response = client.get("/users", headers=_auth_headers(module, "U1"))

    assert response.status_code == 200
    assert all("email" not in user for user in response.get_json())
    legacy = client.get(
        "/whoami",
        headers=_auth_headers(module, "U1", slack_token="xoxb-legacy"),
    )
    assert legacy.status_code == 401


def test_digest_is_admin_only_and_chat_input_is_bounded(web_module):
    module = web_module
    client = module.flask_app.test_client()
    non_admin = _auth_headers(module, "U1")

    digest = client.post("/generate_digest", headers=non_admin, json={})
    assert digest.status_code == 403
    oversized = client.post(
        "/chat",
        headers=non_admin,
        json={"message": "x" * (module.MAX_CHAT_MESSAGE_CHARS + 1)},
    )
    assert oversized.status_code == 400


def test_chat_resolves_context_server_side_with_acl_and_ai_optout(
    web_module, monkeypatch
):
    module = web_module
    allowed_ts = "1700000002.100001"
    ai_optout_ts = "1700000003.100001"
    private_ts = "1700000004.100001"
    conn = sqlite3.connect(os.environ["DB_PATH"])
    conn.executemany(
        """
        INSERT INTO messages
        (message, user, channel, timestamp, permalink, thread_ts, embeddings)
        VALUES (?, ?, ?, ?, '', ?, NULL)
        """,
        [
            ("allowed server-side context", "U2", "CPUBLIC", allowed_ts, allowed_ts),
            ("blocked AI opt-out context", "U1", "CPUBLIC", ai_optout_ts, ai_optout_ts),
            ("blocked private context", "U1", "CPRIVATE", private_ts, private_ts),
        ],
    )
    conn.execute("INSERT INTO optout_ai(user, timestamp) VALUES ('U1', 'now')")
    conn.commit()
    conn.close()

    captured = {}
    monkeypatch.setattr(module, "OpenAI", lambda **_kwargs: object())

    def fake_generate(_client, **kwargs):
        captured.update(kwargs)
        return "safe response"

    monkeypatch.setattr(module, "generate_text_response", fake_generate)
    response = module.flask_app.test_client().post(
        "/chat",
        headers=_auth_headers(module, "U2"),
        json={
            "message": "summarize",
            "context_refs": [
                {"channel": "CPUBLIC", "timestamp": allowed_ts},
                {"channel": "CPUBLIC", "timestamp": allowed_ts},
                {"channel": "CPUBLIC", "timestamp": ai_optout_ts},
                {"channel": "CPRIVATE", "timestamp": private_ts},
            ],
        },
    )

    assert response.status_code == 200
    assert "allowed server-side context" in captured["input_text"]
    assert "blocked AI opt-out context" not in captured["input_text"]
    assert "blocked private context" not in captured["input_text"]
    assert response.get_json()["conversation"][-1]["message"] == "safe response"

    raw_context = module.flask_app.test_client().post(
        "/chat",
        headers=_auth_headers(module, "U2"),
        json={
            "message": "summarize",
            "context": [{"user_name": "leak", "message": "raw archive text"}],
        },
    )
    assert raw_context.status_code == 400


def test_runtime_configuration_fails_fast_on_weak_secrets(web_module, monkeypatch):
    module = web_module
    monkeypatch.setattr(module.flask_app, "secret_key", "too-short")
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        module._validate_runtime_configuration()


def test_validation_exceptions_are_not_exposed(web_module, monkeypatch):
    module = web_module
    monkeypatch.setattr(
        module,
        "_validated_context_refs",
        lambda _value: (_ for _ in ()).throw(ValueError("sensitive internal detail")),
    )
    response = module.flask_app.test_client().post(
        "/chat",
        headers=_auth_headers(module, "U2"),
        json={"message": "summarize", "context_refs": []},
    )

    assert response.status_code == 400
    assert response.get_json() == {"error": "Invalid chat context"}
    assert "sensitive internal detail" not in response.get_data(as_text=True)


def test_runtime_configuration_requires_https_for_oauth(web_module, monkeypatch):
    module = web_module
    monkeypatch.setattr(module, "CLIENT_URL", "http://evil.example/")
    with pytest.raises(RuntimeError, match="CLIENT_URL"):
        module._validate_runtime_configuration()

    monkeypatch.setattr(module, "CLIENT_URL", "https://frontend.example/")
    monkeypatch.setattr(module, "OAUTH_REDIRECT_URI", "http://evil.example/callback")
    with pytest.raises(RuntimeError, match="OAUTH_REDIRECT_URI"):
        module._validate_runtime_configuration()


def test_oauth_state_store_is_globally_bounded(web_module, monkeypatch):
    module = web_module
    monkeypatch.setattr(module, "MAX_OAUTH_STATES", 3)
    for index in range(8):
        module._store_oauth_state(f"state-{index}", "/")

    conn = sqlite3.connect(os.environ["DB_PATH"])
    count = conn.execute("SELECT COUNT(*) FROM oauth_states").fetchone()[0]
    conn.close()
    assert count == 3


def test_historical_aggregate_endpoints_are_admin_only(web_module, monkeypatch):
    module = web_module
    client = module.flask_app.test_client()
    member = _auth_headers(module, "U1")
    admin = _auth_headers(module, "U011PQ7RHRT")

    assert client.get("/stats", headers=member).status_code == 403
    assert client.post(
        "/digest_details", headers=member, json={"query": "summary"}
    ).status_code == 403
    assert client.get("/get_podcast_content", headers=member).status_code == 403
    assert client.get("/get_podcast_audio", headers=member).status_code == 403

    assert client.get("/stats?days=1", headers=admin).status_code == 200
    no_digest = client.post(
        "/digest_details", headers=admin, json={"query": "summary"}
    )
    assert no_digest.status_code == 200
    assert no_digest.get_json()["error"] == "No digest available"

    conn = sqlite3.connect(os.environ["DB_PATH"])
    conn.execute(
        """
        INSERT INTO digests(timestamp, period, digest, posts, podcast_content)
        VALUES ('2026-01-01', 'period', 'digest', 'posts', 'podcast')
        """
    )
    conn.commit()
    conn.close()
    assert client.get("/get_podcast_content", headers=admin).status_code == 200

    monkeypatch.setattr(module, "send_file", lambda *_args, **_kwargs: "audio")
    assert client.get("/get_podcast_audio", headers=admin).status_code == 200


def test_stats_do_not_expose_private_channels_without_membership(web_module):
    module = web_module
    now = datetime.datetime.now(datetime.timezone.utc).timestamp()
    public_root = f"{now - 10:.6f}"
    public_reply = f"{now - 9:.6f}"
    private_root = f"{now - 8:.6f}"
    private_reply = f"{now - 7:.6f}"
    conn = sqlite3.connect(os.environ["DB_PATH"])
    conn.executemany(
        """
        INSERT INTO messages
        (message, user, channel, timestamp, permalink, thread_ts, embeddings)
        VALUES (?, 'U1', ?, ?, '', ?, NULL)
        """,
        [
            ("visible public root", "CPUBLIC", public_root, public_root),
            ("visible public reply", "CPUBLIC", public_reply, public_root),
            ("secret private root", "CPRIVATE", private_root, private_root),
            ("secret private reply", "CPRIVATE", private_reply, private_root),
        ],
    )
    conn.commit()
    conn.close()

    response = module.flask_app.test_client().get(
        "/stats?days=1", headers=_auth_headers(module, "U011PQ7RHRT")
    )
    assert response.status_code == 200
    stats = response.get_json()
    assert {row["channel_id"] for row in stats["engaging_threads"]} == {"CPUBLIC"}
    assert all(row["channel"] != "private" for row in stats["engaging_threads"])
    assert all(row["name"] != "private" for row in stats["top_channels"])


def test_web_ai_rate_limit_and_cross_worker_lock_are_database_backed(web_module):
    module = web_module
    assert module._consume_web_ai_quota(
        "U1", "unit", minute_limit=2, hour_limit=10
    )[0]
    assert module._consume_web_ai_quota(
        "U1", "unit", minute_limit=2, hour_limit=10
    )[0]
    allowed, retry_after = module._consume_web_ai_quota(
        "U1", "unit", minute_limit=2, hour_limit=10
    )
    assert not allowed
    assert retry_after > 0

    owner = module._claim_web_ai_lock("unit-job")
    assert owner
    assert module._claim_web_ai_lock("unit-job") is None
    module._release_web_ai_lock("unit-job", owner)
    assert module._claim_web_ai_lock("unit-job")


def test_emoji_uses_only_the_server_side_bot_token(web_module, monkeypatch):
    module = web_module
    captured = {}

    class EmojiResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"ok": True, "emoji": {}}

    def fake_get(*_args, **kwargs):
        captured.update(kwargs)
        return EmojiResponse()

    monkeypatch.setattr(module.requests, "get", fake_get)
    response = module.flask_app.test_client().get(
        "/emoji", headers=_auth_headers(module, "U1")
    )

    assert response.status_code == 200
    assert captured["headers"]["Authorization"] == "Bearer xoxb-server-side-only"
    assert captured["timeout"] == 10


def test_emoji_missing_scope_degrades_without_breaking_the_ui(web_module, monkeypatch):
    module = web_module

    class MissingScopeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"ok": False, "error": "missing_scope", "needed": "emoji:read"}

    monkeypatch.setattr(module.requests, "get", lambda *_args, **_kwargs: MissingScopeResponse())
    response = module.flask_app.test_client().get(
        "/emoji", headers=_auth_headers(module, "U1")
    )

    assert response.status_code == 200
    assert response.json == {"ok": True, "emoji": {}, "degraded": True}


def test_public_getlink_is_exact_and_never_exposes_private_channels(web_module):
    module = web_module
    public_ts = "1700000000.100001"
    private_ts = "1700000001.100001"
    conn = sqlite3.connect(os.environ["DB_PATH"])
    conn.executemany(
        """
        INSERT INTO messages
        (message, user, channel, timestamp, permalink, thread_ts, embeddings)
        VALUES (?, 'U1', ?, ?, ?, ?, NULL)
        """,
        [
            (
                "public durable link",
                "CPUBLIC",
                public_ts,
                "https://sferait-ws.slack.com/archives/CPUBLIC/p1700000000100001",
                public_ts,
            ),
            (
                "private durable link",
                "CPRIVATE",
                private_ts,
                "https://sferait-ws.slack.com/archives/CPRIVATE/p1700000001100001",
                private_ts,
            ),
        ],
    )
    conn.commit()
    conn.close()

    client = module.flask_app.test_client()
    public = client.get(f"/getlink?timestamp={public_ts}")
    assert public.status_code == 302
    assert public.location.startswith("https://sferait-ws.slack.com/")
    assert public.headers["Cache-Control"] == "no-store"
    assert client.get(f"/getlink?timestamp={private_ts}").status_code == 404
    assert client.get("/getlink?timestamp=%25").status_code == 400


def test_archive_optout_purges_embeddings_links_and_saved_ai_material(web_module):
    module = web_module
    conn = sqlite3.connect(os.environ["DB_PATH"])
    conn.execute(
        "UPDATE messages SET embeddings = ? WHERE channel = 'CPUBLIC' AND timestamp = '100.1'",
        (module.np.array([1.0], dtype=module.np.float32).tobytes(),),
    )
    conn.execute(
        """
        INSERT INTO message_links
        (channel, message_timestamp, thread_ts, normalized_url, original_url,
         permalink, posted_at, deterministic_checked_at, duplicate_checked_at)
        VALUES ('CPUBLIC', '100.1', '100.1', 'https://example.test/',
                'https://example.test/', 'https://sferait-ws.slack.com/a', 100, 100, 100)
        """
    )
    conn.execute(
        """
        INSERT INTO digests(timestamp, period, digest, posts, podcast_content)
        VALUES ('2026-01-01', 'period', 'digest', 'private original post', 'podcast')
        """
    )
    conn.commit()
    conn.close()

    response = module.flask_app.test_client().get(
        "/optout", headers=_auth_headers(module, "U1")
    )
    assert response.status_code == 200

    conn = sqlite3.connect(os.environ["DB_PATH"])
    message = conn.execute(
        "SELECT message, user, permalink, embeddings FROM messages "
        "WHERE channel = 'CPUBLIC' AND timestamp = '100.1'"
    ).fetchone()
    assert message == (
        "User opted out of archiving. This message has been deleted",
        "USLACKBOT",
        "",
        None,
    )
    assert conn.execute("SELECT COUNT(*) FROM message_links").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM digests").fetchone()[0] == 0
    conn.close()


def test_embedding_search_excludes_ai_optout_messages(web_module, monkeypatch):
    module = web_module
    conn = sqlite3.connect(os.environ["DB_PATH"])
    conn.execute(
        "UPDATE messages SET embeddings = ? WHERE channel = 'CPUBLIC' AND timestamp = '100.1'",
        (module.np.array([1.0], dtype=module.np.float32).tobytes(),),
    )
    conn.execute("INSERT INTO optout_ai(user, timestamp) VALUES ('U1', 'now')")
    conn.commit()
    conn.close()
    monkeypatch.setattr(
        module,
        "_get_embedding_model",
        lambda: SimpleNamespace(
            encode=lambda _query: module.np.array([1.0], dtype=module.np.float32)
        ),
    )

    response = module.flask_app.test_client().get(
        "/searchEmbeddings?query=public",
        headers=_auth_headers(module, "U1"),
    )
    assert response.status_code == 200
    assert response.get_json() == []


def test_ai_optout_clears_existing_embeddings(web_module):
    module = web_module
    conn = sqlite3.connect(os.environ["DB_PATH"])
    conn.execute(
        "UPDATE messages SET embeddings = ? WHERE user = 'U1'",
        (module.np.array([1.0], dtype=module.np.float32).tobytes(),),
    )
    conn.commit()
    conn.close()

    response = module.flask_app.test_client().get(
        "/optout_ai", headers=_auth_headers(module, "U1")
    )
    assert response.status_code == 200
    assert response.get_json()["opted_out_ai"] is True

    conn = sqlite3.connect(os.environ["DB_PATH"])
    assert conn.execute(
        "SELECT COUNT(*) FROM messages WHERE user = 'U1' AND embeddings IS NOT NULL"
    ).fetchone()[0] == 0
    conn.close()


def test_embedding_search_skips_legacy_malformed_values(web_module, monkeypatch):
    module = web_module
    conn = sqlite3.connect(os.environ["DB_PATH"])
    conn.executemany(
        """
        INSERT INTO messages
        (message, user, channel, timestamp, permalink, thread_ts, embeddings)
        VALUES (?, 'U2', 'CPUBLIC', ?, '', ?, ?)
        """,
        [
            ("text embedding", "1700000010.100001", "1700000010.100001", ""),
            ("short blob", "1700000011.100001", "1700000011.100001", b"xx"),
        ],
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(
        module,
        "_get_embedding_model",
        lambda: SimpleNamespace(
            encode=lambda _query: module.np.array([1.0], dtype=module.np.float32)
        ),
    )

    response = module.flask_app.test_client().get(
        "/searchEmbeddings?query=embedding",
        headers=_auth_headers(module, "U2"),
    )
    assert response.status_code == 200
    assert response.get_json() == []


def test_download_users_neutralizes_csv_formulas_and_omits_optout(web_module):
    module = web_module
    conn = sqlite3.connect(os.environ["DB_PATH"])
    conn.execute(
        """
        INSERT INTO users(name, id, avatar, real_name, display_name, email, is_deleted)
        VALUES ('=HYPERLINK(""https://evil.test"")', 'UEVIL', '',
                '+cmd', '@formula', '-mail@example.test', 0)
        """
    )
    conn.execute("INSERT INTO optout(user, timestamp) VALUES ('U1', 'now')")
    conn.commit()
    conn.close()

    response = module.flask_app.test_client().get(
        "/download_users", headers=_auth_headers(module, "U011PQ7RHRT")
    )
    assert response.status_code == 200
    csv_text = response.get_json()["csv"]
    assert "'=HYPERLINK" in csv_text
    assert "'+cmd" in csv_text
    assert "'@formula" in csv_text
    assert "'-mail@example.test" in csv_text
    assert "Member One" not in csv_text
