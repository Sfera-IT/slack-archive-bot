"""Bounded, durable archiving of public Instagram post media into Slack."""

from __future__ import annotations

from dataclasses import dataclass, replace
import logging
import math
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import threading
import time
from typing import Callable, Iterable
from urllib.parse import urlsplit
import uuid

import httpx

from link_enrichment import (
    EnrichmentError,
    FetchPolicy,
    _bounded_dns_resolver,
    _pinned_request_target,
    _validate_fetch_destination,
)


_URL_PATTERN = re.compile(r'https?://[^\s<>"{}|\\^`\[\]]+', re.IGNORECASE)
_INSTAGRAM_PATH_PATTERN = re.compile(
    r"^/(reel|p|tv)/([A-Za-z0-9_-]+)/?$", re.IGNORECASE
)
_CONTENT_TYPE_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "video/mp4": ".mp4",
}
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}


class MediaDownloadError(Exception):
    """A bounded media download failed safely."""


class MediaExtractionError(Exception):
    """Instagram did not expose a bounded public-media representation."""


@dataclass(frozen=True)
class InstagramMediaPolicy:
    connect_timeout: float = 5.0
    read_timeout: float = 15.0
    total_timeout: float = 30.0
    max_file_bytes: int = 50 * 1024 * 1024
    max_total_bytes: int = 100 * 1024 * 1024
    max_items: int = 10
    max_redirects: int = 5

    @classmethod
    def from_env(cls) -> "InstagramMediaPolicy":
        def positive_float(name: str, default: float) -> float:
            try:
                value = float(os.getenv(name, str(default)))
                if not math.isfinite(value) or value <= 0:
                    raise ValueError
                return value
            except (TypeError, ValueError):
                return default

        def positive_int(name: str, default: int) -> int:
            try:
                value = int(os.getenv(name, str(default)))
                if value <= 0:
                    raise ValueError
                return value
            except (TypeError, ValueError):
                return default

        return cls(
            connect_timeout=positive_float(
                "INSTAGRAM_MEDIA_CONNECT_TIMEOUT_SECONDS", 5.0
            ),
            read_timeout=positive_float(
                "INSTAGRAM_MEDIA_READ_TIMEOUT_SECONDS", 15.0
            ),
            total_timeout=positive_float(
                "INSTAGRAM_MEDIA_TOTAL_TIMEOUT_SECONDS", 30.0
            ),
            max_file_bytes=positive_int(
                "INSTAGRAM_MEDIA_MAX_FILE_BYTES", 50 * 1024 * 1024
            ),
            max_total_bytes=positive_int(
                "INSTAGRAM_MEDIA_MAX_TOTAL_BYTES", 100 * 1024 * 1024
            ),
            max_items=positive_int("INSTAGRAM_MEDIA_MAX_ITEMS", 10),
            max_redirects=positive_int("INSTAGRAM_MEDIA_MAX_REDIRECTS", 5),
        )


@dataclass(frozen=True)
class RemoteMedia:
    url: str
    index: int
    is_video: bool


@dataclass(frozen=True)
class DownloadedMedia:
    path: Path
    index: int
    is_video: bool
    size: int


@dataclass(frozen=True)
class InstagramMediaJob:
    channel: str
    message_timestamp: str
    thread_ts: str
    author: str
    instagram_url: str
    shortcode: str
    attempts: int
    claim_token: str


def extract_instagram_post_urls(text: str) -> list[str]:
    """Return canonical public Instagram post/reel URLs in message order."""
    if not text:
        return []
    urls: list[str] = []
    seen: set[str] = set()
    for match in _URL_PATTERN.finditer(text):
        raw_url = match.group(0).rstrip(".,;:!?)\"]}")
        parsed = urlsplit(raw_url)
        if parsed.scheme.lower() != "https":
            continue
        if parsed.username is not None or parsed.password is not None:
            continue
        try:
            if parsed.port is not None:
                continue
        except ValueError:
            continue
        host = (parsed.hostname or "").lower()
        if host not in {"instagram.com", "www.instagram.com"}:
            continue
        path_match = _INSTAGRAM_PATH_PATTERN.match(parsed.path.rstrip("/") + "/")
        if not path_match:
            continue
        kind, shortcode = path_match.groups()
        canonical = f"https://www.instagram.com/{kind.lower()}/{shortcode}/"
        if canonical not in seen:
            seen.add(canonical)
            urls.append(canonical)
    return urls


