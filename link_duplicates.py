"""Deterministic and enriched duplicate-link orchestration.

This module owns database decisions and alert claims. Slack API calls remain in
archivebot.py so the behavior can be tested without importing or authenticating
the Slack application.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
import sqlite3
import time
from typing import Callable
from urllib.parse import urlsplit
import uuid

import numpy as np

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
    source_normalized_url: str
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
    current_normalized_url: str
    source_channel: str
    source_message_ts: str
    source_normalized_url: str
    source_permalink: str
    match_type: str
    score: float | None
    text: str
    claim_token: str


@dataclass(frozen=True)
class StoredAlert:
    current_channel: str
    current_message_ts: str
    alert_message_ts: str


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


def route_link_message_event(
    message: dict,
    normalize: Callable[[str], str],
    process: Callable[[list[ExternalLink]], None],
) -> bool:
    """Route every channel root/reply with external links before early returns."""
    if message.get("channel_type") == "im" or not message.get("channel"):
        return False
    links = extract_external_links(message.get("text", ""), normalize)
    if not links:
        return False
    process(links)
    return True


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
        SELECT ml.normalized_url, ml.normalized_url, ml.channel, ml.message_timestamp, ml.thread_ts,
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
    current_normalized_url: str,
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
    current_exists = cursor.execute(
        """
        SELECT 1 FROM message_links
        WHERE channel = ? AND message_timestamp = ? AND normalized_url = ?
          AND duplicate_checked_at IS NULL
          AND (? = 'exact_url' OR deterministic_checked_at IS NOT NULL)
        """,
        (
            current_channel,
            current_message_ts,
            current_normalized_url,
            match_type,
        ),
    ).fetchone()
    source_exists = cursor.execute(
        """
        SELECT 1 FROM message_links
        WHERE channel = ? AND message_timestamp = ? AND normalized_url = ?
        """,
        (
            match.source_channel,
            match.source_message_ts,
            match.source_normalized_url,
        ),
    ).fetchone()
    if current_exists is None or source_exists is None:
        conn.commit()
        return None
    cursor.execute(
        """
        INSERT OR IGNORE INTO link_duplicate_alerts
        (current_channel, current_message_ts, current_thread_ts, current_normalized_url,
         source_channel, source_message_ts, source_normalized_url, source_permalink,
         match_type, score, alert_text, status, claim_token,
         claimed_at, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'claimed', ?, ?, ?, ?)
        """,
        (
            current_channel,
            current_message_ts,
            current_thread_ts,
            current_normalized_url,
            match.source_channel,
            match.source_message_ts,
            match.source_normalized_url,
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
        UPDATE message_links
        SET deterministic_checked_at = COALESCE(deterministic_checked_at, ?),
            duplicate_checked_at = ?
        WHERE channel = ? AND message_timestamp = ?
        """,
        (now, now, current_channel, current_message_ts),
    )
    conn.commit()
    return AlertClaim(
        current_channel=current_channel,
        current_message_ts=current_message_ts,
        current_thread_ts=current_thread_ts,
        current_normalized_url=current_normalized_url,
        source_channel=match.source_channel,
        source_message_ts=match.source_message_ts,
        source_normalized_url=match.source_normalized_url,
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
        (current_channel, current_message_ts, current_thread_ts, current_normalized_url,
         source_channel, source_message_ts, source_normalized_url, source_permalink,
         match_type, score, alert_message_ts, alert_text, status,
         claim_token, claimed_at, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'uncertain', ?, ?, ?, ?)
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
            claim.current_normalized_url,
            claim.source_channel,
            claim.source_message_ts,
            claim.source_normalized_url,
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
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            UPDATE message_links SET deterministic_checked_at = ?
            WHERE channel = ? AND message_timestamp = ?
              AND deterministic_checked_at IS NULL
            """,
            (now if now is not None else time.time(), channel, message_timestamp),
        )
        conn.commit()
        return None

    match = max(matches, key=lambda candidate: candidate.source_posted_at)
    text = (
        f"Ciao {user_display_name}, *stesso link*: era già stato condiviso qui: "
        f"{match.source_permalink}"
    )
    claim = claim_duplicate_alert(
        conn,
        current_channel=channel,
        current_message_ts=message_timestamp,
        current_thread_ts=thread_ts,
        current_normalized_url=match.normalized_url,
        match=match,
        match_type="exact_url",
        text=text,
        now=now,
    )
    if claim is None:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            UPDATE message_links SET deterministic_checked_at = ?
            WHERE channel = ? AND message_timestamp = ?
              AND deterministic_checked_at IS NULL
            """,
            (now if now is not None else time.time(), channel, message_timestamp),
        )
        conn.commit()
    return claim


