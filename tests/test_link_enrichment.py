import os
import socket
import sqlite3
import sys
import tempfile

import httpx
import numpy as np
import pytest


ROOT_DIR = os.path.dirname(os.path.dirname(__file__))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

import link_enrichment as link_enrichment_module
from link_enrichment import (
    EnrichedDocument,
    EnrichmentError,
    FetchPolicy,
    FetchResult,
    LinkEnrichmentWorker,
    claim_next_job,
    complete_job,
    enqueue_link,
    extract_document,
    fetch_html,
    process_next_job,
    validate_fetch_url,
)
from utils import migrate_db


PUBLIC_IP = "93.184.216.34"


def public_resolver(host, port, type=socket.SOCK_STREAM, timeout=None):
    return [(socket.AF_INET, type, 6, "", (PUBLIC_IP, port))]


def private_resolver(host, port, type=socket.SOCK_STREAM, timeout=None):
    return [(socket.AF_INET, type, 6, "", ("127.0.0.1", port))]


def migrated_connection():
    conn = sqlite3.connect(":memory:")
    migrate_db(conn, conn.cursor())
    return conn


def test_validate_fetch_url_rejects_unsafe_destinations_and_credentials():
    policy = FetchPolicy()

    with pytest.raises(EnrichmentError, match="Only HTTP"):
        validate_fetch_url("file:///etc/passwd", policy, resolver=public_resolver)
    with pytest.raises(EnrichmentError, match="credentials"):
        validate_fetch_url("https://user:secret@example.com/", policy, resolver=public_resolver)
    with pytest.raises(EnrichmentError, match="port 8080"):
        validate_fetch_url("https://example.com:8080/", policy, resolver=public_resolver)
    with pytest.raises(EnrichmentError, match="Non-public"):
        validate_fetch_url("http://localhost/", policy, resolver=private_resolver)


def test_validate_fetch_url_rejects_host_if_any_resolved_address_is_private():
    def mixed_resolver(host, port, type=socket.SOCK_STREAM, timeout=None):
        return [
            (socket.AF_INET, type, 6, "", (PUBLIC_IP, port)),
            (socket.AF_INET, type, 6, "", ("10.0.0.4", port)),
        ]

    with pytest.raises(EnrichmentError, match="Non-public"):
        validate_fetch_url("https://example.com", FetchPolicy(), resolver=mixed_resolver)


def test_fetch_html_revalidates_redirect_hops_and_returns_bounded_html():
    resolved = []
    requests = []

    def resolver(host, port, type=socket.SOCK_STREAM, timeout=None):
        resolved.append(host)
        return public_resolver(host, port, type, timeout)

    def handler(request):
        requests.append(request)
        if request.headers["host"] == "example.com":
            return httpx.Response(302, headers={"location": "https://news.example/article"})
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            content=b"<html><title>Story</title><body>Body</body></html>",
        )

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    result = fetch_html(
        "https://example.com/start#fragment",
        client=client,
        resolver=resolver,
    )

    assert resolved == ["example.com", "news.example"]
    assert [request.url.host for request in requests] == [PUBLIC_IP, PUBLIC_IP]
    assert [request.extensions["sni_hostname"] for request in requests] == [
        "example.com",
        "news.example",
    ]
    assert result.final_url == "https://news.example/article"
    assert result.body.startswith(b"<html>")


def test_fetch_html_rejects_redirect_to_private_destination():
    def resolver(host, port, type=socket.SOCK_STREAM, timeout=None):
        if host == "internal.example":
            return private_resolver(host, port, type, timeout)
        return public_resolver(host, port, type, timeout)

    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                302,
                headers={"location": "http://internal.example/secrets"},
            )
        ),
        follow_redirects=False,
    )

    with pytest.raises(EnrichmentError) as error:
        fetch_html("https://example.com", client=client, resolver=resolver)
    assert error.value.code == "blocked_address"


def test_fetch_html_pins_connection_to_validated_ip_so_dns_cannot_rebind():
    resolver_calls = 0

    def resolver(host, port, type=socket.SOCK_STREAM, timeout=None):
        nonlocal resolver_calls
        resolver_calls += 1
        # An attacker could change the next DNS response to loopback. The
        # transport must never perform that second hostname lookup: it receives
        # the validated IP as its request destination.
        return public_resolver(host, port, type, timeout)

    def handler(request):
        assert request.url.host == PUBLIC_IP
        assert request.headers["host"] == "rebind.example"
        assert request.extensions["sni_hostname"] == "rebind.example"
        return httpx.Response(200, headers={"content-type": "text/html"}, content=b"<title>Safe</title>")

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    fetch_html("https://rebind.example/story", client=client, resolver=resolver)

    assert resolver_calls == 1


