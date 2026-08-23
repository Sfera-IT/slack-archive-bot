import os
import sqlite3
import sys

ROOT_DIR = os.path.dirname(os.path.dirname(__file__))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from archive_search import ArchiveSearchEngine, EvidenceRegistry, build_archive_url
from utils import migrate_db


def migrated_connection():
    conn = sqlite3.connect(":memory:")
    migrate_db(conn, conn.cursor())
    return conn


def add_user(conn, user_id, name):
    conn.execute(
        """
        INSERT INTO users(name, id, avatar, is_deleted, real_name, display_name, email)
        VALUES (?, ?, '', 0, ?, ?, '')
        """,
        (name, user_id, name, name),
    )


def add_channel(conn, channel_id, name, *, private=False, members=()):
    conn.execute(
        "INSERT INTO channels(name, id, is_private) VALUES (?, ?, ?)",
        (name, channel_id, int(private)),
    )
    conn.executemany(
        "INSERT INTO members(channel, user) VALUES (?, ?)",
        [(channel_id, member) for member in members],
    )


def add_message(
    conn,
    *,
    text,
    user="U1",
    channel="C1",
    ts="1700000000.1",
    thread_ts=None,
):
    conn.execute(
        """
        INSERT INTO messages(message, user, channel, timestamp, permalink, thread_ts, embeddings)
        VALUES (?, ?, ?, ?, ?, ?, NULL)
        """,
        (
            text,
            user,
            channel,
            ts,
            f"https://sferait-ws.slack.com/archives/{channel}/p{ts.replace('.', '')}",
            thread_ts or ts,
        ),
    )


def seed_base(conn):
    add_user(conn, "U1", "Giorgio")
    add_user(conn, "U2", "Marlon")
    add_user(conn, "UREQUEST", "Fabio")
    add_channel(conn, "C1", "dev")
    add_channel(conn, "C2", "ops")
    conn.commit()


def test_grep_searches_all_public_channels_and_old_posts():
    conn = migrated_connection()
    seed_base(conn)
    add_message(
        conn,
        text="Potremmo fare un talk o un libro su incidents e outages",
        user="U1",
        channel="C1",
        ts="1500000000.1",
    )
    add_message(
        conn,
        text="Un altro post recente sugli incidents",
        user="U2",
        channel="C2",
        ts="1700000000.1",
    )
    conn.commit()

    engine = ArchiveSearchEngine(conn, requester_user_id="UREQUEST")
    payload = engine.grep_archive("talk incidents outages", match_mode="all")

    assert payload["count"] == 1
    assert payload["results"][0]["channel"] == "#dev"
    assert payload["results"][0]["author"] == "Giorgio"
    assert payload["results"][0]["source_id"] == "S1"
    assert payload["searched_scope"].startswith("tutti i canali")


def test_private_channels_are_visible_only_to_members_or_in_current_channel():
    conn = migrated_connection()
    seed_base(conn)
    add_channel(conn, "CPRIVATE", "leadership", private=True, members=("UREQUEST",))
    add_channel(conn, "CHIDDEN", "secret", private=True, members=("U2",))
    add_message(
        conn, text="progetto fenice roadmap", channel="CPRIVATE", ts="1600000000.1"
    )
    add_message(
        conn, text="progetto fenice credenziali", channel="CHIDDEN", ts="1600000001.1"
    )
    conn.commit()

    shared_surface_engine = ArchiveSearchEngine(conn, requester_user_id="UREQUEST")
    shared = shared_surface_engine.grep_archive(
        "progetto fenice", match_mode="all", limit=20
    )
    assert shared["results"] == []

    member_engine = ArchiveSearchEngine(
        conn,
        requester_user_id="UREQUEST",
        allow_member_private_channels=True,
    )
    visible = member_engine.grep_archive("progetto fenice", match_mode="all", limit=20)
    assert [result["channel_id"] for result in visible["results"]] == ["CPRIVATE"]
    assert member_engine.read_thread("CHIDDEN", "1600000001.1")["results"] == []

    current_channel_engine = ArchiveSearchEngine(
        conn,
        requester_user_id="UNLISTED",
        current_channel_id="CHIDDEN",
    )
    current = current_channel_engine.grep_archive("progetto fenice", match_mode="all")
    assert [result["channel_id"] for result in current["results"]] == ["CHIDDEN"]


def test_archive_url_contains_only_validated_ids_and_timestamps():
    url = build_archive_url(
        "C0BSUCGHU8G",
        "1787395457.104349",
        "1787495524.036239",
        base_url="https://sferaarchive-client.vercel.app/?token=secret#fragment",
    )

    assert url == (
        "https://sferaarchive-client.vercel.app/"
        "?channel=C0BSUCGHU8G&thread_ts=1787395457.104349"
        "&message_ts=1787495524.036239"
    )
    assert "token" not in url
    assert build_archive_url("../bad", "1787395457.104349", "1787495524.036239") == ""
    assert build_archive_url("C0BSUCGHU8G", "not-a-ts", "1787495524.036239") == ""