def _shortcode_from_url(url: str) -> str:
    match = _INSTAGRAM_PATH_PATTERN.match(urlsplit(url).path)
    if not match:
        raise ValueError("Unsupported Instagram post URL")
    return match.group(2)


def enqueue_instagram_media(
    conn: sqlite3.Connection,
    *,
    channel: str,
    message_timestamp: str,
    thread_ts: str,
    instagram_url: str,
    author: str = "",
    now: float | None = None,
) -> bool:
    """Record one source and ensure one thread-level media job exists."""
    shortcode = _shortcode_from_url(instagram_url)
    now = time.time() if now is None else now
    conn.execute("BEGIN IMMEDIATE")
    try:
        cursor = conn.execute(
            """
            INSERT INTO instagram_media_jobs
            (channel, message_timestamp, thread_ts, author, instagram_url, shortcode,
             status, attempts, next_attempt_at)
            VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, ?)
            ON CONFLICT(channel, thread_ts, shortcode) DO UPDATE SET
                status = 'pending', attempts = 0,
                next_attempt_at = excluded.next_attempt_at,
                claimed_at = NULL, claim_token = NULL, last_error = NULL
            WHERE instagram_media_jobs.status = 'cancelled'
            """,
            (
                channel,
                message_timestamp,
                thread_ts,
                author,
                instagram_url,
                shortcode,
                now,
            ),
        )
        conn.execute(
            """
            INSERT INTO instagram_media_sources
                (channel, message_timestamp, thread_ts, author, instagram_url, shortcode)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(channel, message_timestamp, shortcode) DO UPDATE SET
                thread_ts = excluded.thread_ts,
                author = excluded.author,
                instagram_url = excluded.instagram_url
            """,
            (channel, message_timestamp, thread_ts, author, instagram_url, shortcode),
        )
        conn.commit()
        return cursor.rowcount == 1
    except Exception:
        conn.rollback()
        raise


