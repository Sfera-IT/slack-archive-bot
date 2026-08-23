"""Small, dependency-free runtime configuration resolvers."""

from __future__ import annotations

import os
from collections.abc import Mapping


def resolve_database_path(
    environ: Mapping[str, str] | None = None,
    *,
    default: str | None = None,
) -> str:
    values = os.environ if environ is None else environ
    canonical = values.get("ARCHIVE_BOT_DATABASE_PATH")
    compatibility = values.get("DB_PATH")
    if canonical and compatibility:
        if os.path.abspath(canonical) != os.path.abspath(compatibility):
            raise RuntimeError(
                "DB_PATH and ARCHIVE_BOT_DATABASE_PATH must reference the same file"
            )
    fallback = values.get("DEFAULT_DATABASE_PATH") or default or "slack.sqlite"
    return canonical or compatibility or fallback
