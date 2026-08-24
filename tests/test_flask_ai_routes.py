import datetime
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
    command = _decorator

    def error(self, function):
        return function


class FakeResponse:
    status = "completed"
    error = None

    def __init__(self, text):
        self.output_text = text


class FakeResponses:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return FakeResponse(self.outputs.pop(0))


@pytest.fixture
def web_module(monkeypatch, tmp_path):
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "dummy")
    monkeypatch.setitem(sys.modules, "slack_bolt", SimpleNamespace(App=FakeSlackApp))
    monkeypatch.setitem(
        sys.modules,
        "sentence_transformers",
        SimpleNamespace(SentenceTransformer=object),
    )
    sys.modules.pop("archivebot", None)
    archivebot = importlib.import_module("archivebot")
    database_path = str(tmp_path / "archive.sqlite")
    archivebot.database_path = database_path
    conn, cursor = archivebot.db_connect(database_path)
    archivebot.migrate_db(conn, cursor)
    conn.close()

    flask_adapter = ModuleType("slack_bolt.adapter.flask")

    class FakeSlackRequestHandler:
        def __init__(self, app):
            self.app = app

        def handle(self, _request):
            return "", 200

    flask_adapter.SlackRequestHandler = FakeSlackRequestHandler
    monkeypatch.setitem(sys.modules, "slack_bolt.adapter", ModuleType("slack_bolt.adapter"))
    monkeypatch.setitem(sys.modules, "slack_bolt.adapter.flask", flask_adapter)
    monkeypatch.setitem(sys.modules, "archivebot", archivebot)
    sys.modules.pop("flask_app", None)
    web = importlib.import_module("flask_app")
    web.flask_app.config.update(TESTING=True)

    def get_db_connection():
        connection = sqlite3.connect(database_path)
        connection.row_factory = sqlite3.Row
        return connection

    monkeypatch.setattr(web, "get_db_connection", get_db_connection)
    monkeypatch.setattr(
        web,
        "verify_token_and_get_user",
        lambda _headers: {"user_id": web.ADMIN_USERS[0], "slack_token": "dummy"},
    )
    monkeypatch.setattr(web, "get_username", lambda _user: "Fabio")
    yield web, database_path
    sys.modules.pop("flask_app", None)


def install_openai_client(web, monkeypatch, *outputs):
    responses = FakeResponses(outputs)
    client = SimpleNamespace(responses=responses)
    monkeypatch.setattr(web, "OpenAI", lambda **_kwargs: client)
    return responses


def auth_headers():
    return {"Authorization": "Bearer test"}


def seed_digest_source(database_path):
    timestamp = str(datetime.datetime.now().timestamp())
    conn = sqlite3.connect(database_path)
    conn.execute("INSERT INTO users(name, id, avatar) VALUES ('Alice', 'U1', '')")
    conn.execute("INSERT INTO channels(name, id, is_private) VALUES ('dev', 'C1', 0)")
    conn.execute(
        """
        INSERT INTO messages
        (message, user, channel, timestamp, permalink, thread_ts, embeddings)
        VALUES ('decisione importante', 'U1', 'C1', ?, '', ?, NULL)
        """,
        (timestamp, timestamp),
    )
    conn.commit()
    conn.close()
    return timestamp


def test_generate_digest_and_podcast_use_responses_api_and_persist_results(
    web_module,
    monkeypatch,
):
    web, database_path = web_module
    seed_digest_source(database_path)
    responses = install_openai_client(
        web,
        monkeypatch,
        "digest generato",
        "podcast generato",
    )
    generated_audio = []
    monkeypatch.setattr(
        web,
        "generate_podcast_audio",
        lambda content: generated_audio.append(content),
    )

    response = web.flask_app.test_client().post(
        "/generate_digest",
        json={"force_generate": True, "send_to_channel": False},
        headers=auth_headers(),
    )

    assert response.status_code == 200
    assert response.get_json()["digest"] == "digest generato"
    assert generated_audio == ["podcast generato"]
    assert len(responses.calls) == 2
    assert responses.calls[0]["model"] == web.DEFAULT_OPENAI_MODEL
    assert responses.calls[0]["reasoning"] == {"effort": "medium"}
    assert responses.calls[0]["max_output_tokens"] == 16384
    assert responses.calls[0]["store"] is False
    assert "decisione importante" in responses.calls[0]["input"]
    assert responses.calls[1]["reasoning"] == {"effort": "low"}
    assert responses.calls[1]["max_output_tokens"] == 8192
    assert responses.calls[1]["store"] is False

    conn = sqlite3.connect(database_path)
    try:
        assert conn.execute(
            "SELECT digest, podcast_content FROM digests"
        ).fetchone() == ("digest generato", "podcast generato")
    finally:
        conn.close()