def _cosine_similarity(left: bytes | None, right: bytes | None) -> float | None:
    if not left or not right:
        return None
    left_array = np.frombuffer(left, dtype=np.float32)
    right_array = np.frombuffer(right, dtype=np.float32)
    if left_array.size == 0 or left_array.shape != right_array.shape:
        return None
    denominator = float(np.linalg.norm(left_array) * np.linalg.norm(right_array))
    if denominator == 0:
        return None
    return float(np.dot(left_array, right_array) / denominator)


def _has_pending_prior_documents(
    conn: sqlite3.Connection,
    *,
    current_channel: str,
    current_normalized_url: str,
    posted_at: float,
) -> bool:
    row = conn.execute(
        """
        SELECT 1
        FROM message_links prior
        JOIN channels prior_channel ON prior_channel.id = prior.channel
        JOIN link_enrichment_jobs job ON job.normalized_url = prior.normalized_url
        WHERE prior.posted_at >= ? AND prior.posted_at < ?
          AND prior.channel != ?
          AND prior.normalized_url != ?
          AND prior_channel.is_private = 0
          AND job.status IN ('pending', 'retry', 'processing')
        LIMIT 1
        """,
        (
            posted_at - DUPLICATE_WINDOW_SECONDS,
            posted_at,
            current_channel,
            current_normalized_url,
        ),
    ).fetchone()
    return row is not None


def _match_to_json(match: DuplicateMatch) -> dict:
    return {
        "normalized_url": match.normalized_url,
        "source_normalized_url": match.source_normalized_url,
        "source_channel": match.source_channel,
        "source_message_ts": match.source_message_ts,
        "source_thread_ts": match.source_thread_ts,
        "source_permalink": match.source_permalink,
        "source_posted_at": match.source_posted_at,
    }


def _match_from_json(payload: dict | None) -> DuplicateMatch | None:
    return DuplicateMatch(**payload) if payload else None


def _claim_or_create_match_scan(
    conn: sqlite3.Connection,
    *,
    now: float,
    stale_after_seconds: float = 60.0,
) -> tuple | None:
    token = uuid.uuid4().hex
    cursor = conn.cursor()
    cursor.execute("BEGIN IMMEDIATE")
    row = cursor.execute(
        """
        SELECT current_channel, current_message_ts, current_normalized_url,
               candidate_after_rowid, state_json
        FROM link_match_scans
        WHERE claim_token = '' OR claimed_at < ?
        ORDER BY created_at
        LIMIT 1
        """,
        (now - stale_after_seconds,),
    ).fetchone()
    if row is None:
        current = cursor.execute(
            """
            SELECT ml.channel, ml.message_timestamp, ml.normalized_url
            FROM message_links ml
            JOIN channels channel
              ON channel.id = ml.channel AND channel.is_private = 0
            JOIN link_documents document
              ON document.normalized_url = ml.normalized_url
             AND document.fetch_status = 'complete'
             AND document.extraction_quality IN ('full_text', 'metadata_only')
            WHERE ml.deterministic_checked_at IS NOT NULL
              AND ml.duplicate_checked_at IS NULL
              AND NOT EXISTS (
                  SELECT 1 FROM link_match_scans scan
                  WHERE scan.current_channel = ml.channel
                    AND scan.current_message_ts = ml.message_timestamp
                    AND scan.current_normalized_url = ml.normalized_url
              )
            ORDER BY ml.posted_at
            LIMIT 1
            """
        ).fetchone()
        if current is None:
            conn.commit()
            return None
        cursor.execute(
            """
            INSERT OR IGNORE INTO link_match_scans
            (current_channel, current_message_ts, current_normalized_url,
             candidate_after_rowid, state_json, claim_token, claimed_at,
             created_at, updated_at)
            VALUES (?, ?, ?, 0, '{}', '', NULL, ?, ?)
            """,
            (*current, now, now),
        )
        row = (*current, 0, "{}")

    cursor.execute(
        """
        UPDATE link_match_scans
        SET claim_token = ?, claimed_at = ?, updated_at = ?
        WHERE current_channel = ? AND current_message_ts = ?
          AND current_normalized_url = ?
          AND (claim_token = '' OR claimed_at < ?)
        """,
        (
            token,
            now,
            now,
            row[0],
            row[1],
            row[2],
            now - stale_after_seconds,
        ),
    )
    if cursor.rowcount != 1:
        conn.commit()
        return None
    conn.commit()
    return (*row, token)