def test_fetch_html_stops_when_dns_consumes_total_deadline():
    now = [100.0]
    connection_attempted = []

    def slow_resolver(host, port, type=socket.SOCK_STREAM, timeout=None):
        assert timeout == 5.0
        now[0] += 6.0
        return public_resolver(host, port, type, timeout)

    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: connection_attempted.append(request) or httpx.Response(200)
        )
    )

    with pytest.raises(EnrichmentError) as error:
        fetch_html(
            "https://slow-dns.example/story",
            policy=FetchPolicy(total_timeout=5.0),
            client=client,
            resolver=slow_resolver,
            clock=lambda: now[0],
        )
    assert error.value.code == "total_timeout"
    assert connection_attempted == []


def test_fetch_html_enforces_total_deadline_against_slow_trickle():
    now = [100.0]

    class SlowStream(httpx.SyncByteStream):
        def __iter__(self):
            now[0] += 6.0
            yield b"a"
            now[0] += 6.0
            yield b"b"

    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                headers={"content-type": "text/html"},
                stream=SlowStream(),
            )
        )
    )

    with pytest.raises(EnrichmentError) as error:
        fetch_html(
            "https://example.com/slow",
            policy=FetchPolicy(total_timeout=5.0),
            client=client,
            resolver=public_resolver,
            clock=lambda: now[0],
        )
    assert error.value.code == "total_timeout"


def test_deadline_stream_recomputes_budget_after_earlier_network_phases():
    now = [6.0]  # A connect/TLS/header phase already consumed six seconds.
    observed_timeouts = []

    class UnderlyingStream:
        def read(self, max_bytes, timeout=None):
            observed_timeouts.append(timeout)
            now[0] += 3.0
            return b"chunk"

        def close(self):
            pass

        def get_extra_info(self, info):
            return None

    stream = link_enrichment_module._DeadlineNetworkStream(
        UnderlyingStream(), deadline=10.0, clock=lambda: now[0]
    )

    assert stream.read(10, timeout=8.0) == b"chunk"
    assert observed_timeouts == [4.0]
    assert stream.read(10, timeout=8.0) == b"chunk"
    assert observed_timeouts == [4.0, 1.0]
    with pytest.raises(link_enrichment_module.httpcore.ReadTimeout):
        stream.read(10, timeout=8.0)


def test_fetch_html_rejects_unsupported_and_oversized_responses():
    pdf_client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, headers={"content-type": "application/pdf"}, content=b"pdf")
        )
    )
    with pytest.raises(EnrichmentError) as unsupported:
        fetch_html("https://example.com/file", client=pdf_client, resolver=public_resolver)
    assert unsupported.value.code == "unsupported_content_type"

    large_client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, headers={"content-type": "text/html"}, content=b"x" * 20)
        )
    )
    with pytest.raises(EnrichmentError) as oversized:
        fetch_html(
            "https://example.com/large",
            policy=FetchPolicy(max_response_bytes=10),
            client=large_client,
            resolver=public_resolver,
        )
    assert oversized.value.code == "response_too_large"


def test_extract_document_prefers_open_graph_and_extracts_main_text():
    body = b"""
        <html><head>
          <title>Fallback title</title>
          <meta property="og:title" content="Canonical Story">
          <meta property="og:description" content="A useful description">
          <meta property="og:url" content="/canonical-story">
        </head><body><article>
          <h1>Canonical Story</h1>
          <p>This is the main article paragraph with enough meaningful words to be extracted.</p>
          <p>It continues with technical details about a release, migration, and rollback plan.</p>
          <p>The final paragraph contains additional context so the extractor sees a real article.</p>
        </article></body></html>
    """
    document = extract_document(
        FetchResult(
            requested_url="https://example.com/story?utm_source=slack",
            final_url="https://example.com/story",
            status_code=200,
            content_type="text/html",
            body=body,
        )
    )

    assert document.title == "Canonical Story"
    assert document.description == "A useful description"
    assert document.canonical_url == "https://example.com/canonical-story"
    assert document.extraction_quality in {"full_text", "metadata_only"}
    assert document.content_hash
    assert "Canonical Story" in document.embedding_text


def test_extract_document_degrades_to_metadata_only():
    document = extract_document(
        FetchResult(
            requested_url="https://example.com",
            final_url="https://example.com/",
            status_code=200,
            content_type="text/html",
            body=b'<meta name="description" content="Small page"><title>Title</title>',
        )
    )

    assert document.extraction_quality == "metadata_only"
    assert document.title == "Title"
    assert document.content_hash


