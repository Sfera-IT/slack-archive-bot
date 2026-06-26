import os
import sys


ROOT_DIR = os.path.dirname(os.path.dirname(__file__))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from xcancel import build_xcancel_response_text


def test_build_xcancel_response_for_single_x_link():
    text = "Guarda https://x.com/someone/status/123"

    assert build_xcancel_response_text(text) == (
        "🔗 Link senza Shitler: https://xcancel.com/someone/status/123"
    )


def test_build_xcancel_response_deduplicates_and_sorts_links():
    text = (
        "https://x.com/b/status/2 "
        "https://x.com/a/status/1 "
        "https://x.com/b/status/2"
    )

    assert build_xcancel_response_text(text) == (
        "🔗 Link senza Shitler:\n"
        "• https://xcancel.com/a/status/1\n"
        "• https://xcancel.com/b/status/2"
    )


def test_build_xcancel_response_ignores_existing_xcancel_link():
    text = "https://x.com/someone/status/123 https://xcancel.com/someone/status/123"

    assert build_xcancel_response_text(text) is None


def test_build_xcancel_response_ignores_non_x_links():
    assert build_xcancel_response_text("https://example.com/post") is None
