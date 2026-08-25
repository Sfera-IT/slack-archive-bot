import os
import sqlite3
import sys

import httpx
import pytest


ROOT_DIR = os.path.dirname(os.path.dirname(__file__))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

import instagram_media as instagram_media_module

from instagram_media import (
    DownloadedMedia,
    InstagramMediaPolicy,
    InstagramMediaWorker,
    MediaDownloadError,
    MediaExtractionError,
    RemoteMedia,
    claim_instagram_media_job,
    download_remote_media,
    enqueue_instagram_media,
    extract_instagram_post_urls,
    mark_instagram_job_uploading,
    process_instagram_media_job,
    resolve_instagram_media,
)
from utils import migrate_db


PUBLIC_IP = "93.184.216.34"


def public_resolver(host, port, type=None, timeout=None):
    return [(2, type, 6, "", (PUBLIC_IP, port))]


def migrated_connection():
    conn = sqlite3.connect(":memory:")
    migrate_db(conn, conn.cursor())
    return conn


@pytest.mark.parametrize(
    ("name", "value", "attribute", "default"),
    [
        ("INSTAGRAM_MEDIA_MAX_ITEMS", "oops", "max_items", 10),
        ("INSTAGRAM_MEDIA_TOTAL_TIMEOUT_SECONDS", "0", "total_timeout", 30.0),
    ],
)
def test_policy_invalid_or_nonpositive_values_use_conservative_defaults(
    monkeypatch, name, value, attribute, default
):
    monkeypatch.setenv(name, value)

    assert getattr(InstagramMediaPolicy.from_env(), attribute) == default


def test_extract_instagram_post_urls_accepts_supported_posts_and_deduplicates():
    text = (
        "<https://www.instagram.com/reel/ABC_123/?igsh=tracking|reel> "
        "https://instagram.com/p/Photo-9/ "
        "https://www.instagram.com/tv/OldTv42/?utm_source=slack "
        "https://instagram.com/reel/ABC_123/"
    )

    assert extract_instagram_post_urls(text) == [
        "https://www.instagram.com/reel/ABC_123/",
        "https://www.instagram.com/p/Photo-9/",
        "https://www.instagram.com/tv/OldTv42/",
    ]


def test_extract_instagram_post_urls_rejects_profiles_lookalikes_and_credentials():
    text = (
        "https://instagram.com/example/ "
        "http://instagram.com/reel/INSECURE/ "
        "https://instagram.com:444/reel/PORT/ "
        "https://instagram.example/reel/ABC/ "
        "https://user:password@instagram.com/reel/SECRET/ "
        "https://example.com/?next=https://instagram.com/reel/NESTED/"
    )

    assert extract_instagram_post_urls(text) == []


def test_enqueue_and_claim_instagram_jobs_are_durable_and_idempotent():
    conn = migrated_connection()
    url = "https://www.instagram.com/reel/ABC_123/"

    assert enqueue_instagram_media(
        conn,
        channel="C1",
        message_timestamp="100.1",
        thread_ts="99.9",
        instagram_url=url,
        now=200.0,
    ) is True
    assert enqueue_instagram_media(
        conn,
        channel="C1",
        message_timestamp="100.1",
        thread_ts="99.9",
        instagram_url=url,
        now=200.0,
    ) is False

    job = claim_instagram_media_job(conn, now=201.0)
    assert job.channel == "C1"
    assert job.message_timestamp == "100.1"
    assert job.thread_ts == "99.9"
    assert job.instagram_url == url
    assert job.shortcode == "ABC_123"
    assert job.attempts == 1
    assert claim_instagram_media_job(conn, now=201.0) is None


def test_instagram_job_persists_source_author():
    conn = migrated_connection()

    enqueue_instagram_media(
        conn,
        channel="C1",
        message_timestamp="100.1",
        thread_ts="100.1",
        instagram_url="https://www.instagram.com/p/AUTHOR/",
        author="U123",
        now=200.0,
    )

    job = claim_instagram_media_job(conn, now=201.0)
    assert job.author == "U123"


