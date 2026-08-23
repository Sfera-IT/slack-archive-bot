import os
import sqlite3
import sys
import tempfile

import pytest
import numpy as np


ROOT_DIR = os.path.dirname(os.path.dirname(__file__))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from link_duplicates import (
    DuplicateMatch,
    claim_duplicate_alert,
    collect_deleted_message_alerts,
    deliver_duplicate_alert,
    ExternalLink,
    extract_external_links,
    finalize_duplicate_alert,
    finalize_stored_alert_cleanup,
    prepare_enriched_duplicate_alerts,
    prepare_exact_duplicate_alert,
    reconcile_edited_message_links,
    release_duplicate_alert,
    route_link_message_event,
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
    conn.execute(
        """
        UPDATE message_links SET deterministic_checked_at = posted_at
        WHERE channel = ? AND message_timestamp = ? AND normalized_url = ?
        """,
        (channel, message_ts, normalized_url),
    )
    conn.commit()


def complete_document(
    conn,
    normalized_url,
    *,
    content,
    content_hash,
    embedding,
    quality="full_text",
):
    conn.execute(
        """
        UPDATE link_documents SET
            content = ?, content_hash = ?, embedding = ?,
            extraction_quality = ?, fetch_status = 'complete',
            fetched_at = 1, expires_at = 999999999
        WHERE normalized_url = ?
        """,
        (
            content,
            content_hash,
            np.asarray(embedding, dtype=np.float32).tobytes(),
            quality,
            normalized_url,
        ),
    )
    conn.execute(
        "UPDATE link_enrichment_jobs SET status = 'complete' WHERE normalized_url = ?",
        (normalized_url,),
    )
    conn.commit()


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


@pytest.mark.parametrize(
    ("text", "thread_ts"),
    [
        ("<@UBOT> review https://example.com/root", None),
        ("<@UBOT> /engage https://example.com/reply", "900.1"),
        ("<@UBOT> stop https://example.com/reply", "900.1"),
    ],
)
def test_link_routing_precedes_mention_and_engagement_early_returns(text, thread_ts):
    processed = []
    message = {"channel": "C1", "channel_type": "channel", "text": text}
    if thread_ts:
        message["thread_ts"] = thread_ts

    routed = route_link_message_event(
        message,
        lambda url: url,
        lambda links: processed.extend(links),
    )

    assert routed is True
    assert [link.normalized_url for link in processed] == [
        "https://example.com/root" if thread_ts is None else "https://example.com/reply"
    ]


@pytest.mark.parametrize(
    "message",
    [
        {"channel": "D1", "channel_type": "im", "text": "https://example.com/private"},
        {"channel": "C1", "channel_type": "channel", "text": "<@UBOT> /engage"},
        {"channel": "C1", "channel_type": "channel", "text": "<@UBOT> stop"},
    ],
)
def test_link_routing_ignores_dms_and_link_free_control_messages(message):
    processed = []

    assert route_link_message_event(message, lambda url: url, processed.extend) is False
    assert processed == []


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


def test_identical_extracted_content_precedes_semantic_similarity():
    conn = migrated_connection()
    add_channel(conn, "C1")
    add_channel(conn, "C2")
    add_link(
        conn,
        channel="C1",
        message_ts="900.1",
        thread_ts="900.1",
        normalized_url="https://source.example/story",
    )
    add_link(
        conn,
        channel="C2",
        message_ts="1000.2",
        thread_ts="1000.2",
        normalized_url="https://mirror.example/story",
    )
    complete_document(
        conn,
        "https://source.example/story",
        content="Identical extracted article",
        content_hash="same-hash",
        embedding=[1.0, 0.0],
    )
    complete_document(
        conn,
        "https://mirror.example/story",
        content="Identical extracted article",
        content_hash="same-hash",
        embedding=[0.0, 1.0],
    )

    claims = prepare_enriched_duplicate_alerts(conn, now=1100.0)

    assert len(claims) == 1
    assert claims[0].match_type == "same_content"
    assert claims[0].score == 1.0
    assert "URL diverso" in claims[0].text


def test_identical_metadata_hash_is_not_definitive_content_match():
    conn = migrated_connection()
    add_channel(conn, "C1")
    add_channel(conn, "C2")
    add_link(
        conn,
        channel="C1",
        message_ts="900.1",
        thread_ts="900.1",
        normalized_url="https://source.example/metadata",
    )
    add_link(
        conn,
        channel="C2",
        message_ts="1000.2",
        thread_ts="1000.2",
        normalized_url="https://mirror.example/metadata",
    )
    complete_document(
        conn,
        "https://source.example/metadata",
        content="Shared short title",
        content_hash="same-short-hash",
        embedding=[1.0, 0.0],
        quality="metadata_only",
    )
    complete_document(
        conn,
        "https://mirror.example/metadata",
        content="Shared short title",
        content_hash="same-short-hash",
        embedding=[0.0, 1.0],
        quality="metadata_only",
    )

    assert prepare_enriched_duplicate_alerts(
        conn,
        similarity_threshold=0.99,
        now=1100.0,
    ) == []


def test_identical_metadata_hash_can_only_produce_semantic_story_match():
    conn = migrated_connection()
    add_channel(conn, "C1")
    add_channel(conn, "C2")
    add_link(
        conn,
        channel="C1",
        message_ts="900.1",
        thread_ts="900.1",
        normalized_url="https://source.example/metadata",
    )
    add_link(
        conn,
        channel="C2",
        message_ts="1000.2",
        thread_ts="1000.2",
        normalized_url="https://mirror.example/metadata",
    )
    complete_document(
        conn,
        "https://source.example/metadata",
        content="Shared short title",
        content_hash="same-short-hash",
        embedding=[1.0, 0.0],
        quality="metadata_only",
    )
    complete_document(
        conn,
        "https://mirror.example/metadata",
        content="Shared short title",
        content_hash="same-short-hash",
        embedding=[1.0, 0.0],
        quality="metadata_only",
    )

    claims = prepare_enriched_duplicate_alerts(
        conn,
        similarity_threshold=0.90,
        now=1100.0,
    )

    assert len(claims) == 1
    assert claims[0].match_type == "same_story"
    assert claims[0].score == pytest.approx(1.0)


def test_high_similarity_different_urls_create_potential_story_claim():
    conn = migrated_connection()
    add_channel(conn, "C1")
    add_channel(conn, "C2")
    add_link(
        conn,
        channel="C1",
        message_ts="900.1",
        thread_ts="900.1",
        normalized_url="https://one.example/report",
    )
    add_link(
        conn,
        channel="C2",
        message_ts="1000.2",
        thread_ts="1000.2",
        normalized_url="https://two.example/report",
    )
    complete_document(
        conn,
        "https://one.example/report",
        content="First report",
        content_hash="first",
        embedding=[1.0, 0.0],
    )
    complete_document(
        conn,
        "https://two.example/report",
        content="Second report",
        content_hash="second",
        embedding=[0.99, 0.01],
    )

    claims = prepare_enriched_duplicate_alerts(
        conn,
        similarity_threshold=0.95,
        now=1100.0,
    )

    assert len(claims) == 1
    assert claims[0].match_type == "same_story"
    assert claims[0].score > 0.99
    assert "Potenzialmente" in claims[0].text
    assert "fonte diversa" in claims[0].text


def test_below_threshold_is_checked_without_alert():
    conn = migrated_connection()
    add_channel(conn, "C1")
    add_channel(conn, "C2")
    add_link(
        conn,
        channel="C1",
        message_ts="900.1",
        thread_ts="900.1",
        normalized_url="https://one.example/report",
    )
    add_link(
        conn,
        channel="C2",
        message_ts="1000.2",
        thread_ts="1000.2",
        normalized_url="https://two.example/report",
    )
    complete_document(conn, "https://one.example/report", content="One", content_hash="one", embedding=[1, 0])
    complete_document(conn, "https://two.example/report", content="Two", content_hash="two", embedding=[0, 1])

    assert prepare_enriched_duplicate_alerts(conn, similarity_threshold=0.95, now=1100.0) == []
    checked = conn.execute(
        "SELECT duplicate_checked_at FROM message_links WHERE channel = 'C2'"
    ).fetchone()[0]
    assert checked == 1100.0


def test_private_channels_are_excluded_from_enriched_matching():
    conn = migrated_connection()
    add_channel(conn, "PRIVATE", private=True)
    add_channel(conn, "PUBLIC")
    add_link(
        conn,
        channel="PRIVATE",
        message_ts="900.1",
        thread_ts="900.1",
        normalized_url="https://one.example/report",
    )
    add_link(
        conn,
        channel="PUBLIC",
        message_ts="1000.2",
        thread_ts="1000.2",
        normalized_url="https://two.example/report",
    )
    complete_document(conn, "https://one.example/report", content="Same", content_hash="same", embedding=[1, 0])
    complete_document(conn, "https://two.example/report", content="Same", content_hash="same", embedding=[1, 0])

    assert prepare_enriched_duplicate_alerts(conn, now=1100.0) == []


def test_out_of_order_enrichment_waits_for_pending_prior_document():
    conn = migrated_connection()
    add_channel(conn, "C1")
    add_channel(conn, "C2")
    add_link(
        conn,
        channel="C1",
        message_ts="900.1",
        thread_ts="900.1",
        normalized_url="https://slow.example/report",
    )
    add_link(
        conn,
        channel="C2",
        message_ts="1000.2",
        thread_ts="1000.2",
        normalized_url="https://fast.example/report",
    )
    complete_document(conn, "https://fast.example/report", content="Same", content_hash="same", embedding=[1, 0])

    assert prepare_enriched_duplicate_alerts(conn, now=1050.0) == []
    assert conn.execute(
        "SELECT duplicate_checked_at FROM message_links WHERE channel = 'C2'"
    ).fetchone()[0] is None

    complete_document(conn, "https://slow.example/report", content="Same", content_hash="same", embedding=[1, 0])
    claims = prepare_enriched_duplicate_alerts(conn, now=1100.0)
    assert len(claims) == 1
    assert claims[0].current_channel == "C2"


def test_edit_removes_obsolete_link_alert_but_retains_shared_document():
    conn = migrated_connection()
    add_channel(conn, "C1")
    add_channel(conn, "C2")
    add_link(conn, channel="C1", message_ts="900.1", thread_ts="900.1")
    claim = prepare(conn)
    assert finalize_duplicate_alert(conn, claim, "1001.9") is True

    alerts = reconcile_edited_message_links(
        conn,
        channel="C2",
        message_timestamp="1000.2",
        active_normalized_urls=set(),
    )

    assert alerts[0].alert_message_ts == "1001.9"
    assert conn.execute("SELECT COUNT(*) FROM message_links WHERE channel = 'C2'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM link_documents").fetchone()[0] == 1
    finalize_stored_alert_cleanup(conn, alerts[0], deleted=True)
    assert conn.execute("SELECT COUNT(*) FROM link_duplicate_alerts").fetchone()[0] == 0


def test_deleting_source_returns_dependent_alert_and_keeps_shared_cache():
    conn = migrated_connection()
    add_channel(conn, "C1")
    add_channel(conn, "C2")
    add_link(conn, channel="C1", message_ts="900.1", thread_ts="900.1")
    claim = prepare(conn)
    assert finalize_duplicate_alert(conn, claim, "1001.9") is True

    alerts = collect_deleted_message_alerts(
        conn,
        channel="C1",
        message_timestamp="900.1",
    )

    assert alerts == [
        link_duplicates_module.StoredAlert("C2", "1000.2", "1001.9")
    ]
    assert conn.execute("SELECT COUNT(*) FROM message_links WHERE channel = 'C1'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM link_documents").fetchone()[0] == 1


def test_editing_source_to_remove_link_returns_dependent_alert():
    conn = migrated_connection()
    add_channel(conn, "C1")
    add_channel(conn, "C2")
    add_link(conn, channel="C1", message_ts="900.1", thread_ts="900.1")
    claim = prepare(conn)
    assert finalize_duplicate_alert(conn, claim, "1001.9") is True

    alerts = reconcile_edited_message_links(
        conn,
        channel="C1",
        message_timestamp="900.1",
        active_normalized_urls=set(),
    )

    assert alerts == [
        link_duplicates_module.StoredAlert("C2", "1000.2", "1001.9")
    ]
    assert conn.execute("SELECT COUNT(*) FROM link_documents").fetchone()[0] == 1


def test_full_45_day_candidate_set_finds_match_older_than_200_newer_links():
    conn = migrated_connection()
    add_channel(conn, "C1")
    add_channel(conn, "C2")
    target_url = "https://old.example/target"
    add_link(
        conn,
        channel="C1",
        message_ts="700.0",
        thread_ts="700.0",
        normalized_url=target_url,
    )
    complete_document(
        conn,
        target_url,
        content="Target article",
        content_hash="target-hash",
        embedding=[1, 0],
    )
    for index in range(201):
        timestamp = 701.0 + index
        url = f"https://noise.example/{index}"
        add_link(
            conn,
            channel="C1",
            message_ts=str(timestamp),
            thread_ts=str(timestamp),
            normalized_url=url,
        )
        complete_document(
            conn,
            url,
            content=f"Noise article {index}",
            content_hash=f"noise-{index}",
            embedding=[0, 1],
        )
    # Reinsert the old matching association after the noise rows so it lives
    # beyond two 100-candidate rowid pages despite having the oldest timestamp.
    conn.execute(
        "DELETE FROM message_links WHERE channel = 'C1' AND message_timestamp = '700.0'"
    )
    conn.commit()
    add_link(
        conn,
        channel="C1",
        message_ts="700.0",
        thread_ts="700.0",
        normalized_url=target_url,
    )
    current_url = "https://new.example/target"
    add_link(
        conn,
        channel="C2",
        message_ts="1000.0",
        thread_ts="1000.0",
        normalized_url=current_url,
    )
    complete_document(
        conn,
        current_url,
        content="Target article",
        content_hash="target-hash",
        embedding=[1, 0],
    )
    conn.execute(
        "UPDATE message_links SET duplicate_checked_at = 1 WHERE channel = 'C1'"
    )
    conn.commit()

    first = prepare_enriched_duplicate_alerts(
        conn,
        similarity_threshold=1.0,
        now=1100.0,
        max_current_rows=1,
        max_candidate_comparisons=100,
    )
    assert first == []
    assert conn.execute(
        "SELECT duplicate_checked_at FROM message_links WHERE channel = 'C2'"
    ).fetchone()[0] is None
    assert conn.execute(
        "SELECT candidate_after_rowid FROM link_match_scans"
    ).fetchone()[0] > 0

    second = prepare_enriched_duplicate_alerts(
        conn,
        similarity_threshold=1.0,
        now=1101.0,
        max_current_rows=1,
        max_candidate_comparisons=100,
    )
    assert second == []
    assert conn.execute(
        "SELECT duplicate_checked_at FROM message_links WHERE channel = 'C2'"
    ).fetchone()[0] is None

    claims = prepare_enriched_duplicate_alerts(
        conn,
        similarity_threshold=1.0,
        now=1102.0,
        max_current_rows=1,
        max_candidate_comparisons=100,
    )

    assert len(claims) == 1
    assert claims[0].source_message_ts == "700.0"
    assert claims[0].match_type == "same_content"


@pytest.mark.parametrize("removed_side", ["current", "source"])
def test_stale_scan_snapshot_cannot_claim_after_association_removal(removed_side):
    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, "stale-scan.sqlite")
        scan_conn = migrated_connection(path)
        add_channel(scan_conn, "C1")
        add_channel(scan_conn, "C2")
        source_url = "https://source.example/story"
        current_url = "https://current.example/story"
        add_link(
            scan_conn,
            channel="C1",
            message_ts="900.0",
            thread_ts="900.0",
            normalized_url=source_url,
        )
        add_link(
            scan_conn,
            channel="C2",
            message_ts="1000.0",
            thread_ts="1000.0",
            normalized_url=current_url,
        )
        stale_match = DuplicateMatch(
            normalized_url=current_url,
            source_normalized_url=source_url,
            source_channel="C1",
            source_message_ts="900.0",
            source_thread_ts="900.0",
            source_permalink="https://workspace.slack.com/source",
            source_posted_at=900.0,
        )

        edit_conn = sqlite3.connect(path)
        if removed_side == "current":
            edit_conn.execute(
                "DELETE FROM message_links WHERE channel = 'C2' AND message_timestamp = '1000.0'"
            )
        else:
            edit_conn.execute(
                "DELETE FROM message_links WHERE channel = 'C1' AND message_timestamp = '900.0'"
            )
        edit_conn.commit()

        claim = claim_duplicate_alert(
            scan_conn,
            current_channel="C2",
            current_message_ts="1000.0",
            current_thread_ts="1000.0",
            current_normalized_url=current_url,
            match=stale_match,
            match_type="same_story",
            text="potential match",
            score=0.99,
            now=1100.0,
        )

        assert claim is None
        assert scan_conn.execute("SELECT COUNT(*) FROM link_duplicate_alerts").fetchone()[0] == 0