def test_search_excludes_archive_and_ai_opt_out_content():
    conn = migrated_connection()
    seed_base(conn)
    add_user(conn, "U3", "Opted Archive")
    add_user(conn, "U4", "Opted AI")
    add_message(conn, text="incidente atlas", user="U3", ts="1600000000.1")
    add_message(conn, text="incidente atlas", user="U4", ts="1600000001.1")
    add_message(conn, text="incidente atlas verificabile", user="U1", ts="1600000002.1")
    conn.execute("INSERT INTO optout(user, timestamp) VALUES ('U3', 'now')")
    conn.execute("INSERT INTO optout_ai(user, timestamp) VALUES ('U4', 'now')")
    conn.commit()

    engine = ArchiveSearchEngine(conn, requester_user_id="UREQUEST")
    payload = engine.grep_archive("incidente atlas", match_mode="all", limit=20)

    assert payload["count"] == 1
    assert payload["results"][0]["author_id"] == "U1"


def test_thread_and_surrounding_expansion_are_chronological_and_reuse_sources():
    conn = migrated_connection()
    seed_base(conn)
    add_message(conn, text="prima del thread", ts="1600000000.1")
    add_message(
        conn, text="root incident atlas", ts="1600000001.1", thread_ts="1600000001.1"
    )
    add_message(
        conn,
        text="reply con la decisione sul talk",
        user="U2",
        ts="1600000002.1",
        thread_ts="1600000001.1",
    )
    add_message(conn, text="dopo il thread", ts="1600000003.1")
    conn.commit()

    evidence = EvidenceRegistry()
    engine = ArchiveSearchEngine(conn, requester_user_id="UREQUEST", evidence=evidence)
    found = engine.grep_archive("incident atlas", match_mode="all")
    thread = engine.read_thread("C1", found["results"][0]["thread_ts"])
    nearby = engine.read_surrounding("C1", "1600000001.1", before=1, after=2)

    assert [result["text"] for result in thread["results"]] == [
        "root incident atlas",
        "reply con la decisione sul talk",
    ]
    assert thread["results"][0]["source_id"] == found["results"][0]["source_id"]
    assert [result["text"] for result in nearby["results"]] == [
        "prima del thread",
        "root incident atlas",
        "reply con la decisione sul talk",
        "dopo il thread",
    ]


def test_before_timestamp_does_not_return_the_triggering_message_or_future_posts():
    conn = migrated_connection()
    seed_base(conn)
    add_message(conn, text="atlas prima", ts="1600000000.1")
    add_message(conn, text="atlas richiesta corrente", ts="1600000001.1")
    add_message(conn, text="atlas futuro", ts="1600000002.1")
    conn.commit()

    engine = ArchiveSearchEngine(
        conn,
        requester_user_id="UREQUEST",
        before_timestamp="1600000001.1",
    )
    payload = engine.grep_archive("atlas", match_mode="all", limit=20)

    assert [result["text"] for result in payload["results"]] == ["atlas prima"]


def test_like_wildcards_in_user_input_are_matched_literally():
    conn = migrated_connection()
    seed_base(conn)
    add_message(conn, text="deploy 100% finito_con_successo", ts="1600000000.1")
    add_message(conn, text="deploy 100x finitoxconxsuccesso", ts="1600000001.1")
    conn.commit()

    engine = ArchiveSearchEngine(conn, requester_user_id="UREQUEST")
    payload = engine.grep_archive("100% finito_con_successo", match_mode="all")

    assert [result["text"] for result in payload["results"]] == [
        "deploy 100% finito_con_successo"
    ]


def test_relevance_search_considers_old_matches_beyond_recent_candidate_windows():
    conn = migrated_connection()
    seed_base(conn)
    add_message(
        conn,
        text="needle exact historical phrase",
        ts="1400000000.1",
    )
    for index in range(5001):
        add_message(
            conn,
            text=f"needle rumore recente {index}",
            ts=f"17{index:08d}.1",
        )
    conn.commit()

    engine = ArchiveSearchEngine(conn, requester_user_id="UREQUEST")
    payload = engine.grep_archive(
        "needle exact historical phrase",
        match_mode="any",
        sort="relevance",
        limit=1,
    )

    assert payload["results"][0]["text"] == "needle exact historical phrase"


def test_search_can_explicitly_sort_oldest_matches_first():
    conn = migrated_connection()
    seed_base(conn)
    add_message(conn, text="incident atlas vecchio", ts="1500000000.1")
    add_message(conn, text="incident atlas recente", ts="1700000000.1")
    conn.commit()

    engine = ArchiveSearchEngine(conn, requester_user_id="UREQUEST")
    payload = engine.grep_archive(
        "incident atlas",
        match_mode="all",
        sort="oldest",
    )

    assert [result["text"] for result in payload["results"]] == [
        "incident atlas vecchio",
        "incident atlas recente",
    ]