def cancel_instagram_media_jobs_for_message(
    conn: sqlite3.Connection,
    *,
    channel: str,
    message_timestamp: str,
) -> int:
    """Remove one source and cancel jobs only when no source remains."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        identities = conn.execute(
            """
            SELECT DISTINCT thread_ts, shortcode
            FROM instagram_media_sources
            WHERE channel = ? AND message_timestamp = ?
            """,
            (channel, message_timestamp),
        ).fetchall()
        conn.execute(
            """
            DELETE FROM instagram_media_sources
            WHERE channel = ? AND message_timestamp = ?
            """,
            (channel, message_timestamp),
        )
        cancelled = 0
        for thread_ts, shortcode in identities:
            cancelled += conn.execute(
                """
                UPDATE instagram_media_jobs
                SET status = 'cancelled', claimed_at = NULL, claim_token = NULL,
                    last_error = 'Last source message was deleted'
                WHERE channel = ? AND thread_ts = ? AND shortcode = ?
                  AND status IN ('pending', 'processing')
                  AND NOT EXISTS (
                      SELECT 1 FROM instagram_media_sources AS source
                      WHERE source.channel = instagram_media_jobs.channel
                        AND source.thread_ts = instagram_media_jobs.thread_ts
                        AND source.shortcode = instagram_media_jobs.shortcode
                  )
                """,
                (channel, thread_ts, shortcode),
            ).rowcount
        conn.commit()
        return cancelled
    except Exception:
        conn.rollback()
        raise


def reconcile_instagram_media_jobs(
    conn: sqlite3.Connection,
    *,
    channel: str,
    message_timestamp: str,
    thread_ts: str,
    author: str,
    instagram_urls: Iterable[str],
    now: float | None = None,
) -> tuple[int, int]:
    """Reconcile publish-safe jobs with the current links in one source message."""
    now = time.time() if now is None else now
    urls = list(instagram_urls)
    active_shortcodes = {_shortcode_from_url(url) for url in urls}
    conn.execute("BEGIN IMMEDIATE")
    try:
        if active_shortcodes:
            placeholders = ", ".join("?" for _ in active_shortcodes)
            removed_identities = conn.execute(
                f"""
                SELECT DISTINCT thread_ts, shortcode
                FROM instagram_media_sources
                WHERE channel = ? AND message_timestamp = ?
                  AND shortcode NOT IN ({placeholders})
                """,
                (channel, message_timestamp, *sorted(active_shortcodes)),
            ).fetchall()
            conn.execute(
                f"""
                DELETE FROM instagram_media_sources
                WHERE channel = ? AND message_timestamp = ?
                  AND shortcode NOT IN ({placeholders})
                """,
                (channel, message_timestamp, *sorted(active_shortcodes)),
            )
        else:
            removed_identities = conn.execute(
                """
                SELECT DISTINCT thread_ts, shortcode
                FROM instagram_media_sources
                WHERE channel = ? AND message_timestamp = ?
                """,
                (channel, message_timestamp),
            ).fetchall()
            conn.execute(
                """
                DELETE FROM instagram_media_sources
                WHERE channel = ? AND message_timestamp = ?
                """,
                (channel, message_timestamp),
            )

        queued = 0
        for instagram_url in urls:
            shortcode = _shortcode_from_url(instagram_url)
            conn.execute(
                """
                INSERT INTO instagram_media_sources
                    (channel, message_timestamp, thread_ts, author,
                     instagram_url, shortcode)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(channel, message_timestamp, shortcode) DO UPDATE SET
                    thread_ts = excluded.thread_ts,
                    author = excluded.author,
                    instagram_url = excluded.instagram_url
                """,
                (channel, message_timestamp, thread_ts, author, instagram_url, shortcode),
            )
            cursor = conn.execute(
                """
                INSERT INTO instagram_media_jobs
                (channel, message_timestamp, thread_ts, author, instagram_url,
                 shortcode, status, attempts, next_attempt_at)
                VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, ?)
                ON CONFLICT(channel, thread_ts, shortcode) DO UPDATE SET
                    status = 'pending',
                    attempts = 0,
                    next_attempt_at = excluded.next_attempt_at,
                    claimed_at = NULL,
                    claim_token = NULL,
                    last_error = NULL
                WHERE instagram_media_jobs.status = 'cancelled'
                """,
                (
                    channel,
                    message_timestamp,
                    thread_ts,
                    author,
                    instagram_url,
                    shortcode,
                    now,
                ),
            )
            queued += cursor.rowcount

        cancelled = 0
        for removed_thread_ts, removed_shortcode in removed_identities:
            cancelled += conn.execute(
                """
                UPDATE instagram_media_jobs
                SET status = 'cancelled', claimed_at = NULL, claim_token = NULL,
                    last_error = 'Last Instagram source link was removed'
                WHERE channel = ? AND thread_ts = ? AND shortcode = ?
                  AND status IN ('pending', 'processing')
                  AND NOT EXISTS (
                      SELECT 1 FROM instagram_media_sources AS source
                      WHERE source.channel = instagram_media_jobs.channel
                        AND source.thread_ts = instagram_media_jobs.thread_ts
                        AND source.shortcode = instagram_media_jobs.shortcode
                  )
                """,
                (channel, removed_thread_ts, removed_shortcode),
            ).rowcount
        conn.commit()
        return queued, cancelled
    except Exception:
        conn.rollback()
        raise


def claim_instagram_media_job(
    conn: sqlite3.Connection,
    *,
    now: float | None = None,
    stale_after_seconds: float = 300.0,
    max_attempts: int = 3,
) -> InstagramMediaJob | None:
    """Atomically claim one pending/retryable job across worker processes."""
    now = time.time() if now is None else now
    token = uuid.uuid4().hex
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            """
            UPDATE instagram_media_jobs
            SET status = 'failed', claim_token = NULL, claimed_at = NULL,
                last_error = 'Worker lease expired at attempt limit'
            WHERE status = 'processing' AND claimed_at <= ? AND attempts >= ?
            """,
            (now - stale_after_seconds, max_attempts),
        )
        conn.execute(
            """
            UPDATE instagram_media_jobs
            SET status = 'pending', claim_token = NULL, claimed_at = NULL
            WHERE status = 'processing' AND claimed_at <= ? AND attempts < ?
            """,
            (now - stale_after_seconds, max_attempts),
        )
        row = conn.execute(
            """
            SELECT channel, message_timestamp, thread_ts, author, instagram_url,
                   shortcode, attempts
            FROM instagram_media_jobs
            WHERE status = 'pending' AND next_attempt_at <= ? AND attempts < ?
            ORDER BY next_attempt_at, rowid
            LIMIT 1
            """,
            (now, max_attempts),
        ).fetchone()
        if row is None:
            conn.commit()
            return None
        updated = conn.execute(
            """
            UPDATE instagram_media_jobs
            SET status = 'processing', attempts = attempts + 1,
                claimed_at = ?, claim_token = ?
            WHERE channel = ? AND thread_ts = ? AND shortcode = ?
              AND status = 'pending'
            """,
            (now, token, row[0], row[2], row[5]),
        )
        if updated.rowcount != 1:
            conn.rollback()
            return None
        conn.commit()
        return InstagramMediaJob(
            channel=row[0],
            message_timestamp=row[1],
            thread_ts=row[2],
            author=row[3],
            instagram_url=row[4],
            shortcode=row[5],
            attempts=row[6] + 1,
            claim_token=token,
        )
    except Exception:
        conn.rollback()
        raise


def mark_instagram_job_uploading(
    conn: sqlite3.Connection,
    job: InstagramMediaJob,
    *,
    now: float | None = None,
) -> bool:
    """Enter the no-retry publication state while still owning the claim."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        updated = conn.execute(
            """
            UPDATE instagram_media_jobs
            SET status = 'uploading', claimed_at = ?
            WHERE channel = ? AND thread_ts = ? AND shortcode = ?
              AND status = 'processing' AND claim_token = ?
              AND EXISTS (
                  SELECT 1
                  FROM instagram_media_sources AS source
                  WHERE source.channel = instagram_media_jobs.channel
                    AND source.thread_ts = instagram_media_jobs.thread_ts
                    AND source.shortcode = instagram_media_jobs.shortcode
                    AND source.author != ''
                    AND NOT EXISTS (
                        SELECT 1 FROM optout WHERE user = source.author
                    )
              )
            """,
            (
                time.time() if now is None else now,
                job.channel,
                job.thread_ts,
                job.shortcode,
                job.claim_token,
            ),
        )
        if updated.rowcount != 1:
            conn.execute(
                """
                UPDATE instagram_media_jobs
                SET status = 'cancelled', claimed_at = NULL, claim_token = NULL,
                    last_error = 'Source author opted out before publication'
                WHERE channel = ? AND thread_ts = ? AND shortcode = ?
                  AND status = 'processing' AND claim_token = ?
                  AND NOT EXISTS (
                      SELECT 1
                      FROM instagram_media_sources AS source
                      WHERE source.channel = instagram_media_jobs.channel
                        AND source.thread_ts = instagram_media_jobs.thread_ts
                        AND source.shortcode = instagram_media_jobs.shortcode
                        AND source.author != ''
                        AND NOT EXISTS (
                            SELECT 1 FROM optout WHERE user = source.author
                        )
                  )
                """,
                (
                    job.channel,
                    job.thread_ts,
                    job.shortcode,
                    job.claim_token,
                ),
            )
        conn.commit()
        return updated.rowcount == 1
    except Exception:
        conn.rollback()
        raise


