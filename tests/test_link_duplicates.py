import os
import sqlite3
import sys
import tempfile

import pytest


ROOT_DIR = os.path.dirname(os.path.dirname(__file__))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from link_duplicates import (
    deliver_duplicate_alert,
    ExternalLink,
    extract_external_links,
    finalize_duplicate_alert,
    prepare_exact_duplicate_alert,
    release_duplicate_alert,
)
import link_duplicates as link_duplicates_module
from link_enrichment import enqueue_link
from utils import migrate_db


def migrated_connection(path=":memory:"):
    conn = sqlite3.connect(path)
    migrate_db(conn, conn.cursor())
    return conn


def add_channel(conn, channel, *, private=False):
    conn.execute(
        "INSERT INTO channels(name, id, is_private) VALUES (?, ?, ?)",
        (channel.lower(), channel, int(private)),
    )
    conn.commit()


def add_link(
    conn,
    *,
    channel,
    message_ts,
    thread_ts,
    normalized_url="https://example.com/story",
    permalink=None,
    posted_at=None,
):
    enqueue_link(
        conn,
        channel=channel,
        message_timestamp=message_ts,
        thread_ts=thread_ts,
        normalized_url=normalized_url,
        original_url=normalized_url,
        permalink=permalink or f"https://workspace.slack.com/archives/{channel}/p{message_ts}",
        posted_at=float(message_ts) if posted_at is None else posted_at,
        now=float(message_ts) if posted_at is None else posted_at,
    )


def prepare(conn, *, channel="C2", message_ts="1000.2", thread_ts=None, links=None):
    return prepare_exact_duplicate_alert(
        conn,
        channel=channel,
        message_timestamp=message_ts,
        thread_ts=thread_ts or message_ts,
        permalink=f"https://workspace.slack.com/archives/{channel}/p{message_ts}",
        posted_at=float(message_ts),
        links=links or [ExternalLink("https://example.com/story", "https://example.com/story")],
        user_display_name="Alice",
        now=float(message_ts),
    )


def test_extract_external_links_is_link_only_unique_and_excludes_slack():
    links = extract_external_links(
        "No link first https://example.com/story?utm=x, "
        "again https://example.com/story?utm=x and "
        "https://workspace.slack.com/archives/C1/p1",
        lambda url: url.replace("?utm=x", ""),
    )

    assert links == [ExternalLink("https://example.com/story?utm=x", "https://example.com/story")]
    assert extract_external_links("Only prose about a migration", lambda url: url) == []


def test_exact_duplicate_covers_root_messages_across_public_channels():
    conn = migrated_connection()
    add_channel(conn, "C1")
    add_channel(conn, "C2")
    add_link(conn, channel="C1", message_ts="900.1", thread_ts="900.1")

    claim = prepare(conn)

    assert claim.match_type == "exact_url"
    assert claim.current_thread_ts == "1000.2"
    assert claim.source_channel == "C1"
    assert "*stesso link*" in claim.text
    assert "https://workspace.slack.com/archives/C1" in claim.text


def test_exact_duplicate_covers_thread_replies_and_posts_in_current_thread():
    conn = migrated_connection()
    add_channel(conn, "C1")
    add_channel(conn, "C2")
    add_link(conn, channel="C1", message_ts="900.1", thread_ts="800.1")

    claim = prepare(conn, message_ts="1000.2", thread_ts="950.1")

    assert claim is not None
    assert claim.current_thread_ts == "950.1"
    row = conn.execute(
        "SELECT thread_ts FROM message_links WHERE channel = 'C2'"
    ).fetchone()
    assert row == ("950.1",)


def test_same_thread_repost_is_recorded_but_does_not_alert():
    conn = migrated_connection()
    add_channel(conn, "C1")
    add_link(conn, channel="C1", message_ts="900.1", thread_ts="800.1")

    claim = prepare(conn, channel="C1", message_ts="1000.2", thread_ts="800.1")

    assert claim is None
    assert conn.execute("SELECT COUNT(*) FROM message_links").fetchone()[0] == 2


def test_cross_channel_private_source_is_not_disclosed():
    conn = migrated_connection()
    add_channel(conn, "PRIVATE", private=True)
    add_channel(conn, "PUBLIC")
    add_link(conn, channel="PRIVATE", message_ts="900.1", thread_ts="900.1")

    assert prepare(conn, channel="PUBLIC") is None


def test_same_private_channel_exact_duplicate_remains_eligible():
    conn = migrated_connection()
    add_channel(conn, "PRIVATE", private=True)
    add_link(conn, channel="PRIVATE", message_ts="900.1", thread_ts="900.1")

    assert prepare(conn, channel="PRIVATE") is not None


def test_exact_duplicate_outside_45_day_window_does_not_alert():
    conn = migrated_connection()
    add_channel(conn, "C1")
    add_channel(conn, "C2")
    current = 10_000_000.0
    add_link(
        conn,
        channel="C1",
        message_ts="1.0",
        thread_ts="1.0",
        posted_at=current - (46 * 24 * 60 * 60),
    )

    claim = prepare_exact_duplicate_alert(
        conn,
        channel="C2",
        message_timestamp=str(current),
        thread_ts=str(current),
        permalink="https://workspace.slack.com/current",
        posted_at=current,
        links=[ExternalLink("https://example.com/story", "https://example.com/story")],
        user_display_name="Alice",
        now=current,
    )
    assert claim is None