def test_cached_exact_url_wins_before_background_enriched_scan():
    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, "exact-precedence.sqlite")
        event_conn = migrated_connection(path)
        add_channel(event_conn, "C1")
        add_channel(event_conn, "C2")
        url = "https://example.com/cached"
        add_link(
            event_conn,
            channel="C1",
            message_ts="900.0",
            thread_ts="900.0",
            normalized_url=url,
        )
        complete_document(
            event_conn,
            url,
            content="Cached article",
            content_hash="cached",
            embedding=[1, 0],
        )

        # The event path has committed enqueue_link but has not yet completed
        # deterministic matching.
        enqueue_link(
            event_conn,
            channel="C2",
            message_timestamp="1000.0",
            thread_ts="1000.0",
            normalized_url=url,
            original_url=url,
            permalink="https://workspace.slack.com/current",
            posted_at=1000.0,
            now=1000.0,
        )
        background_conn = sqlite3.connect(path)
        assert prepare_enriched_duplicate_alerts(background_conn, now=1000.1) == []
        assert background_conn.execute(
            """
            SELECT deterministic_checked_at, duplicate_checked_at
            FROM message_links WHERE channel = 'C2'
            """
        ).fetchone() == (None, None)

        claim = prepare_exact_duplicate_alert(
            event_conn,
            channel="C2",
            message_timestamp="1000.0",
            thread_ts="1000.0",
            permalink="https://workspace.slack.com/current",
            posted_at=1000.0,
            links=[ExternalLink(url, url)],
            user_display_name="Alice",
            now=1000.2,
        )

        assert claim is not None
        assert claim.match_type == "exact_url"