def _delete_match_scan(conn: sqlite3.Connection, scan: tuple) -> None:
    conn.execute(
        """
        DELETE FROM link_match_scans
        WHERE current_channel = ? AND current_message_ts = ?
          AND current_normalized_url = ? AND claim_token = ?
        """,
        (scan[0], scan[1], scan[2], scan[5]),
    )
    conn.commit()


def prepare_enriched_duplicate_alerts(
    conn: sqlite3.Connection,
    *,
    similarity_threshold: float = 0.92,
    now: float | None = None,
    max_current_rows: int = 10,
    max_candidate_comparisons: int = 100,
) -> list[AlertClaim]:
    """Advance resumable same-content/story scans within one work budget."""
    if not 0.0 <= similarity_threshold <= 1.0:
        raise ValueError("similarity_threshold must be between 0 and 1")
    if max_current_rows <= 0 or max_candidate_comparisons <= 0:
        raise ValueError("scan work budgets must be positive")
    now = now if now is not None else time.time()
    claims: list[AlertClaim] = []
    comparisons_left = max_candidate_comparisons

    for _ in range(max_current_rows):
        if comparisons_left <= 0:
            break
        scan = _claim_or_create_match_scan(conn, now=now)
        if scan is None:
            break
        channel, message_ts, normalized_url, after_rowid, state_json, token = scan
        current = conn.execute(
            """
            SELECT ml.thread_ts, ml.posted_at, document.content,
                   document.content_hash, document.embedding,
                   document.extraction_quality
            FROM message_links ml
            JOIN channels channel
              ON channel.id = ml.channel AND channel.is_private = 0
            JOIN link_documents document
              ON document.normalized_url = ml.normalized_url
             AND document.fetch_status = 'complete'
            WHERE ml.channel = ? AND ml.message_timestamp = ?
              AND ml.normalized_url = ?
              AND ml.deterministic_checked_at IS NOT NULL
              AND ml.duplicate_checked_at IS NULL
            """,
            (channel, message_ts, normalized_url),
        ).fetchone()
        if current is None:
            _delete_match_scan(conn, scan)
            continue

        thread_ts, posted_at, current_content, current_hash, current_embedding, current_quality = current
        page_size = comparisons_left
        candidates = conn.execute(
            """
            SELECT prior.rowid, prior.normalized_url, prior.channel,
                   prior.message_timestamp, prior.thread_ts, prior.permalink,
                   prior.posted_at, document.content, document.content_hash,
                   document.embedding, document.extraction_quality,
                   document.fetch_status, prior_channel.is_private
            FROM message_links prior
            LEFT JOIN channels prior_channel ON prior_channel.id = prior.channel
            LEFT JOIN link_documents document
              ON document.normalized_url = prior.normalized_url
            WHERE prior.rowid > ?
            ORDER BY prior.rowid
            LIMIT ?
            """,
            (
                after_rowid,
                page_size + 1,
            ),
        ).fetchall()
        has_more = len(candidates) > page_size
        page = candidates[:page_size]
        comparisons_left -= len(page)
        state = json.loads(state_json or "{}")
        content_match = _match_from_json(state.get("content_match"))
        semantic_payload = state.get("semantic_match")
        semantic_match = (
            (float(semantic_payload["score"]), _match_from_json(semantic_payload["match"]))
            if semantic_payload
            else None
        )

        for (
            rowid,
            source_normalized_url,
            source_channel,
            source_message_ts,
            source_thread_ts,
            source_permalink,
            source_posted_at,
            source_content,
            source_hash,
            source_embedding,
            source_quality,
            source_fetch_status,
            source_is_private,
        ) in page:
            if not (
                posted_at - DUPLICATE_WINDOW_SECONDS <= source_posted_at < posted_at
                and source_channel != channel
                and source_normalized_url != normalized_url
                and source_permalink
                and source_is_private == 0
                and source_fetch_status == "complete"
                and source_quality in {"full_text", "metadata_only"}
            ):
                continue
            match = DuplicateMatch(
                normalized_url=normalized_url,
                source_normalized_url=source_normalized_url,
                source_channel=source_channel,
                source_message_ts=source_message_ts,
                source_thread_ts=source_thread_ts,
                source_permalink=source_permalink,
                source_posted_at=source_posted_at,
            )
            if (
                current_quality == "full_text"
                and source_quality == "full_text"
                and current_content
                and source_content
                and current_hash
                and current_hash == source_hash
            ):
                if content_match is None or match.source_posted_at > content_match.source_posted_at:
                    content_match = match
                continue
            score = _cosine_similarity(current_embedding, source_embedding)
            required = max(similarity_threshold, 0.97) if (
                current_quality == "metadata_only" or source_quality == "metadata_only"
            ) else similarity_threshold
            if score is not None and score >= required:
                candidate_key = (score, match.source_posted_at)
                existing_key = (
                    (semantic_match[0], semantic_match[1].source_posted_at)
                    if semantic_match is not None
                    else None
                )
                if existing_key is None or candidate_key > existing_key:
                    semantic_match = (score, match)

        if has_more:
            state = {
                "content_match": _match_to_json(content_match) if content_match else None,
                "semantic_match": (
                    {"score": semantic_match[0], "match": _match_to_json(semantic_match[1])}
                    if semantic_match
                    else None
                ),
            }
            conn.execute(
                """
                UPDATE link_match_scans
                SET candidate_after_rowid = ?, state_json = ?,
                    claim_token = '', claimed_at = NULL, updated_at = ?
                WHERE current_channel = ? AND current_message_ts = ?
                  AND current_normalized_url = ? AND claim_token = ?
                """,
                (
                    page[-1][0],
                    json.dumps(state, separators=(",", ":")),
                    now,
                    channel,
                    message_ts,
                    normalized_url,
                    token,
                ),
            )
            conn.commit()
            break

        claim = None
        if content_match is not None:
            claim = claim_duplicate_alert(
                conn,
                current_channel=channel,
                current_message_ts=message_ts,
                current_thread_ts=thread_ts,
                current_normalized_url=normalized_url,
                match=content_match,
                match_type="same_content",
                text=(
                    "⚠️ *Stesso contenuto, URL diverso*: questo documento sembra "
                    f"già condiviso qui: {content_match.source_permalink}"
                ),
                score=1.0,
                now=now,
            )
        elif semantic_match is not None:
            score, match = semantic_match
            claim = claim_duplicate_alert(
                conn,
                current_channel=channel,
                current_message_ts=message_ts,
                current_thread_ts=thread_ts,
                current_normalized_url=normalized_url,
                match=match,
                match_type="same_story",
                text=(
                    "⚠️ *Potenzialmente la stessa storia*: un link simile è stato "
                    f"condiviso qui: {match.source_permalink}\n"
                    f"_Similarità {score:.0%} · fonte diversa_"
                ),
                score=score,
                now=now,
            )

        _delete_match_scan(conn, scan)
        if claim is not None:
            claims.append(claim)
        elif content_match is not None or semantic_match is not None:
            # A source/current association changed after the scan. Rescan the
            # still-unchecked row instead of discarding the next-best evidence.
            continue
        elif not _has_pending_prior_documents(
            conn,
            current_channel=channel,
            current_normalized_url=normalized_url,
            posted_at=posted_at,
        ):
            conn.execute(
                """
                UPDATE message_links SET duplicate_checked_at = ?
                WHERE channel = ? AND message_timestamp = ?
                  AND normalized_url = ? AND deterministic_checked_at IS NOT NULL
                  AND duplicate_checked_at IS NULL
                """,
                (now, channel, message_ts, normalized_url),
            )
            conn.commit()
    return claims


