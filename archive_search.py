"""Privacy-aware lexical retrieval over the archived Slack database.

The archive is intentionally searched with deterministic SQL rather than the
legacy embeddings column.  GPT can iterate over these bounded tools, but it can
never bypass channel visibility or AI opt-out rules enforced here.
"""

from __future__ import annotations

import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

ROME = ZoneInfo("Europe/Rome")
MAX_QUERY_CHARS = 240
MAX_RESULTS = 20
MAX_MESSAGE_CHARS = 700
OPTED_OUT_TEXT = "User opted out of archiving. This message has been deleted"
DEFAULT_ARCHIVE_FRONTEND_URL = "https://sferaarchive-client.vercel.app/"
_SLACK_CHANNEL_ID_RE = re.compile(r"^[A-Z][A-Z0-9]{8,}$")
_SLACK_TIMESTAMP_RE = re.compile(r"^\d{10,16}\.\d{1,6}$")


def is_valid_slack_timestamp(value: object) -> bool:
    """Return whether *value* is a canonical Slack message timestamp."""
    return _SLACK_TIMESTAMP_RE.fullmatch(str(value or "")) is not None


def build_archive_url(
    channel_id: str,
    thread_ts: str,
    message_ts: str,
    *,
    base_url: str | None = None,
) -> str:
    """Build a credential-free SferaArchive deep link from validated Slack IDs."""
    channel_id = str(channel_id or "")
    thread_ts = str(thread_ts or "")
    message_ts = str(message_ts or "")
    if not _SLACK_CHANNEL_ID_RE.fullmatch(channel_id):
        return ""
    if not is_valid_slack_timestamp(thread_ts):
        return ""
    if not is_valid_slack_timestamp(message_ts):
        return ""

    configured_base = (
        base_url
        or os.getenv("SFERAARCHIVE_FRONTEND_URL")
        or os.getenv("CLIENT_URL")
        or DEFAULT_ARCHIVE_FRONTEND_URL
    ).strip()
    parsed = urlsplit(configured_base)
    if parsed.scheme not in {"https", "http"} or not parsed.netloc:
        return ""
    if parsed.username or parsed.password:
        return ""

    clean_base = urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path or "/", "", "")
    )
    return clean_base + "?" + urlencode(
        {
            "channel": channel_id,
            "thread_ts": thread_ts,
            "message_ts": message_ts,
        }
    )


@dataclass(frozen=True)
class ArchiveHit:
    text: str
    user_id: str
    user_name: str
    channel_id: str
    channel_name: str
    timestamp: str
    permalink: str
    thread_ts: str
    score: float = 0.0

    @property
    def date_label(self) -> str:
        try:
            return datetime.fromtimestamp(float(self.timestamp), tz=ROME).strftime(
                "%Y-%m-%d %H:%M %Z"
            )
        except (TypeError, ValueError, OSError):
            return "data sconosciuta"

    @property
    def archive_url(self) -> str:
        return build_archive_url(self.channel_id, self.thread_ts, self.timestamp)


class EvidenceRegistry:
    """Assign stable source IDs to unique Slack messages during one answer."""

    def __init__(self):
        self._by_key: dict[tuple[str, str], str] = {}
        self._hits: dict[str, ArchiveHit] = {}

    def register(self, hit: ArchiveHit) -> str:
        key = (hit.channel_id, hit.timestamp)
        source_id = self._by_key.get(key)
        if source_id is None:
            source_id = f"S{len(self._hits) + 1}"
            self._by_key[key] = source_id
            self._hits[source_id] = hit
        return source_id

    def get(self, source_id: str) -> ArchiveHit | None:
        return self._hits.get(source_id)

    def ids(self) -> list[str]:
        return list(self._hits)

    def serialize(self, hits: list[ArchiveHit]) -> dict:
        results = []
        for hit in hits:
            source_id = self.register(hit)
            results.append(
                {
                    "source_id": source_id,
                    "date": hit.date_label,
                    "channel": f"#{hit.channel_name}",
                    "channel_id": hit.channel_id,
                    "author": hit.user_name,
                    "author_id": hit.user_id,
                    "text": _compact_text(hit.text),
                    "thread_ts": hit.thread_ts,
                    "message_ts": hit.timestamp,
                    "permalink": hit.permalink,
                    "archive_url": hit.archive_url,
                }
            )
        return {"count": len(results), "results": results}