def download_remote_media(
    media: RemoteMedia,
    *,
    shortcode: str,
    output_dir: str | os.PathLike[str],
    policy: InstagramMediaPolicy | None = None,
    client: httpx.Client | None = None,
    resolver: Callable[..., Iterable[tuple]] = _bounded_dns_resolver,
    clock: Callable[[], float] = time.monotonic,
) -> DownloadedMedia:
    """Download one resolved media URL with SSRF, deadline, and byte guards."""
    policy = policy or InstagramMediaPolicy.from_env()
    deadline = clock() + policy.total_timeout
    owns_client = client is None
    client = client or httpx.Client(follow_redirects=False, trust_env=False)
    current_url = media.url
    fetch_policy = FetchPolicy(
        connect_timeout=policy.connect_timeout,
        read_timeout=policy.read_timeout,
        total_timeout=policy.total_timeout,
        max_response_bytes=policy.max_file_bytes,
        max_redirects=policy.max_redirects,
    )
    try:
        for redirect_count in range(policy.max_redirects + 1):
            def deadline_resolver(host, port, **kwargs):
                try:
                    return resolver(host, port, **kwargs)
                except TypeError as exc:
                    if "unexpected keyword argument 'timeout'" not in str(exc):
                        raise
                    kwargs.pop("timeout", None)
                    return resolver(host, port, **kwargs)

            try:
                safe_url, addresses = _validate_fetch_destination(
                    current_url,
                    fetch_policy,
                    resolver=deadline_resolver,
                    deadline=deadline,
                    clock=clock,
                )
            except EnrichmentError as exc:
                raise MediaDownloadError(str(exc)) from exc
            response = None
            last_request_error = None
            for address in addresses:
                pinned_url, host_header, sni_hostname = _pinned_request_target(
                    safe_url, address
                )
                remaining = deadline - clock()
                if remaining <= 0:
                    raise MediaDownloadError(
                        "Media download exceeded total deadline"
                    )
                request = client.build_request(
                    "GET",
                    pinned_url,
                    headers={"Host": host_header},
                    timeout=httpx.Timeout(
                        min(policy.read_timeout, remaining),
                        connect=min(policy.connect_timeout, remaining),
                    ),
                )
                request.extensions["sni_hostname"] = sni_hostname
                try:
                    response = client.send(request, stream=True)
                    break
                except httpx.HTTPError as exc:
                    last_request_error = exc
            if response is None:
                raise MediaDownloadError(
                    f"Media request failed: {last_request_error}"
                ) from last_request_error
            try:
                if response.status_code in _REDIRECT_STATUSES:
                    location = response.headers.get("location")
                    if not location or redirect_count >= policy.max_redirects:
                        raise MediaDownloadError("Media redirect limit exceeded")
                    current_url = str(httpx.URL(safe_url).join(location))
                    continue
                if response.status_code < 200 or response.status_code >= 300:
                    raise MediaDownloadError(
                        f"Media request returned HTTP {response.status_code}"
                    )
                content_type = (
                    response.headers.get("content-type", "")
                    .split(";", 1)[0]
                    .strip()
                    .lower()
                )
                extension = _CONTENT_TYPE_EXTENSIONS.get(content_type)
                if extension is None:
                    raise MediaDownloadError(
                        f"Unsupported media content type: {content_type or 'missing'}"
                    )
                output_path = Path(output_dir) / (
                    f"instagram-{shortcode}-{media.index:02d}{extension}"
                )
                total = 0
                with output_path.open("wb") as destination:
                    for chunk in response.iter_bytes():
                        if clock() > deadline:
                            raise MediaDownloadError(
                                "Media download exceeded total deadline"
                            )
                        total += len(chunk)
                        if total > policy.max_file_bytes:
                            raise MediaDownloadError(
                                "Media exceeded configured byte limit"
                            )
                        destination.write(chunk)
                return DownloadedMedia(
                    path=output_path,
                    index=media.index,
                    is_video=media.is_video,
                    size=total,
                )
            finally:
                response.close()
    finally:
        if owns_client:
            client.close()
    raise MediaDownloadError("Media redirect limit exceeded")