def _cancel_unreferenced_jobs(conn: sqlite3.Connection, normalized_urls: set[str]) -> None:
    for normalized_url in normalized_urls:
        referenced = conn.execute(
            "SELECT 1 FROM message_links WHERE normalized_url = ? LIMIT 1",
            (normalized_url,),
        ).fetchone()
        if referenced is None:
            conn.execute(
                "DELETE FROM link_enrichment_jobs WHERE normalized_url = ?",
                (normalized_url,),
            )


def reconcile_edited_message_links(
    conn: sqlite3.Connection,
    *,
    channel: str,
    message_timestamp: str,
    active_normalized_urls: set[str],
) -> list[StoredAlert]:
    """Remove links dropped by an edit and return alerts that became obsolete."""
    existing = {
        row[0]
        for row in conn.execute(
            "SELECT normalized_url FROM message_links WHERE channel = ? AND message_timestamp = ?",
            (channel, message_timestamp),
        ).fetchall()
    }
    removed = existing - active_normalized_urls
    if removed:
        placeholders = ",".join("?" for _ in removed)
        conn.execute(
            f"""
            DELETE FROM message_links
            WHERE channel = ? AND message_timestamp = ?
              AND normalized_url IN ({placeholders})
            """,
            (channel, message_timestamp, *sorted(removed)),
        )
        _cancel_unreferenced_jobs(conn, removed)
        conn.execute(
            f"""
            DELETE FROM link_match_scans
            WHERE current_channel = ? AND current_message_ts = ?
              AND current_normalized_url IN ({placeholders})
            """,
            (channel, message_timestamp, *sorted(removed)),
        )

    alerts = [
        StoredAlert(*row)
        for row in conn.execute(
            """
            SELECT current_channel, current_message_ts, alert_message_ts
            FROM link_duplicate_alerts
            WHERE (
                current_channel = ? AND current_message_ts = ?
                AND current_normalized_url != ''
                AND current_normalized_url NOT IN (
                    SELECT normalized_url FROM message_links
                    WHERE channel = ? AND message_timestamp = ?
                )
            ) OR (
                source_channel = ? AND source_message_ts = ?
                AND source_normalized_url != ''
                AND source_normalized_url NOT IN (
                    SELECT normalized_url FROM message_links
                    WHERE channel = ? AND message_timestamp = ?
                )
            )
            """,
            (
                channel,
                message_timestamp,
                channel,
                message_timestamp,
                channel,
                message_timestamp,
                channel,
                message_timestamp,
            ),
        ).fetchall()
    ]
    conn.commit()
    return alerts


