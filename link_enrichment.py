"""Safe, durable enrichment of external links shared in Slack.

The Slack event path only records message/link associations and enqueues URLs.
Worker threads claim URLs from SQLite, fetch them with strict network and size
boundaries, extract a compact document representation, and cache the result.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from html.parser import HTMLParser
import ipaddress
import json
import logging
import os
import socket
import sqlite3
import threading
import time
from typing import Callable, Iterable
from urllib.parse import urljoin, urlsplit, urlunsplit
import uuid

import httpx
import httpcore
import numpy as np
import trafilatura
import certifi
import ssl
import dns.exception
import dns.resolver

from utils import db_connect


logger = logging.getLogger(__name__)

SUPPORTED_CONTENT_TYPES = {"text/html", "application/xhtml+xml"}
REDIRECT_STATUSES = {301, 302, 303, 307, 308}
# Trafilatura can otherwise promote a long consent banner to article text.
COOKIE_CONSENT_PRUNE_XPATH = (
    "//*[translate(@type, 'abcdefghijklmnopqrstuvwxyz', 'ABCDEFGHIJKLMNOPQRSTUVWXYZ') = "
    "'COOKIE_CONSENT']",
    "//*[contains(translate(@class, 'abcdefghijklmnopqrstuvwxyz', 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'), "
    "'COOKIE_CONSENT')]",
)


class EnrichmentError(Exception):
    """A bounded, persistable enrichment failure."""

    def __init__(self, code: str, message: str, *, http_status: int | None = None):
        super().__init__(message)
        self.code = code
        self.http_status = http_status


@dataclass(frozen=True)
class FetchPolicy:
    connect_timeout: float = 3.0
    read_timeout: float = 5.0
    pool_timeout: float = 2.0
    max_response_bytes: int = 3 * 1024 * 1024
    max_redirects: int = 5
    max_connections: int = 4
    total_timeout: float = 15.0
    allowed_ports: tuple[int, ...] = (80, 443)
    user_agent: str = "slack-archive-bot-link-enricher/0.1"

    @classmethod
    def from_env(cls) -> "FetchPolicy":
        return cls(
            connect_timeout=float(os.getenv("LINK_FETCH_CONNECT_TIMEOUT_SECONDS", "3")),
            read_timeout=float(os.getenv("LINK_FETCH_READ_TIMEOUT_SECONDS", "5")),
            pool_timeout=float(os.getenv("LINK_FETCH_POOL_TIMEOUT_SECONDS", "2")),
            max_response_bytes=int(os.getenv("LINK_FETCH_MAX_BYTES", str(3 * 1024 * 1024))),
            max_redirects=int(os.getenv("LINK_FETCH_MAX_REDIRECTS", "5")),
            max_connections=int(os.getenv("LINK_FETCH_MAX_CONNECTIONS", "4")),
            total_timeout=float(os.getenv("LINK_FETCH_TOTAL_TIMEOUT_SECONDS", "15")),
        )


@dataclass(frozen=True)
class FetchResult:
    requested_url: str
    final_url: str
    status_code: int
    content_type: str
    body: bytes


@dataclass(frozen=True)
class EnrichedDocument:
    requested_url: str
    final_url: str
    canonical_url: str
    title: str
    description: str
    content: str
    content_hash: str | None
    extraction_quality: str
    embedding_text: str
    http_status: int


@dataclass(frozen=True)
class EnrichmentJob:
    normalized_url: str
    requested_url: str
    attempts: int
    claim_token: str


def _is_public_address(address: str) -> bool:
    try:
        return ipaddress.ip_address(address).is_global
    except ValueError:
        return False


def _bounded_dns_resolver(
    hostname: str,
    port: int,
    *,
    type=socket.SOCK_STREAM,
    timeout: float,
    clock: Callable[[], float] = time.monotonic,
) -> list[tuple]:
    """Resolve A/AAAA records with one bounded dnspython lifetime budget."""
    try:
        literal = ipaddress.ip_address(hostname)
        family = socket.AF_INET6 if literal.version == 6 else socket.AF_INET
        return [(family, type, socket.IPPROTO_TCP, "", (str(literal), port))]
    except ValueError:
        pass

    deadline = clock() + timeout
    resolver = dns.resolver.Resolver()
    records: list[tuple] = []
    for record_type, family in (("A", socket.AF_INET), ("AAAA", socket.AF_INET6)):
        remaining = deadline - clock()
        if remaining <= 0:
            raise TimeoutError("DNS resolution deadline exceeded")
        try:
            answers = resolver.resolve(
                hostname,
                record_type,
                lifetime=remaining,
                search=False,
            )
        except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
            continue
        except (dns.exception.Timeout, dns.resolver.LifetimeTimeout) as exc:
            raise TimeoutError("DNS resolution deadline exceeded") from exc
        for answer in answers:
            records.append(
                (family, type, socket.IPPROTO_TCP, "", (answer.address, port))
            )
    return records


def resolve_public_addresses(
    hostname: str,
    port: int,
    *,
    resolver: Callable[..., Iterable[tuple]] = _bounded_dns_resolver,
    deadline: float | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[str, ...]:
    """Resolve every A/AAAA result and reject the host if any is non-public."""
    try:
        remaining = None if deadline is None else deadline - clock()
        if remaining is not None and remaining <= 0:
            raise EnrichmentError("total_timeout", "Total fetch deadline exceeded during DNS")
        records = resolver(
            hostname,
            port,
            type=socket.SOCK_STREAM,
            timeout=remaining if remaining is not None else 15.0,
        )
        if deadline is not None and clock() > deadline:
            raise EnrichmentError("total_timeout", "Total fetch deadline exceeded during DNS")
    except EnrichmentError:
        raise
    except TimeoutError as exc:
        raise EnrichmentError("total_timeout", f"DNS resolution timed out for {hostname}") from exc
    except (OSError, dns.exception.DNSException) as exc:
        raise EnrichmentError("dns_error", f"DNS resolution failed for {hostname}: {exc}") from exc

    addresses = tuple(sorted({record[4][0] for record in records}))
    if not addresses:
        raise EnrichmentError("dns_error", f"DNS resolution returned no addresses for {hostname}")
    blocked = [address for address in addresses if not _is_public_address(address)]
    if blocked:
        raise EnrichmentError("blocked_address", f"Non-public destination rejected: {blocked[0]}")
    return addresses


def _validate_fetch_destination(
    url: str,
    policy: FetchPolicy,
    *,
    resolver: Callable[..., Iterable[tuple]] = _bounded_dns_resolver,
    deadline: float | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[str, tuple[str, ...]]:
    parsed = urlsplit(url)
    if parsed.scheme.lower() not in {"http", "https"}:
        raise EnrichmentError("invalid_scheme", "Only HTTP(S) URLs can be enriched")
    if parsed.username is not None or parsed.password is not None:
        raise EnrichmentError("credentials_forbidden", "URL credentials are forbidden")
    if not parsed.hostname:
        raise EnrichmentError("invalid_host", "URL hostname is required")

    try:
        port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    except ValueError as exc:
        raise EnrichmentError("invalid_port", "Invalid URL port") from exc
    if port not in policy.allowed_ports:
        raise EnrichmentError("blocked_port", f"Destination port {port} is not allowed")

    addresses = resolve_public_addresses(
        parsed.hostname,
        port,
        resolver=resolver,
        deadline=deadline,
        clock=clock,
    )
    # Fragments never affect the fetched representation.
    safe_url = urlunsplit((parsed.scheme.lower(), parsed.netloc, parsed.path or "/", parsed.query, ""))
    return safe_url, addresses


def validate_fetch_url(
    url: str,
    policy: FetchPolicy,
    *,
    resolver: Callable[..., Iterable[tuple]] = _bounded_dns_resolver,
) -> str:
    safe_url, _ = _validate_fetch_destination(url, policy, resolver=resolver)
    return safe_url


class _DeadlineNetworkStream(httpcore.NetworkStream):
    """Apply one absolute deadline to every blocking stream operation."""

    def __init__(self, stream, deadline: float, clock: Callable[[], float]):
        self._stream = stream
        self._deadline = deadline
        self._clock = clock

    def _remaining(self, requested: float | None, error_type):
        remaining = self._deadline - self._clock()
        if remaining <= 0:
            raise error_type("Total fetch deadline exceeded")
        return remaining if requested is None else min(requested, remaining)

    def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        return self._stream.read(
            max_bytes,
            timeout=self._remaining(timeout, httpcore.ReadTimeout),
        )

    def write(self, buffer: bytes, timeout: float | None = None) -> None:
        self._stream.write(
            buffer,
            timeout=self._remaining(timeout, httpcore.WriteTimeout),
        )

    def close(self) -> None:
        self._stream.close()

    def start_tls(
        self,
        ssl_context,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ):
        stream = self._stream.start_tls(
            ssl_context,
            server_hostname=server_hostname,
            timeout=self._remaining(timeout, httpcore.ConnectTimeout),
        )
        return _DeadlineNetworkStream(stream, self._deadline, self._clock)

    def get_extra_info(self, info: str):
        return self._stream.get_extra_info(info)


class _DeadlineNetworkBackend(httpcore.NetworkBackend):
    def __init__(self, deadline: float, clock: Callable[[], float]):
        self._backend = httpcore.SyncBackend()
        self._deadline = deadline
        self._clock = clock

    def _remaining(self, requested: float | None) -> float:
        remaining = self._deadline - self._clock()
        if remaining <= 0:
            raise httpcore.ConnectTimeout("Total fetch deadline exceeded")
        return remaining if requested is None else min(requested, remaining)

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        stream = self._backend.connect_tcp(
            host,
            port,
            timeout=self._remaining(timeout),
            local_address=local_address,
            socket_options=socket_options,
        )
        return _DeadlineNetworkStream(stream, self._deadline, self._clock)

    def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise httpcore.UnsupportedProtocol("Unix sockets are disabled for link enrichment")

    def sleep(self, seconds: float) -> None:
        remaining = self._remaining(seconds)
        self._backend.sleep(min(seconds, remaining))


class _DeadlineHTTPTransport(httpx.HTTPTransport):
    """HTTPX transport backed by deadline-aware httpcore sockets."""

    def __init__(self, policy: FetchPolicy, deadline: float, clock: Callable[[], float]):
        limits = httpx.Limits(
            max_connections=policy.max_connections,
            max_keepalive_connections=0,
        )
        super().__init__(trust_env=False, limits=limits, retries=0)
        self._pool.close()
        self._pool = httpcore.ConnectionPool(
            ssl_context=ssl.create_default_context(cafile=certifi.where()),
            max_connections=policy.max_connections,
            max_keepalive_connections=0,
            http1=True,
            http2=False,
            retries=0,
            network_backend=_DeadlineNetworkBackend(deadline, clock),
        )


def _httpx_client(
    policy: FetchPolicy,
    deadline: float,
    clock: Callable[[], float],
) -> httpx.Client:
    timeout = httpx.Timeout(
        policy.read_timeout,
        connect=policy.connect_timeout,
        read=policy.read_timeout,
        write=policy.read_timeout,
        pool=policy.pool_timeout,
    )
    return httpx.Client(
        timeout=timeout,
        transport=_DeadlineHTTPTransport(policy, deadline, clock),
        follow_redirects=False,
        trust_env=False,
        headers={
            "User-Agent": policy.user_agent,
            "Accept": "text/html,application/xhtml+xml;q=0.9",
            "Connection": "close",
        },
    )


def _pinned_request_target(logical_url: str, address: str) -> tuple[str, str, str]:
    """Return IP URL, Host authority, and TLS SNI name for one validated address."""
    parsed = urlsplit(logical_url)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    ip_host = f"[{address}]" if ":" in address else address
    default_port = (parsed.scheme == "https" and port == 443) or (parsed.scheme == "http" and port == 80)
    pinned_authority = ip_host if default_port else f"{ip_host}:{port}"
    original_host = parsed.hostname or ""
    try:
        original_host = original_host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise EnrichmentError("invalid_host", "Hostname cannot be encoded") from exc
    host_authority = f"[{original_host}]" if ":" in original_host else original_host
    if not default_port:
        host_authority = f"{host_authority}:{port}"
    pinned_url = urlunsplit(
        (parsed.scheme, pinned_authority, parsed.path or "/", parsed.query, "")
    )
    return pinned_url, host_authority, original_host


def fetch_html(
    url: str,
    *,
    policy: FetchPolicy | None = None,
    client: httpx.Client | None = None,
    resolver: Callable[..., Iterable[tuple]] = _bounded_dns_resolver,
    clock: Callable[[], float] = time.monotonic,
) -> FetchResult:
    """Fetch HTML with manual, revalidated redirects and a decompressed byte cap."""
    policy = policy or FetchPolicy.from_env()
    deadline = clock() + policy.total_timeout
    owns_client = client is None
    client = client or _httpx_client(policy, deadline, clock)
    current_url = url

    try:
        for redirect_count in range(policy.max_redirects + 1):
            remaining = deadline - clock()
            if remaining <= 0:
                raise EnrichmentError("total_timeout", "Total fetch deadline exceeded")
            safe_url, addresses = _validate_fetch_destination(
                current_url,
                policy,
                resolver=resolver,
                deadline=deadline,
                clock=clock,
            )
            pinned_url, host_header, sni_hostname = _pinned_request_target(safe_url, addresses[0])
            request_timeout = httpx.Timeout(
                min(policy.read_timeout, remaining),
                connect=min(policy.connect_timeout, remaining),
                read=min(policy.read_timeout, remaining),
                write=min(policy.read_timeout, remaining),
                pool=min(policy.pool_timeout, remaining),
            )
            try:
                request = client.build_request(
                    "GET",
                    pinned_url,
                    headers={"Host": host_header},
                    timeout=request_timeout,
                )
                # httpcore connects to the IP in request.url but validates the
                # certificate and sends TLS SNI for the original hostname.
                request.extensions["sni_hostname"] = sni_hostname
                response = client.send(request, stream=True)
                try:
                    if response.status_code in REDIRECT_STATUSES:
                        location = response.headers.get("location")
                        if not location:
                            raise EnrichmentError(
                                "invalid_redirect",
                                "Redirect response did not include Location",
                                http_status=response.status_code,
                            )
                        if redirect_count >= policy.max_redirects:
                            raise EnrichmentError(
                                "too_many_redirects",
                                "Redirect limit exceeded",
                                http_status=response.status_code,
                            )
                        current_url = urljoin(safe_url, location)
                        continue

                    if response.status_code < 200 or response.status_code >= 300:
                        raise EnrichmentError(
                            "http_error",
                            f"HTTP status {response.status_code}",
                            http_status=response.status_code,
                        )

                    content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                    if content_type not in SUPPORTED_CONTENT_TYPES:
                        raise EnrichmentError(
                            "unsupported_content_type",
                            f"Unsupported content type: {content_type or 'missing'}",
                            http_status=response.status_code,
                        )

                    chunks: list[bytes] = []
                    total = 0
                    for chunk in response.iter_bytes():
                        if clock() > deadline:
                            raise EnrichmentError(
                                "total_timeout",
                                "Total fetch deadline exceeded",
                                http_status=response.status_code,
                            )
                        total += len(chunk)
                        if total > policy.max_response_bytes:
                            raise EnrichmentError(
                                "response_too_large",
                                "Response exceeded configured byte limit",
                                http_status=response.status_code,
                            )
                        chunks.append(chunk)

                    return FetchResult(
                        requested_url=url,
                        final_url=safe_url,
                        status_code=response.status_code,
                        content_type=content_type,
                        body=b"".join(chunks),
                    )
                finally:
                    response.close()
            except EnrichmentError:
                raise
            except httpx.TimeoutException as exc:
                code = "total_timeout" if clock() >= deadline else "timeout"
                raise EnrichmentError(code, f"Fetch timed out: {exc}") from exc
            except httpx.HTTPError as exc:
                raise EnrichmentError("network_error", f"Fetch failed: {exc}") from exc
    finally:
        if owns_client:
            client.close()

    raise EnrichmentError("too_many_redirects", "Redirect limit exceeded")


class _MetadataParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.in_title = False
        self.metadata: dict[str, str] = {}
        self.canonical_url = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.lower(): (value or "") for key, value in attrs}
        if tag.lower() == "title":
            self.in_title = True
        elif tag.lower() == "meta":
            key = (values.get("property") or values.get("name") or "").lower()
            content = values.get("content", "").strip()
            if key and content and key not in self.metadata:
                self.metadata[key] = content
        elif tag.lower() == "link" and "canonical" in values.get("rel", "").lower().split():
            self.canonical_url = values.get("href", "").strip()

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "title":
            self.in_title = False

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title_parts.append(data)


def _normalize_text(value: str, limit: int) -> str:
    return " ".join((value or "").split())[:limit]


def extract_document(result: FetchResult, *, max_content_chars: int = 20_000) -> EnrichedDocument:
    encoding = "utf-8"
    html = result.body.decode(encoding, errors="replace")
    parser = _MetadataParser()
    try:
        parser.feed(html)
    except Exception:
        logger.debug("HTML metadata parser failed", exc_info=True)

    title = _normalize_text(
        parser.metadata.get("og:title")
        or parser.metadata.get("twitter:title")
        or "".join(parser.title_parts),
        500,
    )
    description = _normalize_text(
        parser.metadata.get("og:description")
        or parser.metadata.get("twitter:description")
        or parser.metadata.get("description", ""),
        2_000,
    )
    canonical_candidate = parser.metadata.get("og:url") or parser.canonical_url
    canonical_url = urljoin(result.final_url, canonical_candidate) if canonical_candidate else result.final_url
    canonical_parts = urlsplit(canonical_url)
    if canonical_parts.scheme.lower() not in {"http", "https"} or not canonical_parts.hostname:
        canonical_url = result.final_url

    content = ""
    try:
        extracted = trafilatura.extract(
            html,
            url=result.final_url,
            include_comments=False,
            include_tables=False,
            favor_precision=True,
            output_format="json",
            with_metadata=True,
            prune_xpath=list(COOKIE_CONSENT_PRUNE_XPATH),
        )
        if extracted:
            payload = json.loads(extracted)
            content = _normalize_text(payload.get("text", ""), max_content_chars)
            title = title or _normalize_text(payload.get("title", ""), 500)
            description = description or _normalize_text(payload.get("description", ""), 2_000)
    except Exception:
        logger.debug("Main-content extraction failed", exc_info=True)

    if len(content) >= 200:
        quality = "full_text"
    elif title or description:
        quality = "metadata_only"
    else:
        quality = "url_only"

    if quality == "full_text":
        hash_input = content
    elif quality == "metadata_only":
        # Short extracted text is often shared navigation/interstitial boilerplate.
        hash_input = "\n".join(part for part in (title, description) if part)
    else:
        hash_input = content
    content_hash = hashlib.sha256(hash_input.casefold().encode("utf-8")).hexdigest() if hash_input else None
    embedding_text = "\n".join(part for part in (title, description, content) if part)

    return EnrichedDocument(
        requested_url=result.requested_url,
        final_url=result.final_url,
        canonical_url=canonical_url,
        title=title,
        description=description,
        content=content,
        content_hash=content_hash,
        extraction_quality=quality,
        embedding_text=embedding_text,
        http_status=result.status_code,
    )


def enqueue_link(
    conn: sqlite3.Connection,
    *,
    channel: str,
    message_timestamp: str,
    thread_ts: str,
    normalized_url: str,
    original_url: str,
    permalink: str,
    posted_at: float,
    now: float | None = None,
) -> bool:
    """Idempotently associate a link and queue its document if cache is absent/stale."""
    now = now if now is not None else time.time()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT OR IGNORE INTO message_links
        (channel, message_timestamp, thread_ts, normalized_url, original_url, permalink, posted_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (channel, message_timestamp, thread_ts, normalized_url, original_url, permalink, posted_at),
    )
    inserted = cursor.rowcount == 1
    cursor.execute(
        """
        INSERT OR IGNORE INTO link_documents
        (normalized_url, requested_url, extraction_quality, fetch_status)
        VALUES (?, ?, 'pending', 'pending')
        """,
        (normalized_url, original_url),
    )
    cursor.execute(
        """
        SELECT fetch_status, expires_at FROM link_documents WHERE normalized_url = ?
        """,
        (normalized_url,),
    )
    fetch_status, expires_at = cursor.fetchone()
    cache_valid = (
        fetch_status in {"complete", "failed"}
        and expires_at is not None
        and expires_at > now
    )
    if not cache_valid:
        cursor.execute(
            """
            INSERT INTO link_enrichment_jobs
            (normalized_url, status, attempts, recoveries, available_at, created_at, updated_at)
            VALUES (?, 'pending', 0, 0, ?, ?, ?)
            ON CONFLICT(normalized_url) DO UPDATE SET
                status = CASE
                    WHEN link_enrichment_jobs.status = 'processing' THEN 'processing'
                    ELSE 'pending'
                END,
                attempts = CASE
                    WHEN link_enrichment_jobs.status = 'processing' THEN link_enrichment_jobs.attempts
                    ELSE 0
                END,
                recoveries = CASE
                    WHEN link_enrichment_jobs.status = 'processing' THEN link_enrichment_jobs.recoveries
                    ELSE 0
                END,
                available_at = CASE
                    WHEN link_enrichment_jobs.status = 'processing' THEN link_enrichment_jobs.available_at
                    ELSE excluded.available_at
                END,
                updated_at = excluded.updated_at,
                last_error = CASE
                    WHEN link_enrichment_jobs.status = 'processing' THEN link_enrichment_jobs.last_error
                    ELSE NULL
                END
            """,
            (normalized_url, now, now, now),
        )
    conn.commit()
    return inserted


def claim_next_job(
    conn: sqlite3.Connection,
    *,
    now: float | None = None,
    stale_after_seconds: float = 300,
    max_attempts: int = 3,
    max_recoveries: int = 2,
) -> EnrichmentJob | None:
    """Atomically claim one due job, including recovery of abandoned claims."""
    now = now if now is not None else time.time()
    stale_before = now - stale_after_seconds
    cursor = conn.cursor()
    try:
        cursor.execute("BEGIN IMMEDIATE")
        cursor.execute(
            """
            UPDATE link_enrichment_jobs
            SET status = 'failed', claim_token = NULL, claimed_at = NULL,
                updated_at = ?, last_error = 'stale claim recovery limit exceeded'
            WHERE status = 'processing' AND claimed_at < ? AND recoveries >= ?
            """,
            (now, stale_before, max_recoveries),
        )
        row = cursor.execute(
            """
            SELECT j.normalized_url, d.requested_url, j.attempts, j.status, j.recoveries
            FROM link_enrichment_jobs j
            JOIN link_documents d ON d.normalized_url = j.normalized_url
            WHERE (
                    (j.status IN ('pending', 'retry') AND j.attempts < ? AND j.available_at <= ?)
                 OR (j.status = 'processing' AND j.attempts <= ?
                     AND j.claimed_at < ? AND j.recoveries < ?)
            )
            ORDER BY j.available_at, j.created_at
            LIMIT 1
            """,
            (max_attempts, now, max_attempts, stale_before, max_recoveries),
        ).fetchone()
        if row is None:
            conn.commit()
            return None
        normalized_url, requested_url, attempts, previous_status, recoveries = row
        next_attempts = attempts if previous_status == "processing" else attempts + 1
        next_recoveries = recoveries + 1 if previous_status == "processing" else recoveries
        claim_token = uuid.uuid4().hex
        cursor.execute(
            """
            UPDATE link_enrichment_jobs
            SET status = 'processing', attempts = ?, recoveries = ?,
                claimed_at = ?, claim_token = ?, updated_at = ?
            WHERE normalized_url = ?
            """,
            (next_attempts, next_recoveries, now, claim_token, now, normalized_url),
        )
        conn.commit()
        return EnrichmentJob(normalized_url, requested_url, next_attempts, claim_token)
    except Exception:
        conn.rollback()
        raise


def _embedding_blob(embedding: object) -> bytes | None:
    if embedding is None or isinstance(embedding, str):
        return None
    array = np.asarray(embedding, dtype=np.float32)
    if array.size == 0:
        return None
    return array.tobytes()


def complete_job(
    conn: sqlite3.Connection,
    job: EnrichmentJob,
    document: EnrichedDocument,
    embedding: object,
    *,
    now: float | None = None,
    cache_ttl_seconds: float = 7 * 24 * 60 * 60,
) -> bool:
    now = now if now is not None else time.time()
    cursor = conn.cursor()
    cursor.execute("BEGIN IMMEDIATE")
    cursor.execute(
        """
        UPDATE link_enrichment_jobs
        SET status = 'complete', claimed_at = NULL, claim_token = NULL,
            updated_at = ?, last_error = NULL
        WHERE normalized_url = ? AND status = 'processing' AND claim_token = ?
        """,
        (now, job.normalized_url, job.claim_token),
    )
    if cursor.rowcount != 1:
        conn.rollback()
        return False
    cursor.execute(
        """
        UPDATE link_documents SET
            final_url = ?, canonical_url = ?, title = ?, description = ?,
            content = ?, content_hash = ?, embedding = ?, extraction_quality = ?,
            fetch_status = 'complete', http_status = ?, fetched_at = ?,
            expires_at = ?, last_error = NULL
        WHERE normalized_url = ?
        """,
        (
            document.final_url,
            document.canonical_url,
            document.title,
            document.description,
            document.content,
            document.content_hash,
            _embedding_blob(embedding),
            document.extraction_quality,
            document.http_status,
            now,
            now + cache_ttl_seconds,
            job.normalized_url,
        ),
    )
    conn.commit()
    return True


def fail_job(
    conn: sqlite3.Connection,
    job: EnrichmentJob,
    error: EnrichmentError,
    *,
    now: float | None = None,
    max_attempts: int = 3,
) -> bool:
    now = now if now is not None else time.time()
    retryable = error.code in {"timeout", "network_error", "dns_error"} and job.attempts < max_attempts
    status = "retry" if retryable else "failed"
    delay = min(300, 10 * (2 ** max(0, job.attempts - 1))) if retryable else 0
    message = str(error)[:1_000]
    cursor = conn.cursor()
    cursor.execute("BEGIN IMMEDIATE")
    cursor.execute(
        """
        UPDATE link_enrichment_jobs SET
            status = ?, available_at = ?, claimed_at = NULL, claim_token = NULL,
            updated_at = ?, last_error = ?
        WHERE normalized_url = ? AND status = 'processing' AND claim_token = ?
        """,
        (status, now + delay, now, message, job.normalized_url, job.claim_token),
    )
    if cursor.rowcount != 1:
        conn.rollback()
        return False
    cursor.execute(
        """
        UPDATE link_documents SET
            fetch_status = ?, http_status = ?, fetched_at = ?,
            expires_at = ?, last_error = ?
        WHERE normalized_url = ?
        """,
        (status, error.http_status, now, now + (3600 if retryable else 24 * 3600), message, job.normalized_url),
    )
    conn.commit()
    return True


def process_next_job(
    database_path: str,
    embed: Callable[[str], object],
    *,
    fetcher: Callable[[str], FetchResult] = fetch_html,
    on_document_ready: Callable[[str], None] | None = None,
) -> bool:
    conn, _ = db_connect(database_path)
    ready_url = None
    try:
        job = claim_next_job(conn)
        if job is None:
            return False
        try:
            document = extract_document(fetcher(job.requested_url))
            embedding = embed(document.embedding_text) if document.embedding_text else None
            cache_ttl = float(
                os.getenv("LINK_FETCH_CACHE_TTL_SECONDS", str(7 * 24 * 60 * 60))
            )
            completed = complete_job(
                conn,
                job,
                document,
                embedding,
                cache_ttl_seconds=cache_ttl,
            )
            if completed:
                ready_url = job.normalized_url
        except EnrichmentError as exc:
            fail_job(conn, job, exc)
        except Exception as exc:
            logger.exception("Unexpected link enrichment failure for %s", job.normalized_url)
            fail_job(conn, job, EnrichmentError("internal_error", str(exc)))
        if ready_url is not None and on_document_ready is not None:
            # Matching failure must not rewrite a completed fetch as failed.
            # The worker loop will retry durable unchecked message_links.
            on_document_ready(ready_url)
        return True
    finally:
        conn.close()


class LinkEnrichmentWorker:
    def __init__(
        self,
        database_path: str,
        embed: Callable[[str], object],
        *,
        on_document_ready: Callable[[str], None] | None = None,
        poll_interval: float = 2.0,
        error_backoff: float = 5.0,
    ) -> None:
        self.database_path = database_path
        self.embed = embed
        self.on_document_ready = on_document_ready
        self.poll_interval = poll_interval
        self.error_backoff = error_backoff
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        # The application migrates the database once before Gunicorn forks.
        # Repeating DDL here makes every post_fork hook contend for SQLite's
        # writer lock and can prevent all web workers from booting. Runtime
        # lock contention is handled by the retry loop in _run instead.
        self._thread = threading.Thread(
            target=self._run,
            name="link-enrichment-worker",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def _run(self) -> None:
        logger.info("Link enrichment worker started")
        while not self._stop.is_set():
            try:
                processed = process_next_job(
                    self.database_path,
                    self.embed,
                    on_document_ready=self.on_document_ready,
                )
            except Exception:
                logger.exception("Link enrichment worker iteration failed; retrying")
                self._stop.wait(self.error_backoff)
                continue
            if not processed:
                if self.on_document_ready is not None:
                    try:
                        self.on_document_ready("")
                    except Exception:
                        logger.exception("Pending link duplicate scan failed; retrying")
                        self._stop.wait(self.error_backoff)
                        continue
                self._stop.wait(self.poll_interval)
        logger.info("Link enrichment worker stopped")
