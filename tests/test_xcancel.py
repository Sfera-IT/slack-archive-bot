import os
import sys

import pytest


ROOT_DIR = os.path.dirname(os.path.dirname(__file__))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from xcancel import build_xcancel_response_text, xcancel_alternatives_enabled


def test_xcancel_alternatives_are_enabled_by_default(monkeypatch):
    monkeypatch.delenv("XCANCEL_ALTERNATIVES_ENABLED", raising=False)

    assert xcancel_alternatives_enabled() is True
    assert build_xcancel_response_text("https://x.com/someone/status/123") == (
        "🔗 Link senza Shitler: https://xcancel.com/someone/status/123"
    )


@pytest.mark.parametrize("value", ["true", "1", "yes", "on", " TRUE "])
def test_xcancel_alternatives_can_be_explicitly_enabled(monkeypatch, value):
    monkeypatch.setenv("XCANCEL_ALTERNATIVES_ENABLED", value)

    assert xcancel_alternatives_enabled() is True
    assert build_xcancel_response_text("https://x.com/someone/status/123") == (
        "🔗 Link senza Shitler: https://xcancel.com/someone/status/123"
    )


@pytest.mark.parametrize("value", ["false", "0", "no", "off", " FALSE ", "", "invalid"])
def test_xcancel_alternatives_can_be_explicitly_disabled(monkeypatch, value):
    monkeypatch.setenv("XCANCEL_ALTERNATIVES_ENABLED", value)

    assert xcancel_alternatives_enabled() is False
    assert build_xcancel_response_text("https://x.com/someone/status/123") is None


def test_build_xcancel_response_for_single_x_link():
    text = "Guarda https://x.com/someone/status/123"

    assert build_xcancel_response_text(text, enabled=True) == (
        "🔗 Link senza Shitler: https://xcancel.com/someone/status/123"
    )


def test_build_xcancel_response_deduplicates_and_sorts_links():
    text = (
        "https://x.com/b/status/2 "
        "https://x.com/a/status/1 "
        "https://x.com/b/status/2"
    )

    assert build_xcancel_response_text(text, enabled=True) == (
        "🔗 Link senza Shitler:\n"
        "• https://xcancel.com/a/status/1\n"
        "• https://xcancel.com/b/status/2"
    )


def test_build_xcancel_response_ignores_existing_xcancel_link():
    text = "https://x.com/someone/status/123 https://xcancel.com/someone/status/123"

    assert build_xcancel_response_text(text, enabled=True) is None


def test_build_xcancel_response_ignores_non_x_links():
    assert build_xcancel_response_text("https://example.com/post", enabled=True) is None