def test_optout_immediately_before_upload_cancels_claimed_job(tmp_path):
    conn = migrated_connection()
    enqueue_instagram_media(
        conn,
        channel="C1",
        message_timestamp="100.1",
        thread_ts="100.1",
        instagram_url="https://www.instagram.com/p/OPTOUT/",
        author="U123",
        now=200.0,
    )
    job = claim_instagram_media_job(conn, now=201.0)
    assert job is not None
    conn.execute(
        "INSERT INTO optout (user, timestamp) VALUES (?, CURRENT_TIMESTAMP)",
        ("U123",),
    )
    conn.commit()
    uploads = []

    def downloader(media, **kwargs):
        path = tmp_path / "one.jpg"
        path.write_bytes(b"one")
        return DownloadedMedia(path=path, index=1, is_video=False, size=3)

    slack_client = type(
        "SlackClient",
        (),
        {"files_upload_v2": lambda self, **kwargs: uploads.append(kwargs)},
    )()

    assert process_instagram_media_job(
        conn,
        job,
        slack_client,
        media_resolver=lambda shortcode, max_items: [
            RemoteMedia("https://cdn.example/one.jpg", 1, False)
        ],
        media_downloader=downloader,
        now=202.0,
    ) is False
    assert uploads == []
    assert conn.execute(
        "SELECT status, claim_token FROM instagram_media_jobs"
    ).fetchone() == ("cancelled", None)


def test_same_instagram_post_is_deduplicated_within_a_slack_thread():
    conn = migrated_connection()
    url = "https://www.instagram.com/p/Photo9/"

    assert enqueue_instagram_media(
        conn,
        channel="C1",
        message_timestamp="100.1",
        thread_ts="99.9",
        instagram_url=url,
        now=200.0,
    ) is True
    assert enqueue_instagram_media(
        conn,
        channel="C1",
        message_timestamp="100.2",
        thread_ts="99.9",
        instagram_url=url,
        now=201.0,
    ) is False
    assert enqueue_instagram_media(
        conn,
        channel="C1",
        message_timestamp="101.1",
        thread_ts="101.1",
        instagram_url=url,
        now=202.0,
    ) is True


def test_repeated_migration_does_not_restore_a_deleted_source():
    conn = migrated_connection()
    url = "https://www.instagram.com/p/SHARED/"
    enqueue_instagram_media(
        conn,
        channel="C1",
        message_timestamp="100.1",
        thread_ts="100.1",
        author="U1",
        instagram_url=url,
        now=1.0,
    )
    enqueue_instagram_media(
        conn,
        channel="C1",
        message_timestamp="100.2",
        thread_ts="100.1",
        author="U2",
        instagram_url=url,
        now=1.0,
    )

    instagram_media_module.cancel_instagram_media_jobs_for_message(
        conn, channel="C1", message_timestamp="100.1"
    )
    migrate_db(conn, conn.cursor())

    assert conn.execute(
        "SELECT message_timestamp FROM instagram_media_sources ORDER BY message_timestamp"
    ).fetchall() == [("100.2",)]