def test_enqueue_and_claim_are_idempotent_and_claim_once():
    conn = migrated_connection()

    args = dict(
        channel="C1",
        message_timestamp="100.1",
        thread_ts="100.1",
        normalized_url="https://example.com/story",
        original_url="https://example.com/story?utm_source=slack",
        permalink="https://slack.example/message",
        posted_at=100.1,
        now=200.0,
    )
    assert enqueue_link(conn, **args) is True
    assert enqueue_link(conn, **args) is False

    assert conn.execute("SELECT COUNT(*) FROM message_links").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM link_enrichment_jobs").fetchone()[0] == 1
    first = claim_next_job(conn, now=200.0)
    second = claim_next_job(conn, now=200.0)
    assert first.normalized_url == "https://example.com/story"
    assert first.attempts == 1
    assert second is None


def test_complete_job_caches_document_and_prevents_fresh_requeue():
    conn = migrated_connection()
    enqueue_link(
        conn,
        channel="C1",
        message_timestamp="100.1",
        thread_ts="100.1",
        normalized_url="https://example.com/story",
        original_url="https://example.com/story",
        permalink="https://slack.example/one",
        posted_at=100.1,
        now=200.0,
    )
    job = claim_next_job(conn, now=200.0)
    document = EnrichedDocument(
        requested_url="https://example.com/story",
        final_url="https://example.com/story",
        canonical_url="https://example.com/story",
        title="Story",
        description="Description",
        content="Article content",
        content_hash="abc",
        extraction_quality="full_text",
        embedding_text="Story\nDescription\nArticle content",
        http_status=200,
    )
    complete_job(conn, job, document, np.array([1.0, 2.0]), now=201.0)

    enqueue_link(
        conn,
        channel="C2",
        message_timestamp="101.1",
        thread_ts="101.1",
        normalized_url="https://example.com/story",
        original_url="https://example.com/story",
        permalink="https://slack.example/two",
        posted_at=101.1,
        now=202.0,
    )

    row = conn.execute(
        "SELECT fetch_status, extraction_quality, length(embedding) FROM link_documents"
    ).fetchone()
    assert row == ("complete", "full_text", 8)
    assert claim_next_job(conn, now=202.0) is None


def test_negative_cache_prevents_immediate_requeue():
    conn = migrated_connection()
    enqueue_link(
        conn,
        channel="C1",
        message_timestamp="100.1",
        thread_ts="100.1",
        normalized_url="https://example.com/missing",
        original_url="https://example.com/missing",
        permalink="",
        posted_at=100.1,
        now=200.0,
    )
    claim_next_job(conn, now=200.0)
    conn.execute(
        "UPDATE link_documents SET fetch_status = 'failed', expires_at = 500"
    )
    conn.execute(
        "UPDATE link_enrichment_jobs SET status = 'failed', attempts = 3"
    )
    conn.commit()

    enqueue_link(
        conn,
        channel="C2",
        message_timestamp="101.1",
        thread_ts="101.1",
        normalized_url="https://example.com/missing",
        original_url="https://example.com/missing",
        permalink="",
        posted_at=101.1,
        now=300.0,
    )

    assert conn.execute("SELECT status FROM link_enrichment_jobs").fetchone()[0] == "failed"
    assert claim_next_job(conn, now=300.0) is None


def test_stale_processing_claim_is_recovered_without_consuming_an_extra_attempt():
    conn = migrated_connection()
    enqueue_link(
        conn,
        channel="C1",
        message_timestamp="100.1",
        thread_ts="100.1",
        normalized_url="https://example.com/story",
        original_url="https://example.com/story",
        permalink="",
        posted_at=100.1,
        now=100.0,
    )
    first = claim_next_job(conn, now=100.0)
    assert first.attempts == 1

    recovered = claim_next_job(conn, now=1_000.0, stale_after_seconds=300)
    assert recovered.attempts == 1
    assert recovered.claim_token != first.claim_token


def test_stale_worker_cannot_finalize_after_new_worker_recovers_claim():
    with tempfile.TemporaryDirectory() as directory:
        database_path = os.path.join(directory, "claims.sqlite")
        first_conn = sqlite3.connect(database_path)
        migrate_db(first_conn, first_conn.cursor())
        enqueue_link(
            first_conn,
            channel="C1",
            message_timestamp="100.1",
            thread_ts="100.1",
            normalized_url="https://example.com/story",
            original_url="https://example.com/story",
            permalink="",
            posted_at=100.1,
            now=100.0,
        )
        first_job = claim_next_job(first_conn, now=100.0)

        second_conn = sqlite3.connect(database_path)
        second_job = claim_next_job(second_conn, now=1_000.0, stale_after_seconds=300)
        document = EnrichedDocument(
            requested_url="https://example.com/story",
            final_url="https://example.com/story",
            canonical_url="https://example.com/story",
            title="Story",
            description="Description",
            content="New worker content",
            content_hash="new",
            extraction_quality="full_text",
            embedding_text="New worker content",
            http_status=200,
        )

        assert complete_job(first_conn, first_job, document, np.array([1.0]), now=1_001.0) is False
        assert complete_job(second_conn, second_job, document, np.array([2.0]), now=1_002.0) is True
        assert second_conn.execute("SELECT content_hash FROM link_documents").fetchone()[0] == "new"