def test_multiple_matching_links_create_only_one_alert_for_new_message():
    conn = migrated_connection()
    add_channel(conn, "C1")
    add_channel(conn, "C2")
    add_link(
        conn,
        channel="C1",
        message_ts="800.1",
        thread_ts="800.1",
        normalized_url="https://one.example/story",
    )
    add_link(
        conn,
        channel="C1",
        message_ts="900.1",
        thread_ts="900.1",
        normalized_url="https://two.example/story",
    )
    links = [
        ExternalLink("https://one.example/story", "https://one.example/story"),
        ExternalLink("https://two.example/story", "https://two.example/story"),
    ]

    first = prepare(conn, links=links)
    second = prepare(conn, links=links)

    assert first.source_message_ts == "900.1"
    assert second is None
    assert conn.execute("SELECT COUNT(*) FROM link_duplicate_alerts").fetchone()[0] == 1


def test_finalize_and_release_are_fenced_by_claim_token():
    conn = migrated_connection()
    add_channel(conn, "C1")
    add_channel(conn, "C2")
    add_link(conn, channel="C1", message_ts="900.1", thread_ts="900.1")
    claim = prepare(conn)

    assert finalize_duplicate_alert(conn, claim, "1001.9", now=1001.9) is True
    assert finalize_duplicate_alert(conn, claim, "1002.9", now=1002.9) is False
    assert release_duplicate_alert(conn, claim) is False
    row = conn.execute(
        "SELECT status, alert_message_ts FROM link_duplicate_alerts"
    ).fetchone()
    assert row == ("posted", "1001.9")


def test_ambiguous_post_exception_is_uncertain_and_suppresses_retry():
    conn = migrated_connection()
    add_channel(conn, "C1")
    add_channel(conn, "C2")
    add_link(conn, channel="C1", message_ts="900.1", thread_ts="900.1")
    first = prepare(conn)

    accepted = []

    def accepted_then_timeout(text, thread_ts):
        accepted.append((text, thread_ts))
        raise TimeoutError("response lost after Slack accepted request")

    with pytest.raises(TimeoutError):
        deliver_duplicate_alert(
            conn,
            first,
            post=accepted_then_timeout,
            delete=lambda channel, ts: None,
        )

    assert len(accepted) == 1
    assert conn.execute("SELECT status FROM link_duplicate_alerts").fetchone()[0] == "uncertain"
    assert prepare(conn) is None


def test_missing_post_timestamp_is_ambiguous_and_suppresses_retry():
    conn = migrated_connection()
    add_channel(conn, "C1")
    add_channel(conn, "C2")
    add_link(conn, channel="C1", message_ts="900.1", thread_ts="900.1")
    claim = prepare(conn)

    assert deliver_duplicate_alert(
        conn,
        claim,
        post=lambda text, thread_ts: {"ok": True},
        delete=lambda channel, ts: None,
    ) is False
    assert conn.execute("SELECT status FROM link_duplicate_alerts").fetchone()[0] == "uncertain"
    assert prepare(conn) is None


def test_concurrent_event_delivery_only_one_connection_claims_alert():
    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, "duplicates.sqlite")
        first_conn = migrated_connection(path)
        add_channel(first_conn, "C1")
        add_channel(first_conn, "C2")
        add_link(first_conn, channel="C1", message_ts="900.1", thread_ts="900.1")
        second_conn = sqlite3.connect(path)

        first = prepare(first_conn)
        second = prepare(second_conn)

        assert first is not None
        assert second is None
        assert second_conn.execute("SELECT COUNT(*) FROM link_duplicate_alerts").fetchone()[0] == 1


def test_post_success_finalize_error_deletes_alert_before_releasing(monkeypatch):
    conn = migrated_connection()
    add_channel(conn, "C1")
    add_channel(conn, "C2")
    add_link(conn, channel="C1", message_ts="900.1", thread_ts="900.1")
    claim = prepare(conn)
    deleted = []

    def fail_finalize(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(link_duplicates_module, "finalize_duplicate_alert", fail_finalize)
    with pytest.raises(sqlite3.OperationalError):
        deliver_duplicate_alert(
            conn,
            claim,
            post=lambda text, thread_ts: {"ts": "1001.9"},
            delete=lambda channel, ts: deleted.append((channel, ts)),
        )

    assert deleted == [("C2", "1001.9")]
    assert conn.execute("SELECT COUNT(*) FROM link_duplicate_alerts").fetchone()[0] == 0
    assert prepare(conn) is not None


def test_cleanup_failure_preserves_uncertain_state_and_suppresses_retry(monkeypatch):
    conn = migrated_connection()
    add_channel(conn, "C1")
    add_channel(conn, "C2")
    add_link(conn, channel="C1", message_ts="900.1", thread_ts="900.1")
    claim = prepare(conn)

    def fail_finalize(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    def fail_delete(channel, ts):
        raise RuntimeError("Slack cleanup failed")

    monkeypatch.setattr(link_duplicates_module, "finalize_duplicate_alert", fail_finalize)
    with pytest.raises(RuntimeError, match="Slack cleanup failed"):
        deliver_duplicate_alert(
            conn,
            claim,
            post=lambda text, thread_ts: {"ts": "1001.9"},
            delete=fail_delete,
        )

    row = conn.execute(
        "SELECT status, alert_message_ts FROM link_duplicate_alerts"
    ).fetchone()
    assert row == ("uncertain", "1001.9")
    assert prepare(conn) is None


def test_interrupted_claim_is_fail_closed_against_repeated_delivery():
    conn = migrated_connection()
    add_channel(conn, "C1")
    add_channel(conn, "C2")
    add_link(conn, channel="C1", message_ts="900.1", thread_ts="900.1")

    first = prepare(conn)
    # Simulate process interruption after Slack posting but before finalize:
    # the durable claim remains and is intentionally not time-reclaimed.
    second = prepare(conn)

    assert first is not None
    assert second is None
    assert conn.execute("SELECT status FROM link_duplicate_alerts").fetchone()[0] == "claimed"
