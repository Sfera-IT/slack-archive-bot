"""Deterministic and enriched duplicate-link orchestration.

This module owns database decisions and alert claims. Slack API calls remain in
archivebot.py so the behavior can be tested without importing or authenticating
the Slack application.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
import sqlite3
import time
from typing import Callable
from urllib.parse import urlsplit
import uuid

from link_enrichment import enqueue_link


DUPLICATE_WINDOW_SECONDS = 45 * 24 * 60 * 60
URL_PATTERN = re.compile(r'https?://[^\s<>"{}|\\^`\[\]]+', re.IGNORECASE)


@dataclass(frozen=True)
class ExternalLink:
    original_url: str
    normalized_url: str


@dataclass(frozen=True)
class DuplicateMatch:
    normalized_url: str
    source_channel: str
    source_message_ts: str
    source_thread_ts: str
    source_permalink: str
    source_posted_at: float


@dataclass(frozen=True)
class AlertClaim:
    current_channel: str
    current_message_ts: str
    current_thread_ts: str
    source_channel: str
    source_message_ts: str
    source_permalink: str
    match_type: str
    score: float | None
    text: str
    claim_token: str


def is_slack_url(url: str) -> bool:
    try:
        hostname = (urlsplit(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    return hostname == "slack.com" or hostname.endswith(".slack.com")


def extract_external_links(
    text: str,
    normalize: Callable[[str], str],
) -> list[ExternalLink]:
    """Extract unique external HTTP(S) links in message order."""
    links: list[ExternalLink] = []
    seen: set[str] = set()
    for match in URL_PATTERN.findall(text or ""):
        original_url = match.rstrip(".,;:!?")
        if is_slack_url(original_url):
            continue
        normalized_url = normalize(original_url)
        try:
            parsed = urlsplit(normalized_url)
        except ValueError:
            continue
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            continue
        if normalized_url in seen:
            continue
        seen.add(normalized_url)
        links.append(ExternalLink(original_url, normalized_url))
    return links


def find_exact_duplicate(
    conn: sqlite3.Connection,
    *,
    normalized_url: str,
    current_channel: str,
    current_message_ts: str,
    current_thread_ts: str,
    posted_at: float,
    window_seconds: float = DUPLICATE_WINDOW_SECONDS,
) -> DuplicateMatch | None:
    """Find the newest eligible exact URL without crossing privacy boundaries."""
    row = conn.execute(
        """
        SELECT ml.normalized_url, ml.channel, ml.message_timestamp, ml.thread_ts,
               ml.permalink, ml.posted_at
        FROM message_links ml
        LEFT JOIN channels source_channel ON source_channel.id = ml.channel
        LEFT JOIN channels current_channel ON current_channel.id = ?
        WHERE ml.normalized_url = ?
          AND ml.posted_at >= ?
          AND ml.posted_at <= ?
          AND NOT (ml.channel = ? AND ml.message_timestamp = ?)
          AND NOT (ml.channel = ? AND ml.thread_ts = ?)
          AND ml.permalink != ''
          AND (
                ml.channel = ?
                OR (
                    COALESCE(source_channel.is_private, 1) = 0
                    AND COALESCE(current_channel.is_private, 1) = 0
                )
          )
        ORDER BY ml.posted_at DESC
        LIMIT 1
        """,
        (
            current_channel,
            normalized_url,
            posted_at - window_seconds,
            posted_at,
            current_channel,
            current_message_ts,
            current_channel,
            current_thread_ts,
            current_channel,
        ),
    ).fetchone()
    if row is None:
        return None
    return DuplicateMatch(*row)


def claim_duplicate_alert(
    conn: sqlite3.Connection,
    *,
    current_channel: str,
    current_message_ts: str,
    current_thread_ts: str,
    match: DuplicateMatch,
    match_type: str,
    text: str,
    score: float | None = None,
    now: float | None = None,
) -> AlertClaim | None:
    now = now if now is not None else time.time()
    claim_token = uuid.uuid4().hex
    cursor = conn.cursor()
    cursor.execute("BEGIN IMMEDIATE")
    cursor.execute(
        """
        INSERT OR IGNORE INTO link_duplicate_alerts
        (current_channel, current_message_ts, current_thread_ts,
         source_channel, source_message_ts, source_permalink,
         match_type, score, alert_text, status, claim_token,
         claimed_at, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'claimed', ?, ?, ?, ?)
        """,
        (
            current_channel,
            current_message_ts,
            current_thread_ts,
            match.source_channel,
            match.source_message_ts,
            match.source_permalink,
            match_type,
            score,
            text,
            claim_token,
            now,
            now,
            now,
        ),
    )
    if cursor.rowcount != 1:
        conn.commit()
        return None
    cursor.execute(
        """
        UPDATE message_links SET duplicate_checked_at = ?
        WHERE channel = ? AND message_timestamp = ?
        """,
        (now, current_channel, current_message_ts),
    )
    conn.commit()
    return AlertClaim(
        current_channel=current_channel,
        current_message_ts=current_message_ts,
        current_thread_ts=current_thread_ts,
        source_channel=match.source_channel,
        source_message_ts=match.source_message_ts,
        source_permalink=match.source_permalink,
        match_type=match_type,
        score=score,
        text=text,
        claim_token=claim_token,
    )


def finalize_duplicate_alert(
    conn: sqlite3.Connection,
    claim: AlertClaim,
    alert_message_ts: str,
    *,
    now: float | None = None,
) -> bool:
    now = now if now is not None else time.time()
    cursor = conn.cursor()
    cursor.execute(
        """
        UPDATE link_duplicate_alerts
        SET alert_message_ts = ?, status = 'posted', claim_token = '', updated_at = ?
        WHERE current_channel = ? AND current_message_ts = ?
          AND status = 'claimed' AND claim_token = ?
        """,
        (
            alert_message_ts,
            now,
            claim.current_channel,
            claim.current_message_ts,
            claim.claim_token,
        ),
    )
    conn.commit()
    return cursor.rowcount == 1


def release_duplicate_alert(
    conn: sqlite3.Connection,
    claim: AlertClaim,
) -> bool:
    cursor = conn.cursor()
    cursor.execute("BEGIN IMMEDIATE")
    cursor.execute(
        """
        DELETE FROM link_duplicate_alerts
        WHERE current_channel = ? AND current_message_ts = ?
          AND status = 'claimed' AND claim_token = ?
        """,
        (claim.current_channel, claim.current_message_ts, claim.claim_token),
    )
    released = cursor.rowcount == 1
    if released:
        cursor.execute(
            """
            UPDATE message_links SET duplicate_checked_at = NULL
            WHERE channel = ? AND message_timestamp = ?
            """,
            (claim.current_channel, claim.current_message_ts),
        )
    conn.commit()
    return released


def mark_duplicate_alert_uncertain(
    conn: sqlite3.Connection,
    claim: AlertClaim,
    alert_message_ts: str,
    *,
    now: float | None = None,
) -> bool:
    """Persist a fail-closed delivery state when Slack cleanup is unconfirmed."""
    now = now if now is not None else time.time()
    try:
        conn.rollback()
    except sqlite3.Error:
        pass
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO link_duplicate_alerts
        (current_channel, current_message_ts, current_thread_ts,
         source_channel, source_message_ts, source_permalink,
         match_type, score, alert_message_ts, alert_text, status,
         claim_token, claimed_at, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'uncertain', ?, ?, ?, ?)
        ON CONFLICT(current_channel, current_message_ts) DO UPDATE SET
            alert_message_ts = CASE
                WHEN link_duplicate_alerts.status = 'posted'
                    THEN link_duplicate_alerts.alert_message_ts
                ELSE excluded.alert_message_ts
            END,
            status = CASE
                WHEN link_duplicate_alerts.status = 'posted' THEN 'posted'
                ELSE 'uncertain'
            END,
            updated_at = excluded.updated_at
        """,
        (
            claim.current_channel,
            claim.current_message_ts,
            claim.current_thread_ts,
            claim.source_channel,
            claim.source_message_ts,
            claim.source_permalink,
            claim.match_type,
            claim.score,
            alert_message_ts,
            claim.text,
            claim.claim_token,
            now,
            now,
            now,
        ),
    )
    conn.commit()
    return cursor.rowcount >= 1