def test_generate_digest_reuses_recent_cached_result_without_openai(web_module):
    web, database_path = web_module
    conn = sqlite3.connect(database_path)
    conn.execute(
        "INSERT INTO digests(timestamp, period, digest) VALUES (CURRENT_TIMESTAMP, 'oggi', 'cached')"
    )
    conn.commit()
    conn.close()

    response = web.flask_app.test_client().post(
        "/generate_digest",
        json={"force_generate": False, "send_to_channel": False},
        headers=auth_headers(),
    )

    assert response.status_code == 200
    assert response.get_json() == {
        "status": "success",
        "digest": "cached",
        "period": "oggi",
    }


def test_digest_details_uses_responses_api_and_persists_history(
    web_module,
    monkeypatch,
):
    web, database_path = web_module
    conn = sqlite3.connect(database_path)
    conn.execute(
        """
        INSERT INTO digests(timestamp, period, digest, posts)
        VALUES ('2026-08-24T10:00:00', 'oggi', 'digest', 'post sorgente')
        """
    )
    conn.commit()
    conn.close()
    responses = install_openai_client(web, monkeypatch, "dettaglio generato")

    response = web.flask_app.test_client().post(
        "/digest_details",
        json={"query": "Spiegami la decisione"},
        headers=auth_headers(),
    )

    assert response.status_code == 200
    assert response.get_json() == {
        "status": "success",
        "details": "dettaglio generato",
    }
    assert len(responses.calls) == 1
    assert responses.calls[0]["reasoning"] == {"effort": "medium"}
    assert responses.calls[0]["max_output_tokens"] == 4096
    assert "post sorgente" in responses.calls[0]["input"]
    assert "Spiegami la decisione" in responses.calls[0]["input"]
    conn = sqlite3.connect(database_path)
    try:
        assert conn.execute(
            "SELECT query, details, digest_timestamp FROM digest_details"
        ).fetchone() == (
            "Spiegami la decisione",
            "dettaglio generato",
            "2026-08-24T10:00:00",
        )
    finally:
        conn.close()