def collect_deleted_message_alerts(
    conn: sqlite3.Connection,
    *,
    channel: str,
    message_timestamp: str,
) -> list[StoredAlert]:
    """Remove message-link state and return current/source alerts for Slack cleanup."""
    alerts = [
        StoredAlert(*row)
        for row in conn.execute(
            """
            SELECT current_channel, current_message_ts, alert_message_ts
            FROM link_duplicate_alerts
            WHERE (current_channel = ? AND current_message_ts = ?)
               OR (source_channel = ? AND source_message_ts = ?)
            """,
            (channel, message_timestamp, channel, message_timestamp),
        ).fetchall()
    ]
    normalized_urls = {
        row[0]
        for row in conn.execute(
            "SELECT normalized_url FROM message_links WHERE channel = ? AND message_timestamp = ?",
            (channel, message_timestamp),
        ).fetchall()
    }
    conn.execute(
        "DELETE FROM message_links WHERE channel = ? AND message_timestamp = ?",
        (channel, message_timestamp),
    )
    conn.execute(
        "DELETE FROM link_match_scans WHERE current_channel = ? AND current_message_ts = ?",
        (channel, message_timestamp),
    )
    _cancel_unreferenced_jobs(conn, normalized_urls)
    conn.commit()
    return alerts


def finalize_stored_alert_cleanup(
    conn: sqlite3.Connection,
    alert: StoredAlert,
    *,
    deleted: bool,
) -> None:
    if deleted:
        conn.execute(
            """
            DELETE FROM link_duplicate_alerts
            WHERE current_channel = ? AND current_message_ts = ?
            """,
            (alert.current_channel, alert.current_message_ts),
        )
    else:
        conn.execute(
            """
            UPDATE link_duplicate_alerts SET status = 'orphaned', updated_at = ?
            WHERE current_channel = ? AND current_message_ts = ?
            """,
            (time.time(), alert.current_channel, alert.current_message_ts),
        )
    conn.commit()