def deliver_duplicate_alert(
    conn: sqlite3.Connection,
    claim: AlertClaim,
    *,
    post: Callable[[str, str], dict | None],
    delete: Callable[[str, str], object],
) -> bool:
    """Deliver one claimed alert with fail-closed post/finalize reconciliation.

    A claimed or uncertain row is intentionally never reclaimed automatically:
    after process interruption we cannot prove whether Slack accepted the post.
    Suppression is safer than emitting a duplicate alert.
    """
    try:
        result = post(claim.text, claim.current_thread_ts)
    except Exception:
        # A timeout/reset may happen after Slack accepted the mutation. Without
        # an authoritative idempotency key, releasing would permit a duplicate.
        try:
            mark_duplicate_alert_uncertain(conn, claim, "")
        except Exception:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
        raise

    alert_ts = result.get("ts") if result else None
    if not alert_ts:
        # A malformed/partial response is also ambiguous after an external
        # mutation. Preserve terminal suppression rather than blindly retrying.
        try:
            mark_duplicate_alert_uncertain(conn, claim, "")
        except Exception:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
        return False

    try:
        finalized = finalize_duplicate_alert(conn, claim, alert_ts)
    except Exception:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        try:
            delete(claim.current_channel, alert_ts)
        except Exception:
            # Preserve a terminal suppression row. If this update also fails,
            # the original claimed row remains the fail-closed state.
            try:
                mark_duplicate_alert_uncertain(conn, claim, alert_ts)
            except Exception:
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
            raise
        release_duplicate_alert(conn, claim)
        raise

    if finalized:
        return True

    # The claim disappeared or changed while Slack was posting. Remove only the
    # message created by this call. If cleanup fails, retain/restore a terminal
    # suppression row rather than allowing blind reposts.
    try:
        delete(claim.current_channel, alert_ts)
    except Exception:
        try:
            mark_duplicate_alert_uncertain(conn, claim, alert_ts)
        except Exception:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
        raise
    return False