def test_abandoned_claim_recovery_is_bounded():
    conn = migrated_connection()
    enqueue_link(
        conn,
        channel="C1",
        message_timestamp="100.1",
        thread_ts="100.1",
        normalized_url="https://example.com/story",
        original_url="https://example.com/story",
        permalink="",
        posted_at=100.1,
        now=100.0,
    )
    assert claim_next_job(conn, now=100.0).attempts == 1
    assert claim_next_job(conn, now=1_000.0, stale_after_seconds=300).attempts == 1
    assert claim_next_job(conn, now=2_000.0, stale_after_seconds=300).attempts == 1
    assert claim_next_job(conn, now=3_000.0, stale_after_seconds=300) is None
    assert conn.execute("SELECT status FROM link_enrichment_jobs").fetchone()[0] == "failed"


def test_worker_start_does_not_open_database_before_background_retry(monkeypatch):
    worker = LinkEnrichmentWorker(
        "locked.sqlite",
        lambda text: np.array([1.0]),
        poll_interval=0,
        error_backoff=0,
    )

    def locked_database(_database_path):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(link_enrichment_module, "db_connect", locked_database)
    monkeypatch.setattr(worker, "_run", worker._stop.set)

    worker.start()
    worker._thread.join(timeout=1.0)

    assert not worker._thread.is_alive()


def test_worker_starts_and_stops_without_blocking_shutdown():
    with tempfile.TemporaryDirectory() as directory:
        database_path = os.path.join(directory, "worker.sqlite")
        conn = sqlite3.connect(database_path)
        migrate_db(conn, conn.cursor())
        conn.close()
        worker = LinkEnrichmentWorker(database_path, lambda text: np.array([1.0]), poll_interval=0.01)
        worker.start()
        assert worker._thread.daemon is True
        worker.stop(timeout=1.0)
        assert not worker._thread.is_alive()


def test_worker_survives_transient_iteration_failure(monkeypatch):
    calls = []
    worker = LinkEnrichmentWorker(
        "unused.sqlite",
        lambda text: np.array([1.0]),
        poll_interval=0,
        error_backoff=0,
    )

    def flaky_process(*args, **kwargs):
        calls.append("called")
        if len(calls) == 1:
            raise sqlite3.OperationalError("database is locked")
        worker._stop.set()
        return False

    monkeypatch.setattr(link_enrichment_module, "process_next_job", flaky_process)
    worker._run()

    assert calls == ["called", "called"]


def test_matching_callback_failure_does_not_reclassify_completed_fetch():
    with tempfile.TemporaryDirectory() as directory:
        database_path = os.path.join(directory, "callback.sqlite")
        conn = sqlite3.connect(database_path)
        migrate_db(conn, conn.cursor())
        enqueue_link(
            conn,
            channel="C1",
            message_timestamp="100.1",
            thread_ts="100.1",
            normalized_url="https://example.com/story",
            original_url="https://example.com/story",
            permalink="https://slack/story",
            posted_at=100.1,
            now=100.1,
        )
        conn.close()

        def fetcher(url):
            return FetchResult(
                requested_url=url,
                final_url=url,
                status_code=200,
                content_type="text/html",
                body=b"<title>Story</title><p>Enough metadata for a document.</p>",
            )

        with pytest.raises(RuntimeError, match="matching unavailable"):
            process_next_job(
                database_path,
                lambda text: np.array([1.0]),
                fetcher=fetcher,
                on_document_ready=lambda url: (_ for _ in ()).throw(
                    RuntimeError("matching unavailable")
                ),
            )

        conn = sqlite3.connect(database_path)
        assert conn.execute("SELECT status FROM link_enrichment_jobs").fetchone()[0] == "complete"
        assert conn.execute("SELECT fetch_status FROM link_documents").fetchone()[0] == "complete"


def test_idle_worker_retries_pending_duplicate_scan(monkeypatch):
    scans = []
    worker = LinkEnrichmentWorker(
        "unused.sqlite",
        lambda text: np.array([1.0]),
        on_document_ready=lambda url: scans.append(url),
        poll_interval=0,
        error_backoff=0,
    )

    monkeypatch.setattr(link_enrichment_module, "process_next_job", lambda *args, **kwargs: False)

    def scan(url):
        scans.append(url)
        if len(scans) == 1:
            raise sqlite3.OperationalError("database is locked")
        worker._stop.set()

    worker.on_document_ready = scan
    worker._run()

    assert scans == ["", ""]
