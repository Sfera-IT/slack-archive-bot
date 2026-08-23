import pytest

from runtime_config import resolve_database_path


def test_database_path_resolver_supports_container_default_and_single_overrides():
    assert resolve_database_path({"DEFAULT_DATABASE_PATH": "/data/slack.sqlite"}) == (
        "/data/slack.sqlite"
    )
    assert resolve_database_path(
        {
            "DEFAULT_DATABASE_PATH": "/data/slack.sqlite",
            "ARCHIVE_BOT_DATABASE_PATH": "/data/custom.sqlite",
        }
    ) == "/data/custom.sqlite"
    assert resolve_database_path(
        {"DEFAULT_DATABASE_PATH": "/data/slack.sqlite", "DB_PATH": "/data/web.sqlite"}
    ) == "/data/web.sqlite"
    assert resolve_database_path(
        {"ARCHIVE_BOT_DATABASE_PATH": "/same.sqlite", "DB_PATH": "/same.sqlite"}
    ) == "/same.sqlite"


def test_database_path_resolver_rejects_split_brain_configuration():
    with pytest.raises(RuntimeError, match="same file"):
        resolve_database_path(
            {"ARCHIVE_BOT_DATABASE_PATH": "/bot.sqlite", "DB_PATH": "/web.sqlite"}
        )
