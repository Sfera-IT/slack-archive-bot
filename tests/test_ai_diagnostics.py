import os
import sqlite3
import sys
from typing import ClassVar

ROOT_DIR = os.path.dirname(os.path.dirname(__file__))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from ai_diagnostics import (
    build_private_ai_error_report,
    get_ai_debug_recipients,
    is_ai_debug_enabled,
    new_ai_error_id,
    send_private_ai_error,
    set_ai_debug_enabled,
)
from utils import migrate_db


class ApiFailure(RuntimeError):
    status_code = 400
    code = "invalid_request"
    param = "tools"
    request_id = "req_123"
    body: ClassVar[dict] = {
        "error": {
            "message": "Bad request with sk-test-secret123 and token=xoxb-secret123",
            "code": "invalid_request",
            "param": "tools",
        }
    }


def _raise_api_failure():
    raise ApiFailure("Authorization: Bearer should-not-leak")


def test_private_error_report_contains_actionable_metadata_and_no_secrets():
    try:
        _raise_api_failure()
    except ApiFailure as exception:
        report = build_private_ai_error_report(
            exception,
            event={"user": "UTRIGGER", "channel": "C123", "ts": "1700000000.1"},
            model="gpt-5.6-sol",
            reasoning_effort="medium",
            error_id="AI-ABC123",
            source="engaged_thread",
        )

    assert "AI-ABC123" in report
    assert "ApiFailure" in report
    assert "status: `400`" in report
    assert "code: `invalid_request`" in report
    assert "param: `tools`" in report
    assert "request_id: `req_123`" in report
    assert "gpt-5.6-sol" in report
    assert "engaged_thread" in report
    assert "UTRIGGER" in report
    assert "test_ai_diagnostics.py" in report
    assert "sk-test-secret123" not in report
    assert "xoxb-secret123" not in report
    assert "should-not-leak" not in report
    assert "[REDACTED]" in report


def test_private_error_report_escapes_slack_mentions_and_bounds_message():
    exception = RuntimeError("<@U123> " + "x" * 5000)
    report = build_private_ai_error_report(
        exception,
        event={"channel": "C123", "ts": "1700000000.1"},
        model="gpt-5.6-sol",
        reasoning_effort="medium",
        error_id="AI-ABC123",
    )

    assert "<@U123>" not in report
    assert "&lt;@U123&gt;" in report
    assert "[troncato]" in report
    assert len(report) <= 3600


def test_error_id_is_short_and_has_a_stable_prefix():
    error_id = new_ai_error_id()

    assert error_id.startswith("AI-")
    assert len(error_id) == 13


def test_private_error_is_sent_to_the_requesting_users_dm():
    class FakeSlackClient:
        def __init__(self):
            self.calls = []

        def chat_postMessage(self, **kwargs):
            self.calls.append(kwargs)

    client = FakeSlackClient()
    send_private_ai_error(
        client,
        RuntimeError("boom"),
        event={"user": "UREQUESTER", "channel": "C123", "ts": "1700000000.1"},
        model="gpt-5.6-sol",
        reasoning_effort="medium",
        error_id="AI-ABC123",
        recipient_user_id="UADMIN",
        source="app_mention",
    )

    assert len(client.calls) == 1
    assert client.calls[0]["channel"] == "UADMIN"
    assert "AI-ABC123" in client.calls[0]["text"]


def test_ai_debug_subscription_is_disabled_by_default_and_can_be_toggled():
    conn = sqlite3.connect(":memory:")
    cursor = conn.cursor()
    migrate_db(conn, cursor)
    admin_user = "U011PQ7RHRT"

    assert not is_ai_debug_enabled(cursor, admin_user)
    assert get_ai_debug_recipients(cursor) == []

    set_ai_debug_enabled(cursor, admin_user, True)
    conn.commit()
    assert is_ai_debug_enabled(cursor, admin_user)
    assert get_ai_debug_recipients(cursor) == [admin_user]

    set_ai_debug_enabled(cursor, admin_user, False)
    conn.commit()
    assert not is_ai_debug_enabled(cursor, admin_user)
    assert get_ai_debug_recipients(cursor) == []
    conn.close()


def test_ai_debug_recipients_always_exclude_non_admin_subscribers():
    conn = sqlite3.connect(":memory:")
    cursor = conn.cursor()
    migrate_db(conn, cursor)
    set_ai_debug_enabled(cursor, "UNOTADMIN", True)
    set_ai_debug_enabled(cursor, "U011PQ7RHRT", True)
    conn.commit()

    assert get_ai_debug_recipients(cursor) == ["U011PQ7RHRT"]
    assert get_ai_debug_recipients(cursor, admin_users={"UNOTADMIN"}) == []
    assert get_ai_debug_recipients(cursor, admin_users=set()) == []
    conn.close()