def resolve_instagram_media(
    shortcode: str,
    max_items: int,
    post_loader=None,
) -> list[RemoteMedia]:
    """Resolve a public Instagram post to ordered image/video CDN URLs."""
    loader = None
    if post_loader is None:
        try:
            import instaloader

            loader = instaloader.Instaloader(
                sleep=False,
                quiet=True,
                download_pictures=False,
                download_videos=False,
                download_video_thumbnails=False,
                download_geotags=False,
                download_comments=False,
                save_metadata=False,
                post_metadata_txt_pattern="",
                max_connection_attempts=1,
                request_timeout=15.0,
                iphone_support=False,
            )
            post_loader = lambda code: instaloader.Post.from_shortcode(
                loader.context, code
            )
        except Exception as exc:
            if loader is not None:
                loader.close()
            raise MediaExtractionError(
                f"Could not initialize Instagram extractor: {exc}"
            ) from exc

    try:
        try:
            post = post_loader(shortcode)
        except Exception as exc:
            raise MediaExtractionError(
                f"Could not resolve public Instagram post: {exc}"
            ) from exc

        if getattr(post, "typename", "") == "GraphSidecar":
            nodes = list(post.get_sidecar_nodes())
            if len(nodes) > max_items:
                raise MediaExtractionError(
                    f"Instagram post contains more than {max_items} media items"
                )
            media = [
                RemoteMedia(
                    url=node.video_url if node.is_video else node.display_url,
                    index=index,
                    is_video=bool(node.is_video),
                )
                for index, node in enumerate(nodes, start=1)
            ]
        else:
            is_video = bool(getattr(post, "is_video", False))
            media = [
                RemoteMedia(
                    url=post.video_url if is_video else post.url,
                    index=1,
                    is_video=is_video,
                )
            ]

        if not media or any(
            not item.url or urlsplit(item.url).scheme.lower() != "https"
            for item in media
        ):
            raise MediaExtractionError(
                "Instagram returned missing or non-HTTPS media URLs"
            )
        return media
    finally:
        if loader is not None:
            loader.close()