def test_chat_uses_responses_api_and_returns_updated_conversation(
    web_module,
    monkeypatch,
):
    web, _database_path = web_module
    responses = install_openai_client(web, monkeypatch, "risposta chat")

    response = web.flask_app.test_client().post(
        "/chat",
        json={
            "message": "Cosa abbiamo deciso?",
            "context": [{"user_name": "Alice", "message": "Usiamo SQLite"}],
            "conversation": [{"user_name": "Fabio", "message": "Ricapitoliamo"}],
        },
        headers=auth_headers(),
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["status"] == "success"
    assert payload["conversation"][-1]["user_name"] == "AI"
    assert payload["conversation"][-1]["message"] == "risposta chat"
    assert len(responses.calls) == 1
    assert responses.calls[0]["reasoning"] == {"effort": "medium"}
    assert responses.calls[0]["max_output_tokens"] == 4096
    assert "Alice: Usiamo SQLite" in responses.calls[0]["input"]
    assert "Fabio: Ricapitoliamo" in responses.calls[0]["input"]
    assert "User: Cosa abbiamo deciso?" in responses.calls[0]["input"]


def test_chat_resolves_active_frontend_context_refs_server_side(
    web_module,
    monkeypatch,
):
    web, database_path = web_module
    conn = sqlite3.connect(database_path)
    conn.execute("INSERT INTO users(name, id, avatar) VALUES ('Alice', 'U1', '')")
    conn.execute(
        "INSERT INTO channels(name, id, is_private) VALUES ('dev', 'C12345678', 0)"
    )
    conn.execute(
        """
        INSERT INTO messages
        (message, user, channel, timestamp, permalink, thread_ts, embeddings)
        VALUES ('decisione dal riferimento', 'U1', 'C12345678',
                '1787572162.797899', '', '1787572000.000001', NULL)
        """
    )
    conn.commit()
    conn.close()
    responses = install_openai_client(web, monkeypatch, "risposta con contesto")

    response = web.flask_app.test_client().post(
        "/chat",
        json={
            "message": "Cosa abbiamo deciso?",
            "context_refs": [
                {"channel": "C12345678", "timestamp": "1787572162.797899"}
            ],
            "conversation": [],
        },
        headers=auth_headers(),
    )

    assert response.status_code == 200
    assert "Alice: decisione dal riferimento" in responses.calls[0]["input"]


def test_chat_context_refs_exclude_archive_and_ai_optouts(
    web_module,
    monkeypatch,
):
    web, database_path = web_module
    conn = sqlite3.connect(database_path)
    conn.executemany(
        "INSERT INTO users(name, id, avatar) VALUES (?, ?, '')",
        [("Visible", "UVISIBLE"), ("Archive opt-out", "UARCHIVE"), ("AI opt-out", "UAI")],
    )
    conn.execute(
        "INSERT INTO channels(name, id, is_private) VALUES ('dev', 'C12345678', 0)"
    )
    rows = [
        ("visible context", "UVISIBLE", "1787572162.000001"),
        ("archive private context", "UARCHIVE", "1787572162.000002"),
        ("ai private context", "UAI", "1787572162.000003"),
    ]
    for message, user, timestamp in rows:
        conn.execute(
            """
            INSERT INTO messages
            (message, user, channel, timestamp, permalink, thread_ts, embeddings)
            VALUES (?, ?, 'C12345678', ?, '', ?, NULL)
            """,
            (message, user, timestamp, timestamp),
        )
    conn.execute("INSERT INTO optout(user, timestamp) VALUES ('UARCHIVE', 'now')")
    conn.execute("INSERT INTO optout_ai(user, timestamp) VALUES ('UAI', 'now')")
    conn.commit()
    conn.close()
    responses = install_openai_client(web, monkeypatch, "filtered response")

    response = web.flask_app.test_client().post(
        "/chat",
        json={
            "message": "Ricapitola",
            "context_refs": [
                {"channel": "C12345678", "timestamp": timestamp}
                for _message, _user, timestamp in rows
            ],
            "conversation": [],
        },
        headers=auth_headers(),
    )

    assert response.status_code == 200
    request_input = responses.calls[0]["input"]
    assert "Visible: visible context" in request_input
    assert "archive private context" not in request_input
    assert "ai private context" not in request_input


def test_chat_rejects_oversized_context_refs_before_openai(web_module):
    web, _database_path = web_module

    response = web.flask_app.test_client().post(
        "/chat",
        json={
            "message": "Ricapitola",
            "context_refs": [
                {"channel": "C12345678", "timestamp": f"1787572162.{index:06d}"}
                for index in range(web.MAX_CHAT_CONTEXT_REFS + 1)
            ],
            "conversation": [],
        },
        headers=auth_headers(),
    )

    assert response.status_code == 400
    assert response.get_json() == {"error": "Invalid chat context"}


def test_exact_thread_route_matches_active_frontend_contract(web_module):
    web, database_path = web_module
    conn = sqlite3.connect(database_path)
    conn.execute("INSERT INTO users(name, id, avatar) VALUES ('Alice', 'U1', '')")
    conn.executemany(
        "INSERT INTO channels(name, id, is_private) VALUES (?, ?, 0)",
        [("dev", "C12345678"), ("ops", "C87654321")],
    )
    rows = [
        ("root dev", "C12345678", "1787572000.000001"),
        ("reply dev", "C12345678", "1787572162.797899"),
        ("same timestamp other channel", "C87654321", "1787572000.000001"),
    ]
    for text, channel, timestamp in rows:
        conn.execute(
            """
            INSERT INTO messages
            (message, user, channel, timestamp, permalink, thread_ts, embeddings)
            VALUES (?, 'U1', ?, ?, '', '1787572000.000001', NULL)
            """,
            (text, channel, timestamp),
        )
    conn.commit()
    conn.close()

    response = web.flask_app.test_client().get(
        "/thread/C12345678/1787572000.000001",
        headers=auth_headers(),
    )

    assert response.status_code == 200
    assert [item["message"] for item in response.get_json()] == [
        "root dev",
        "reply dev",
    ]


def test_podcast_content_and_audio_routes_keep_existing_contract(
    web_module,
    monkeypatch,
):
    web, database_path = web_module
    conn = sqlite3.connect(database_path)
    conn.execute(
        """
        INSERT INTO digests(timestamp, period, digest, podcast_content)
        VALUES ('2026-08-24T10:00:00', 'oggi', 'digest', 'contenuto podcast')
        """
    )
    conn.commit()
    conn.close()

    content_response = web.flask_app.test_client().get(
        "/get_podcast_content",
        headers=auth_headers(),
    )
    assert content_response.status_code == 200
    assert content_response.get_json() == {"podcast_content": "contenuto podcast"}

    sent_files = []
    monkeypatch.setattr(
        web,
        "send_file",
        lambda path, **kwargs: sent_files.append((path, kwargs)) or ("audio", 200),
    )
    audio_response = web.flask_app.test_client().get(
        "/get_podcast_audio",
        headers=auth_headers(),
    )
    assert audio_response.status_code == 200
    assert sent_files == [
        ("podcast.mp3", {"mimetype": "audio/mpeg", "as_attachment": True})
    ]
