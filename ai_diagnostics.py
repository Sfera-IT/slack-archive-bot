"""Safe, user-visible diagnostics for failures in the Slack AI handler."""

from __future__ import annotations

import re
import traceback
import uuid
from collections.abc import Mapping
from typing import Any

MAX_ERROR_MESSAGE_CHARS = 1200
MAX_STACK_FRAMES = 8
MAX_REPORT_CHARS = 3600

# Keep the delivery boundary fail-closed even if a stale/non-admin subscriber row
# is present in the database. This mirrors the bot's administrator allowlist.
DEFAULT_AI_DEBUG_ADMIN_USERS = frozenset({
    'U011PQ7RHRT',
    'U011MV24J2W',
    'U0129HFHRJ4',
    'U011N8WRRD0',
    'U011Z26G449',
    'U011CKQ7D71',
    'U011KE4BF0W',
    'U011PN35BHT',
})

_TOKEN_PATTERN = re.compile(r"\b(?:sk|xox[baprs])-[A-Za-z0-9._-]{8,}\b", re.IGNORECASE)
_BEARER_PATTERN = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]+", re.IGNORECASE)
_NAMED_SECRET_PATTERN = re.compile(
    r"\b(authorization|api[_-]?key|token|secret)\s*[:=]\s*"
    r"(?:['\"])?[^\s,;'\"}]+",
    re.IGNORECASE,
)


def new_ai_error_id() -> str:
    """Return a short correlation ID suitable for Slack and logs."""
    return f"AI-{uuid.uuid4().hex[:10].upper()}"


def build_private_ai_error_report(
    exception: Exception,
    *,
    event: Mapping[str, Any],
    model: str,
    reasoning_effort: str,
    error_id: str,
    source: str = "ai",
) -> str:
    """Build a bounded diagnostic report without secrets, locals, or prompt data."""
    details = _exception_details(exception)
    frames = traceback.extract_tb(exception.__traceback__)[-MAX_STACK_FRAMES:]
    stack = (
        "\n".join(
            f"{_basename(frame.filename)}:{frame.lineno} in {frame.name}"
            for frame in frames
        )
        or "non disponibile"
    )

    lines = [
        f":warning: *Debug errore AI* `{_slack_escape(error_id)}`",
        f"• tipo: `{_slack_escape(type(exception).__name__)}`",
        f"• messaggio: `{_slack_escape(details['message'])}`",
        f"• modello: `{_slack_escape(model)}`",
        f"• reasoning: `{_slack_escape(reasoning_effort)}`",
        f"• flusso: `{_slack_escape(source)}`",
        f"• utente: `{_slack_escape(str(event.get('user') or 'n/d'))}`",
        f"• canale: `{_slack_escape(str(event.get('channel') or 'n/d'))}`",
        f"• messaggio: `{_slack_escape(str(event.get('ts') or 'n/d'))}`",
    ]
    for label in ("status", "code", "param", "request_id"):
        value = details.get(label)
        if value:
            lines.append(f"• {label}: `{_slack_escape(value)}`")
    lines.extend(("• stack (senza variabili locali):", f"```{_slack_escape(stack)}```"))

    report = "\n".join(lines)
    if len(report) > MAX_REPORT_CHARS:
        report = report[: MAX_REPORT_CHARS - 16].rstrip() + "\n…[troncato]"
    return report


def send_private_ai_error(
    client,
    exception: Exception,
    *,
    event: Mapping[str, Any],
    model: str,
    reasoning_effort: str,
    error_id: str,
    recipient_user_id: str | None = None,
    source: str = "ai",
) -> None:
    """Send the sanitized report to one explicitly selected Slack DM."""
    user_id = str(recipient_user_id or event.get("user") or "")
    if not user_id:
        raise ValueError("missing requesting Slack user ID")
    client.chat_postMessage(
        channel=user_id,
        text=build_private_ai_error_report(
            exception,
            event=event,
            model=model,
            reasoning_effort=reasoning_effort,
            error_id=error_id,
            source=source,
        ),
    )


def set_ai_debug_enabled(cursor, user_id: str, enabled: bool) -> None:
    """Persist an administrator's opt-in state; missing rows mean disabled."""
    cursor.execute(
        """
        INSERT INTO ai_debug_subscribers(user_id, enabled, updated_at)
        VALUES (?, ?, strftime('%s', 'now'))
        ON CONFLICT(user_id) DO UPDATE SET
            enabled = excluded.enabled,
            updated_at = excluded.updated_at
        """,
        (str(user_id), 1 if enabled else 0),
    )


def is_ai_debug_enabled(cursor, user_id: str) -> bool:
    cursor.execute(
        "SELECT enabled FROM ai_debug_subscribers WHERE user_id = ?",
        (str(user_id),),
    )
    row = cursor.fetchone()
    return bool(row and row[0])


def get_ai_debug_recipients(cursor, admin_users=None) -> list[str]:
    allowed = DEFAULT_AI_DEBUG_ADMIN_USERS
    if admin_users is not None:
        allowed = allowed.intersection(str(user_id) for user_id in admin_users)
    if not allowed:
        return []
    padded_allowed = tuple(sorted(allowed)) + ('',) * (
        len(DEFAULT_AI_DEBUG_ADMIN_USERS) - len(allowed)
    )
    cursor.execute(
        """SELECT user_id FROM ai_debug_subscribers
            WHERE enabled = 1 AND user_id IN (?, ?, ?, ?, ?, ?, ?, ?)
            ORDER BY user_id""",
        padded_allowed,
    )
    return [str(row[0]) for row in cursor.fetchall()]


def _exception_details(exception: Exception) -> dict[str, str]:
    body = getattr(exception, "body", None)
    error_body = body.get("error", body) if isinstance(body, dict) else {}
    if not isinstance(error_body, dict):
        error_body = {}

    message = error_body.get("message") or str(exception) or "nessun dettaglio"
    details = {
        "message": _sanitize(message, MAX_ERROR_MESSAGE_CHARS),
        "status": _safe_value(getattr(exception, "status_code", None)),
        "code": _safe_value(getattr(exception, "code", None) or error_body.get("code")),
        "param": _safe_value(
            getattr(exception, "param", None) or error_body.get("param")
        ),
        "request_id": _safe_value(getattr(exception, "request_id", None)),
    }
    return details


def _safe_value(value: Any) -> str:
    if value is None:
        return ""
    return _sanitize(str(value), 200)


def _sanitize(value: Any, limit: int) -> str:
    text = str(value).replace("\x00", " ").replace("```", "'''")
    text = _TOKEN_PATTERN.sub("[REDACTED]", text)
    text = _BEARER_PATTERN.sub("Bearer [REDACTED]", text)
    text = _NAMED_SECRET_PATTERN.sub(lambda match: f"{match.group(1)}=[REDACTED]", text)
    text = " ".join(text.split())
    if len(text) > limit:
        return text[: limit - 12].rstrip() + " …[troncato]"
    return text


def _slack_escape(value: Any) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("`", "'")
    )


def _basename(path: str) -> str:
    return path.replace("\\", "/").rsplit("/", 1)[-1]