class ArchiveSearchEngine:
    """Search every archived message that the requesting user may access."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        requester_user_id: str,
        current_channel_id: str = "",
        before_timestamp: str | None = None,
        allow_member_private_channels: bool = False,
        evidence: EvidenceRegistry | None = None,
    ):
        self.conn = conn
        self.requester_user_id = requester_user_id or ""
        self.current_channel_id = current_channel_id or ""
        self.before_timestamp = before_timestamp
        self.allow_member_private_channels = bool(allow_member_private_channels)
        self.evidence = evidence or EvidenceRegistry()

    def grep_archive(
        self,
        query: str,
        *,
        match_mode: str = "all",
        channel: str = "",
        user: str = "",
        after: str = "",
        before: str = "",
        sort: str = "relevance",
        limit: int = 10,
    ) -> dict:
        """Run a grep-like case-insensitive search over messages and metadata."""
        clean_query = _clean_query(query)
        if not clean_query:
            return {"count": 0, "results": [], "error": "query vuota"}

        mode = match_mode if match_mode in {"all", "any", "phrase"} else "all"
        terms = _search_terms(clean_query, phrase=(mode == "phrase"))
        if not terms:
            return {"count": 0, "results": [], "error": "nessun termine ricercabile"}

        where, params = self._base_where()
        term_clauses = []
        for term in terms:
            pattern = f"%{_escape_like(term.lower())}%"
            term_clauses.append(
                "(LOWER(m.message) LIKE ? ESCAPE '\\' "
                "OR LOWER(COALESCE(u.display_name, u.name, u.real_name, '')) LIKE ? ESCAPE '\\' "
                "OR LOWER(c.name) LIKE ? ESCAPE '\\')"
            )
            params.extend([pattern, pattern, pattern])
        joiner = " OR " if mode == "any" else " AND "
        where.append("(" + joiner.join(term_clauses) + ")")

        if channel:
            where.append("(LOWER(c.name) = LOWER(?) OR m.channel = ?)")
            params.extend([channel.lstrip("#"), channel])
        if user:
            where.append(
                "(LOWER(COALESCE(u.display_name, u.name, u.real_name, '')) LIKE ? ESCAPE '\\' "
                "OR m.user = ?)"
            )
            params.extend([f"%{_escape_like(user.lower().lstrip('@'))}%", user])

        after_ts = _parse_date_boundary(after, end_of_day=False)
        before_ts = _parse_date_boundary(before, end_of_day=True)
        if after_ts is not None:
            where.append("CAST(m.timestamp AS REAL) >= ?")
            params.append(after_ts)
        if before_ts is not None:
            where.append("CAST(m.timestamp AS REAL) <= ?")
            params.append(before_ts)

        sort_mode = sort if sort in {"relevance", "newest", "oldest"} else "relevance"
        if sort_mode == "relevance":
            order_sql, order_params = _relevance_order(clean_query, terms)
            params.extend(order_params)
        elif sort_mode == "oldest":
            order_sql = "CAST(m.timestamp AS REAL) ASC"
        else:
            order_sql = "CAST(m.timestamp AS REAL) DESC"

        sql = self._select_sql(where) + f" ORDER BY {order_sql} LIMIT ?"
        params.append(_bounded_limit(limit))
        rows = self.conn.execute(sql, params).fetchall()
        hits = [self._row_to_hit(row) for row in rows]
        payload = self.evidence.serialize(hits)
        payload["query"] = clean_query
        payload["match_mode"] = mode
        payload["sort"] = sort_mode
        payload["searched_scope"] = (
            "tutti i canali visibili all'utente e tutti i messaggi archiviati"
            if self.allow_member_private_channels
            else "tutti i canali pubblici e il canale corrente"
        )
        return payload

    def read_thread(self, channel_id: str, thread_ts: str, *, limit: int = 80) -> dict:
        """Read an archived thread in chronological order after access checks."""
        where, params = self._base_where()
        where.extend(
            [
                "m.channel = ?",
                "COALESCE(m.thread_ts, m.timestamp) = ?",
            ]
        )
        params.extend([channel_id, thread_ts])
        sql = (
            self._select_sql(where) + " ORDER BY CAST(m.timestamp AS REAL) ASC LIMIT ?"
        )
        params.append(min(max(int(limit or 80), 1), 200))
        hits = [
            self._row_to_hit(row) for row in self.conn.execute(sql, params).fetchall()
        ]
        payload = self.evidence.serialize(hits)
        payload.update({"channel_id": channel_id, "thread_ts": thread_ts})
        return payload

    def read_surrounding(
        self,
        channel_id: str,
        message_ts: str,
        *,
        before: int = 4,
        after: int = 4,
    ) -> dict:
        """Read neighboring root/reply messages around one archived result."""
        before = min(max(int(before or 4), 0), 20)
        after = min(max(int(after or 4), 0), 20)
        base_where, base_params = self._base_where()
        base_where.append("m.channel = ?")
        base_params.append(channel_id)

        older_sql = self._select_sql(base_where + ["CAST(m.timestamp AS REAL) < ?"])
        older_sql += " ORDER BY CAST(m.timestamp AS REAL) DESC LIMIT ?"
        older = self.conn.execute(
            older_sql, [*base_params, _numeric_ts(message_ts), before]
        ).fetchall()

        current_sql = self._select_sql(base_where + ["m.timestamp = ?"])
        current = self.conn.execute(current_sql, [*base_params, message_ts]).fetchall()

        newer_sql = self._select_sql(base_where + ["CAST(m.timestamp AS REAL) > ?"])
        newer_sql += " ORDER BY CAST(m.timestamp AS REAL) ASC LIMIT ?"
        newer = self.conn.execute(
            newer_sql, [*base_params, _numeric_ts(message_ts), after]
        ).fetchall()

        rows = list(reversed(older)) + current + newer
        hits = [self._row_to_hit(row) for row in rows]
        payload = self.evidence.serialize(hits)
        payload.update({"channel_id": channel_id, "message_ts": message_ts})
        return payload

    def _base_where(self) -> tuple[list[str], list]:
        where = [
            "m.message IS NOT NULL",
            "TRIM(m.message) != ''",
            "m.user != 'USLACKBOT'",
            "m.message != ?",
            "NOT EXISTS (SELECT 1 FROM optout o WHERE o.user = m.user)",
            "NOT EXISTS (SELECT 1 FROM optout_ai oa WHERE oa.user = m.user)",
        ]
        params: list = [OPTED_OUT_TEXT]
        if self.allow_member_private_channels:
            where.append(
                "(c.is_private = 0 OR m.channel = ? OR EXISTS ("
                "SELECT 1 FROM members visibility "
                "WHERE visibility.channel = m.channel AND visibility.user = ?))"
            )
            params.extend([self.current_channel_id, self.requester_user_id])
        else:
            # Responses posted in a shared Slack surface must never disclose a
            # different private channel merely because the requester can see it.
            where.append("(c.is_private = 0 OR m.channel = ?)")
            params.append(self.current_channel_id)
        if self.before_timestamp:
            where.append("CAST(m.timestamp AS REAL) < ?")
            params.append(_numeric_ts(self.before_timestamp))
        return where, params

    @staticmethod
    def _select_sql(where: list[str]) -> str:
        # `where` contains only fixed fragments assembled by this module. Every
        # untrusted value is still passed separately as a SQLite parameter.
        return (
            "SELECT m.message, m.user, "
            "COALESCE(NULLIF(u.display_name, ''), NULLIF(u.name, ''), "
            "NULLIF(u.real_name, ''), 'Unknown') AS user_name, "
            "m.channel, c.name, m.timestamp, COALESCE(m.permalink, ''), "
            "COALESCE(m.thread_ts, m.timestamp) "
            "FROM messages m "
            "INNER JOIN channels c ON c.id = m.channel "
            "LEFT JOIN users u ON u.id = m.user "
            "WHERE " + " AND ".join(where)  # nosec B608
        )

    @staticmethod
    def _row_to_hit(row) -> ArchiveHit:
        return ArchiveHit(
            text=row[0] or "",
            user_id=row[1] or "",
            user_name=row[2] or "Unknown",
            channel_id=row[3] or "",
            channel_name=row[4] or "canale",
            timestamp=str(row[5] or ""),
            permalink=row[6] or "",
            thread_ts=str(row[7] or row[5] or ""),
        )


def _clean_query(query: str) -> str:
    return re.sub(r"\s+", " ", str(query or "")).strip()[:MAX_QUERY_CHARS]


def _search_terms(query: str, *, phrase: bool) -> list[str]:
    if phrase:
        return [query]
    terms = re.findall(r"[\wÀ-ÿ][\wÀ-ÿ+.#/@-]*", query, flags=re.UNICODE)
    unique = []
    for term in terms:
        normalized = term.casefold()
        if len(normalized) < 2 or normalized in unique:
            continue
        unique.append(normalized)
    return unique[:12]


def _relevance_order(query: str, terms: list[str]) -> tuple[str, list[str]]:
    """Build a bounded SQL relevance order that still evaluates all matches."""
    clauses = ["CASE WHEN LOWER(m.message) LIKE ? ESCAPE '\\' THEN 50 ELSE 0 END"]
    params = [f"%{_escape_like(query.lower())}%"]
    display_name = "LOWER(COALESCE(u.display_name, u.name, u.real_name, ''))"
    for term in terms:
        pattern = f"%{_escape_like(term.lower())}%"
        clauses.append(
            "CASE WHEN LOWER(m.message) LIKE ? ESCAPE '\\' THEN 10 ELSE 0 END"
        )
        params.append(pattern)
        clauses.append(
            f"CASE WHEN {display_name} LIKE ? ESCAPE '\\' "
            "OR LOWER(c.name) LIKE ? ESCAPE '\\' THEN 3 ELSE 0 END"
        )
        params.extend([pattern, pattern])
    expression = " + ".join(clauses)
    return f"({expression}) DESC, CAST(m.timestamp AS REAL) DESC", params


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _parse_date_boundary(value: str, *, end_of_day: bool) -> float | None:
    value = str(value or "").strip()
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=ROME)
        if end_of_day and len(value) == 10:
            parsed = parsed.replace(hour=23, minute=59, second=59, microsecond=999999)
        return parsed.timestamp()
    except ValueError:
        return None


def _numeric_ts(value: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _bounded_limit(limit: int) -> int:
    try:
        parsed = int(limit)
    except (TypeError, ValueError):
        parsed = 10
    return min(max(parsed, 1), MAX_RESULTS)


def _compact_text(text: str) -> str:
    compact = re.sub(r"\s+", " ", text or "").strip()
    if len(compact) <= MAX_MESSAGE_CHARS:
        return compact
    return compact[: MAX_MESSAGE_CHARS - 1].rstrip() + "…"