def prepare_exact_duplicate_alert(
    conn: sqlite3.Connection,
    *,
    channel: str,
    message_timestamp: str,
    thread_ts: str,
    permalink: str,
    posted_at: float,
    links: list[ExternalLink],
    user_display_name: str,
    now: float | None = None,
) -> AlertClaim | None:
    """Record all links, queue enrichment, and reserve at most one exact alert."""
    for link in links:
        enqueue_link(
            conn,
            channel=channel,
            message_timestamp=message_timestamp,
            thread_ts=thread_ts,
            normalized_url=link.normalized_url,
            original_url=link.original_url,
            permalink=permalink,
            posted_at=posted_at,
            now=now,
        )

    matches = [
        match
        for link in links
        if (
            match := find_exact_duplicate(
                conn,
                normalized_url=link.normalized_url,
                current_channel=channel,
                current_message_ts=message_timestamp,
                current_thread_ts=thread_ts,
                posted_at=posted_at,
            )
        )
        is not None
    ]
    if not matches:
        return None

    match = max(matches, key=lambda candidate: candidate.source_posted_at)
    text = (
        f"Ciao {user_display_name}, *stesso link*: era già stato condiviso qui: "
        f"{match.source_permalink}"
    )
    return claim_duplicate_alert(
        conn,
        current_channel=channel,
        current_message_ts=message_timestamp,
        current_thread_ts=thread_ts,
        match=match,
        match_type="exact_url",
        text=text,
        now=now,
    )