def test_legacy_message_keyed_queue_is_rebuilt_fail_closed():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE instagram_media_jobs (
            channel TEXT NOT NULL,
            message_timestamp TEXT NOT NULL,
            thread_ts TEXT NOT NULL,
            instagram_url TEXT NOT NULL,
            shortcode TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            next_attempt_at REAL NOT NULL DEFAULT 0,
            claimed_at REAL,
            claim_token TEXT,
            last_error TEXT,
            completed_at REAL,
            PRIMARY KEY (channel, message_timestamp, shortcode)
        )
        """
    )
    conn.execute(
        """
        INSERT INTO instagram_media_jobs
        (channel, message_timestamp, thread_ts, instagram_url, shortcode, status)
        VALUES ('C1', '100.1', '100.1',
                'https://www.instagram.com/p/LEGACY/', 'LEGACY', 'pending')
        """
    )
    conn.commit()

    migrate_db(conn, conn.cursor())

    pk_columns = [
        row[1]
        for row in sorted(
            (row for row in conn.execute("PRAGMA table_info(instagram_media_jobs)") if row[5]),
            key=lambda row: row[5],
        )
    ]
    assert pk_columns == ["channel", "thread_ts", "shortcode"]
    assert conn.execute(
        "SELECT status FROM instagram_media_jobs WHERE shortcode = 'LEGACY'"
    ).fetchone() == ("cancelled",)
    assert enqueue_instagram_media(
        conn,
        channel="C1",
        message_timestamp="100.2",
        thread_ts="100.1",
        author="U2",
        instagram_url="https://www.instagram.com/p/LEGACY/",
        now=2.0,
    ) is True


def test_unknown_source_author_cannot_transition_to_uploading():
    conn = migrated_connection()
    enqueue_instagram_media(
        conn,
        channel="C1",
        message_timestamp="100.1",
        thread_ts="100.1",
        author="",
        instagram_url="https://www.instagram.com/p/UNKNOWN/",
        now=1.0,
    )
    job = claim_instagram_media_job(conn, now=2.0)
    assert job is not None

    assert mark_instagram_job_uploading(conn, job, now=3.0) is False
    assert conn.execute(
        "SELECT status FROM instagram_media_jobs WHERE shortcode = 'UNKNOWN'"
    ).fetchone() == ("cancelled",)


def test_deleting_first_of_two_thread_sources_keeps_deduplicated_job_pending():
    conn = migrated_connection()
    url = "https://www.instagram.com/p/SHARED/"

    assert enqueue_instagram_media(
        conn,
        channel="C1",
        message_timestamp="100.1",
        thread_ts="99.9",
        instagram_url=url,
        author="U1",
        now=200.0,
    ) is True
    assert enqueue_instagram_media(
        conn,
        channel="C1",
        message_timestamp="100.2",
        thread_ts="99.9",
        instagram_url=url,
        author="U2",
        now=201.0,
    ) is False

    assert instagram_media_module.cancel_instagram_media_jobs_for_message(
        conn, channel="C1", message_timestamp="100.1"
    ) == 0
    assert conn.execute(
        "SELECT status FROM instagram_media_jobs"
    ).fetchone() == ("pending",)

    assert instagram_media_module.cancel_instagram_media_jobs_for_message(
        conn, channel="C1", message_timestamp="100.2"
    ) == 1
    assert conn.execute(
        "SELECT status FROM instagram_media_jobs"
    ).fetchone() == ("cancelled",)


def test_edit_removing_one_of_two_thread_sources_keeps_deduplicated_job_pending():
    conn = migrated_connection()
    url = "https://www.instagram.com/p/SHARED/"
    for message_timestamp, author in (("100.1", "U1"), ("100.2", "U2")):
        enqueue_instagram_media(
            conn,
            channel="C1",
            message_timestamp=message_timestamp,
            thread_ts="99.9",
            instagram_url=url,
            author=author,
            now=200.0,
        )

    queued, cancelled = instagram_media_module.reconcile_instagram_media_jobs(
        conn,
        channel="C1",
        message_timestamp="100.1",
        thread_ts="99.9",
        author="U1",
        instagram_urls=[],
        now=201.0,
    )

    assert (queued, cancelled) == (0, 0)
    assert conn.execute(
        "SELECT status FROM instagram_media_jobs"
    ).fetchone() == ("pending",)


def test_preupload_optout_uses_any_remaining_nonopted_source(tmp_path):
    conn = migrated_connection()
    url = "https://www.instagram.com/p/SHARED/"
    for message_timestamp, author in (("100.1", "U1"), ("100.2", "U2")):
        enqueue_instagram_media(
            conn,
            channel="C1",
            message_timestamp=message_timestamp,
            thread_ts="99.9",
            instagram_url=url,
            author=author,
            now=200.0,
        )
    job = claim_instagram_media_job(conn, now=201.0)
    assert job is not None
    conn.execute(
        "INSERT INTO optout (user, timestamp) VALUES (?, CURRENT_TIMESTAMP)",
        ("U1",),
    )
    conn.commit()
    uploads = []

    def downloader(media, **kwargs):
        path = tmp_path / "one.jpg"
        path.write_bytes(b"one")
        return DownloadedMedia(path=path, index=1, is_video=False, size=3)

    slack_client = type(
        "SlackClient",
        (),
        {"files_upload_v2": lambda self, **kwargs: uploads.append(kwargs)},
    )()

    assert process_instagram_media_job(
        conn,
        job,
        slack_client,
        media_resolver=lambda shortcode, max_items: [
            RemoteMedia("https://cdn.example/one.jpg", 1, False)
        ],
        media_downloader=downloader,
        now=202.0,
    ) is True
    assert len(uploads) == 1


def test_edit_reconciliation_enqueues_added_and_cancels_removed_claim_safely():
    conn = migrated_connection()
    for shortcode in ("KEEP", "REMOVE", "PROCESS", "UPLOAD"):
        enqueue_instagram_media(
            conn,
            channel="C1",
            message_timestamp="100.1",
            thread_ts="100.1",
            author="U1",
            instagram_url=f"https://www.instagram.com/p/{shortcode}/",
            now=200.0,
        )
    conn.execute(
        "UPDATE instagram_media_jobs SET next_attempt_at = 999 "
        "WHERE shortcode != 'PROCESS'"
    )
    conn.commit()
    processing = claim_instagram_media_job(conn, now=201.0)
    conn.execute(
        "UPDATE instagram_media_jobs SET status = 'processing', claim_token = 'upload', "
        "claimed_at = 201 WHERE shortcode = 'UPLOAD'"
    )
    conn.commit()
    uploading = processing.__class__(
        channel="C1",
        message_timestamp="100.1",
        thread_ts="100.1",
        author="U1",
        instagram_url="https://www.instagram.com/p/UPLOAD/",
        shortcode="UPLOAD",
        attempts=1,
        claim_token="upload",
    )
    assert mark_instagram_job_uploading(conn, uploading, now=202.0) is True

    queued, cancelled = instagram_media_module.reconcile_instagram_media_jobs(
        conn,
        channel="C1",
        message_timestamp="100.1",
        thread_ts="100.1",
        author="U1",
        instagram_urls=[
            "https://www.instagram.com/p/KEEP/",
            "https://www.instagram.com/p/NEW/",
        ],
        now=203.0,
    )

    assert (queued, cancelled) == (1, 2)
    assert dict(
        conn.execute(
            "SELECT shortcode, status FROM instagram_media_jobs ORDER BY shortcode"
        ).fetchall()
    ) == {
        "KEEP": "pending",
        "NEW": "pending",
        "PROCESS": "cancelled",
        "REMOVE": "cancelled",
        "UPLOAD": "uploading",
    }
    assert mark_instagram_job_uploading(conn, processing, now=204.0) is False


def test_message_deletion_cancels_only_pending_and_processing_jobs():
    conn = migrated_connection()
    for shortcode in ("PENDING", "PROCESSING", "UPLOADING"):
        enqueue_instagram_media(
            conn,
            channel="C1",
            message_timestamp="100.1",
            thread_ts="100.1",
            author="U1",
            instagram_url=f"https://www.instagram.com/p/{shortcode}/",
            now=200.0,
        )
    conn.execute(
        "UPDATE instagram_media_jobs SET status = 'processing', claim_token = 'p' "
        "WHERE shortcode = 'PROCESSING'"
    )
    conn.execute(
        "UPDATE instagram_media_jobs SET status = 'uploading', claim_token = 'u' "
        "WHERE shortcode = 'UPLOADING'"
    )
    conn.commit()

    assert instagram_media_module.cancel_instagram_media_jobs_for_message(
        conn, channel="C1", message_timestamp="100.1"
    ) == 2
    assert dict(
        conn.execute("SELECT shortcode, status FROM instagram_media_jobs").fetchall()
    ) == {
        "PENDING": "cancelled",
        "PROCESSING": "cancelled",
        "UPLOADING": "uploading",
    }


def test_mark_uploading_is_claim_token_guarded_and_not_reclaimable():
    conn = migrated_connection()
    enqueue_instagram_media(
        conn,
        channel="C1",
        message_timestamp="100.1",
        thread_ts="100.1",
        author="U1",
        instagram_url="https://www.instagram.com/p/Photo9/",
        now=200.0,
    )
    job = claim_instagram_media_job(conn, now=201.0)

    assert mark_instagram_job_uploading(conn, job, now=202.0) is True
    assert mark_instagram_job_uploading(
        conn,
        job.__class__(**{**job.__dict__, "claim_token": "stale"}),
        now=203.0,
    ) is False
    assert claim_instagram_media_job(conn, now=10_000.0, stale_after_seconds=1) is None
    assert conn.execute(
        "SELECT status FROM instagram_media_jobs"
    ).fetchone()[0] == "uploading"


def test_job_state_updates_use_immutable_thread_identity():
    conn = migrated_connection()
    enqueue_instagram_media(
        conn,
        channel="C1",
        message_timestamp="100.1",
        thread_ts="99.9",
        author="U1",
        instagram_url="https://www.instagram.com/p/IDENTITY/",
        now=200.0,
    )
    job = claim_instagram_media_job(conn, now=201.0)
    assert job is not None
    conn.execute(
        "UPDATE instagram_media_jobs SET message_timestamp = 'corrected-source-ts'"
    )
    conn.commit()

    assert mark_instagram_job_uploading(conn, job, now=202.0) is True
    assert conn.execute(
        "SELECT status FROM instagram_media_jobs"
    ).fetchone()[0] == "uploading"


def test_stale_job_at_attempt_limit_becomes_terminal():
    conn = migrated_connection()
    enqueue_instagram_media(
        conn,
        channel="C1",
        message_timestamp="100.1",
        thread_ts="100.1",
        instagram_url="https://www.instagram.com/p/STALE/",
        now=0.0,
    )
    job = claim_instagram_media_job(conn, now=1.0)
    conn.execute(
        "UPDATE instagram_media_jobs SET attempts = 3, claimed_at = 1.0 "
        "WHERE claim_token = ?",
        (job.claim_token,),
    )
    conn.commit()

    assert claim_instagram_media_job(
        conn,
        now=100.0,
        stale_after_seconds=1.0,
        max_attempts=3,
    ) is None
    assert conn.execute(
        "SELECT status FROM instagram_media_jobs"
    ).fetchone()[0] == "failed"


def test_download_remote_media_streams_to_disk_with_content_type_extension(tmp_path):
    media = RemoteMedia(
        url="https://cdninstagram.example/media/one",
        index=1,
        is_video=False,
    )
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                headers={"content-type": "image/jpeg"},
                content=b"jpeg-bytes",
            )
        )
    )

    downloaded = download_remote_media(
        media,
        shortcode="ABC_123",
        output_dir=tmp_path,
        policy=InstagramMediaPolicy(max_file_bytes=100),
        client=client,
        resolver=public_resolver,
    )

    assert downloaded.path.name == "instagram-ABC_123-01.jpg"
    assert downloaded.path.read_bytes() == b"jpeg-bytes"
    assert downloaded.is_video is False


def test_download_remote_media_accepts_stdlib_resolver_signature(tmp_path):
    media = RemoteMedia(
        url="https://cdninstagram.example/media/one",
        index=1,
        is_video=False,
    )
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                headers={"content-type": "image/jpeg"},
                content=b"jpeg-bytes",
            )
        )
    )

    def stdlib_resolver(host, port, family=0, type=0):
        return [(2, type, 6, "", (PUBLIC_IP, port))]

    downloaded = download_remote_media(
        media,
        shortcode="ABC_123",
        output_dir=tmp_path,
        policy=InstagramMediaPolicy(max_file_bytes=100),
        client=client,
        resolver=stdlib_resolver,
    )

    assert downloaded.size == len(b"jpeg-bytes")


def test_download_remote_media_falls_back_to_next_public_address(tmp_path):
    media = RemoteMedia(
        url="https://cdninstagram.example/media/one",
        index=1,
        is_video=False,
    )
    attempted_hosts = []

    def transport(request):
        attempted_hosts.append(request.url.host)
        if ":" in request.url.host:
            raise httpx.ConnectError("IPv6 unreachable", request=request)
        return httpx.Response(
            200,
            headers={"content-type": "image/jpeg"},
            content=b"jpeg-bytes",
        )

    def dual_stack_resolver(host, port, type=None, timeout=None):
        return [
            (10, type, 6, "", ("2a02:1420:107:800:face:b00c:3333:a3f", port)),
            (2, type, 6, "", ("62.127.100.35", port)),
        ]

    downloaded = download_remote_media(
        media,
        shortcode="ABC_123",
        output_dir=tmp_path,
        policy=InstagramMediaPolicy(max_file_bytes=100),
        client=httpx.Client(transport=httpx.MockTransport(transport)),
        resolver=dual_stack_resolver,
    )

    assert attempted_hosts == [
        "2a02:1420:107:800:face:b00c:3333:a3f",
        "62.127.100.35",
    ]
    assert downloaded.size == len(b"jpeg-bytes")


def test_download_remote_media_rejects_unsupported_and_oversized_files(tmp_path):
    media = RemoteMedia(
        url="https://cdninstagram.example/media/one",
        index=1,
        is_video=False,
    )
    unsupported_client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                headers={"content-type": "text/html"},
                content=b"login",
            )
        )
    )
    with pytest.raises(MediaDownloadError, match="Unsupported media content type"):
        download_remote_media(
            media,
            shortcode="ABC",
            output_dir=tmp_path,
            client=unsupported_client,
            resolver=public_resolver,
        )

    oversized_client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                headers={"content-type": "video/mp4"},
                content=b"x" * 11,
            )
        )
    )
    with pytest.raises(MediaDownloadError, match="configured byte limit"):
        download_remote_media(
            RemoteMedia(
                url="https://cdninstagram.example/media/two",
                index=2,
                is_video=True,
            ),
            shortcode="ABC",
            output_dir=tmp_path,
            policy=InstagramMediaPolicy(max_file_bytes=10),
            client=oversized_client,
            resolver=public_resolver,
        )


def test_resolve_instagram_media_preserves_mixed_carousel_order():
    class Node:
        def __init__(self, *, is_video, display_url, video_url=None):
            self.is_video = is_video
            self.display_url = display_url
            self.video_url = video_url

    class Post:
        typename = "GraphSidecar"

        def get_sidecar_nodes(self):
            return iter(
                [
                    Node(is_video=False, display_url="https://cdn.example/one.jpg"),
                    Node(
                        is_video=True,
                        display_url="https://cdn.example/two-cover.jpg",
                        video_url="https://cdn.example/two.mp4",
                    ),
                ]
            )

    media = resolve_instagram_media(
        "ABC_123", max_items=10, post_loader=lambda shortcode: Post()
    )

    assert media == [
        RemoteMedia(url="https://cdn.example/one.jpg", index=1, is_video=False),
        RemoteMedia(url="https://cdn.example/two.mp4", index=2, is_video=True),
    ]


def test_resolve_instagram_media_handles_single_image_and_rejects_oversized_carousel():
    class ImagePost:
        typename = "GraphImage"
        is_video = False
        url = "https://cdn.example/image.jpg"
        video_url = None

    assert resolve_instagram_media(
        "PHOTO", max_items=10, post_loader=lambda shortcode: ImagePost()
    ) == [
        RemoteMedia(url="https://cdn.example/image.jpg", index=1, is_video=False)
    ]

    class SidecarPost:
        typename = "GraphSidecar"

        def get_sidecar_nodes(self):
            node = type(
                "Node",
                (),
                {
                    "is_video": False,
                    "display_url": "https://cdn.example/image.jpg",
                    "video_url": None,
                },
            )()
            return iter([node, node, node])

    with pytest.raises(MediaExtractionError, match="more than 2 media items"):
        resolve_instagram_media(
            "TOO_MANY", max_items=2, post_loader=lambda shortcode: SidecarPost()
        )


def test_process_instagram_media_job_uploads_all_files_once_and_completes(tmp_path):
    conn = migrated_connection()
    url = "https://www.instagram.com/p/CAROUSEL/"
    enqueue_instagram_media(
        conn,
        channel="C1",
        message_timestamp="100.1",
        thread_ts="99.9",
        author="U1",
        instagram_url=url,
        now=200.0,
    )
    job = claim_instagram_media_job(conn, now=201.0)
    calls = []

    class SlackClient:
        def files_upload_v2(self, **kwargs):
            calls.append(kwargs)
            return {"ok": True, "files": [{"id": "F1"}, {"id": "F2"}]}

    def downloader(media, **kwargs):
        suffix = ".mp4" if media.is_video else ".jpg"
        path = tmp_path / f"item-{media.index}{suffix}"
        path.write_bytes(b"x" * media.index)
        return DownloadedMedia(
            path=path,
            index=media.index,
            is_video=media.is_video,
            size=media.index,
        )

    assert process_instagram_media_job(
        conn,
        job,
        SlackClient(),
        policy=InstagramMediaPolicy(max_total_bytes=10),
        media_resolver=lambda shortcode, max_items: [
            RemoteMedia("https://cdn.example/1.jpg", 1, False),
            RemoteMedia("https://cdn.example/2.mp4", 2, True),
        ],
        media_downloader=downloader,
        now=202.0,
    ) is True

    assert len(calls) == 1
    assert calls[0]["channel"] == "C1"
    assert calls[0]["thread_ts"] == "99.9"
    assert calls[0]["initial_comment"] == (
        "📦 Copia archivio Instagram: https://www.instagram.com/p/CAROUSEL/"
    )
    assert [item["filename"] for item in calls[0]["file_uploads"]] == [
        "instagram-CAROUSEL-01.jpg",
        "instagram-CAROUSEL-02.mp4",
    ]
    assert conn.execute(
        "SELECT status, completed_at FROM instagram_media_jobs"
    ).fetchone() == ("complete", 202.0)


def test_process_instagram_media_job_limits_each_download_to_remaining_total(tmp_path):
    conn = migrated_connection()
    enqueue_instagram_media(
        conn,
        channel="C1",
        message_timestamp="100.1",
        thread_ts="100.1",
        instagram_url="https://www.instagram.com/p/TOTAL/",
        now=200.0,
    )
    job = claim_instagram_media_job(conn, now=201.0)
    observed_limits = []

    def downloader(media, **kwargs):
        observed_limits.append(kwargs["policy"].max_file_bytes)
        path = tmp_path / f"{media.index}.jpg"
        path.write_bytes(b"x" * 6)
        return DownloadedMedia(
            path=path,
            index=media.index,
            is_video=False,
            size=6,
        )

    assert process_instagram_media_job(
        conn,
        job,
        object(),
        policy=InstagramMediaPolicy(max_file_bytes=100, max_total_bytes=10),
        media_resolver=lambda shortcode, max_items: [
            RemoteMedia("https://cdn.example/one.jpg", 1, False),
            RemoteMedia("https://cdn.example/two.jpg", 2, False),
        ],
        media_downloader=downloader,
        now=202.0,
    ) is False

    assert observed_limits == [10, 4]


def test_process_instagram_media_job_retries_extraction_but_not_uncertain_upload(tmp_path):
    conn = migrated_connection()
    url = "https://www.instagram.com/reel/RETRY/"
    enqueue_instagram_media(
        conn,
        channel="C1",
        message_timestamp="100.1",
        thread_ts="100.1",
        author="U1",
        instagram_url=url,
        now=200.0,
    )
    job = claim_instagram_media_job(conn, now=201.0)

    assert process_instagram_media_job(
        conn,
        job,
        object(),
        media_resolver=lambda shortcode, max_items: (_ for _ in ()).throw(
            MediaExtractionError("rate limited")
        ),
        now=202.0,
    ) is False
    status, next_attempt_at = conn.execute(
        "SELECT status, next_attempt_at FROM instagram_media_jobs"
    ).fetchone()
    assert status == "pending"
    assert next_attempt_at > 202.0

    retried = claim_instagram_media_job(conn, now=next_attempt_at)

    class UncertainSlackClient:
        def files_upload_v2(self, **kwargs):
            raise TimeoutError("upload response lost")

    def downloader(media, **kwargs):
        path = tmp_path / "one.jpg"
        path.write_bytes(b"one")
        return DownloadedMedia(path=path, index=1, is_video=False, size=3)

    assert process_instagram_media_job(
        conn,
        retried,
        UncertainSlackClient(),
        media_resolver=lambda shortcode, max_items: [
            RemoteMedia("https://cdn.example/one.jpg", 1, False)
        ],
        media_downloader=downloader,
        now=next_attempt_at + 1,
    ) is False
    assert conn.execute(
        "SELECT status FROM instagram_media_jobs"
    ).fetchone()[0] == "publication_uncertain"


def test_worker_process_once_claims_and_completes_a_durable_job(tmp_path):
    database_path = tmp_path / "archive.sqlite"
    conn = sqlite3.connect(database_path)
    migrate_db(conn, conn.cursor())
    enqueue_instagram_media(
        conn,
        channel="C1",
        message_timestamp="100.1",
        thread_ts="100.1",
        author="U1",
        instagram_url="https://www.instagram.com/p/PHOTO/",
        now=0.0,
    )
    conn.close()

    class SlackClient:
        def files_upload_v2(self, **kwargs):
            return {"ok": True, "files": [{"id": "F1"}]}

    def downloader(media, **kwargs):
        path = kwargs["output_dir"] / "one.jpg"
        path.write_bytes(b"one")
        return DownloadedMedia(path=path, index=1, is_video=False, size=3)

    worker = InstagramMediaWorker(
        str(database_path),
        SlackClient(),
        policy=InstagramMediaPolicy(),
        media_resolver=lambda shortcode, max_items: [
            RemoteMedia("https://cdn.example/one.jpg", 1, False)
        ],
        media_downloader=downloader,
    )

    assert worker.process_once(now=1.0) is True
    conn = sqlite3.connect(database_path)
    assert conn.execute(
        "SELECT status FROM instagram_media_jobs"
    ).fetchone()[0] == "complete"
    conn.close()