def _set_job_failure(
    conn: sqlite3.Connection,
    job: InstagramMediaJob,
    error: Exception,
    *,
    now: float,
    publication_uncertain: bool = False,
    max_attempts: int = 3,
) -> None:
    if publication_uncertain:
        status = "publication_uncertain"
        next_attempt_at = now
    elif job.attempts >= max_attempts:
        status = "failed"
        next_attempt_at = now
    else:
        status = "pending"
        next_attempt_at = now + min(300.0, 5.0 * (2 ** (job.attempts - 1)))
    conn.execute(
        """
        UPDATE instagram_media_jobs
        SET status = ?, next_attempt_at = ?, last_error = ?,
            claimed_at = NULL, claim_token = NULL
        WHERE channel = ? AND thread_ts = ? AND shortcode = ?
          AND claim_token = ?
        """,
        (
            status,
            next_attempt_at,
            str(error)[:1000],
            job.channel,
            job.thread_ts,
            job.shortcode,
            job.claim_token,
        ),
    )
    conn.commit()


def process_instagram_media_job(
    conn: sqlite3.Connection,
    job: InstagramMediaJob,
    slack_client,
    *,
    policy: InstagramMediaPolicy | None = None,
    media_resolver=resolve_instagram_media,
    media_downloader=download_remote_media,
    now: float | None = None,
) -> bool:
    """Resolve, download, and publish one claimed job without upload retries."""
    policy = policy or InstagramMediaPolicy.from_env()
    now = time.time() if now is None else now
    try:
        remote_media = media_resolver(job.shortcode, policy.max_items)
        if not remote_media:
            raise MediaExtractionError("Instagram post did not contain media")
        with tempfile.TemporaryDirectory(prefix="archivebot-instagram-") as directory:
            downloaded: list[DownloadedMedia] = []
            total_bytes = 0
            for media in remote_media:
                remaining_bytes = policy.max_total_bytes - total_bytes
                if remaining_bytes <= 0:
                    raise MediaDownloadError(
                        "Instagram post exceeded configured total byte limit"
                    )
                item_policy = replace(
                    policy,
                    max_file_bytes=min(policy.max_file_bytes, remaining_bytes),
                )
                item = media_downloader(
                    media,
                    shortcode=job.shortcode,
                    output_dir=Path(directory),
                    policy=item_policy,
                )
                total_bytes += item.size
                if total_bytes > policy.max_total_bytes:
                    raise MediaDownloadError(
                        "Instagram post exceeded configured total byte limit"
                    )
                downloaded.append(item)

            if not mark_instagram_job_uploading(conn, job, now=now):
                return False

            file_uploads = [
                {
                    "file": str(item.path),
                    "filename": (
                        f"instagram-{job.shortcode}-{item.index:02d}"
                        f"{item.path.suffix.lower()}"
                    ),
                    "title": f"Instagram {job.shortcode} — {item.index}",
                }
                for item in downloaded
            ]
            try:
                slack_client.files_upload_v2(
                    file_uploads=file_uploads,
                    channel=job.channel,
                    thread_ts=job.thread_ts,
                    initial_comment=f"📦 Copia archivio Instagram: {job.instagram_url}",
                )
            except Exception as exc:
                _set_job_failure(
                    conn,
                    job,
                    exc,
                    now=now,
                    publication_uncertain=True,
                )
                return False

        updated = conn.execute(
            """
            UPDATE instagram_media_jobs
            SET status = 'complete', completed_at = ?, last_error = NULL,
                claimed_at = NULL, claim_token = NULL
            WHERE channel = ? AND thread_ts = ? AND shortcode = ?
              AND status = 'uploading' AND claim_token = ?
            """,
            (
                now,
                job.channel,
                job.thread_ts,
                job.shortcode,
                job.claim_token,
            ),
        )
        conn.commit()
        return updated.rowcount == 1
    except (MediaExtractionError, MediaDownloadError, httpx.HTTPError) as exc:
        _set_job_failure(conn, job, exc, now=now)
        return False


class InstagramMediaWorker:
    """Small daemon loop around the durable Instagram media queue."""

    def __init__(
        self,
        database_path: str,
        slack_client,
        *,
        policy: InstagramMediaPolicy | None = None,
        media_resolver=resolve_instagram_media,
        media_downloader=download_remote_media,
        poll_interval: float = 2.0,
        error_backoff: float = 5.0,
        logger: logging.Logger | None = None,
    ):
        self.database_path = database_path
        self.slack_client = slack_client
        self.policy = policy or InstagramMediaPolicy.from_env()
        self.media_resolver = media_resolver
        self.media_downloader = media_downloader
        self.poll_interval = poll_interval
        self.error_backoff = error_backoff
        self.logger = logger or logging.getLogger(__name__)
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def process_once(self, *, now: float | None = None) -> bool:
        conn = sqlite3.connect(self.database_path, timeout=30)
        try:
            job = claim_instagram_media_job(conn, now=now)
            if job is None:
                return False
            return process_instagram_media_job(
                conn,
                job,
                self.slack_client,
                policy=self.policy,
                media_resolver=self.media_resolver,
                media_downloader=self.media_downloader,
                now=now,
            )
        finally:
            conn.close()

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                processed = self.process_once()
                delay = 0.0 if processed else self.poll_interval
            except Exception:
                self.logger.exception("Instagram media worker iteration failed")
                delay = self.error_backoff
            if delay > 0:
                self._stop_event.wait(delay)

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run,
                name="instagram-media-worker",
                daemon=True,
            )
            self._thread.start()

    def stop(self, timeout: float = 5.0) -> bool:
        with self._lock:
            thread = self._thread
            if thread is None:
                return True
            self._stop_event.set()
        thread.join(timeout=timeout)
        with self._lock:
            if self._thread is thread and not thread.is_alive():
                self._thread = None
                return True
            return self._thread is None
