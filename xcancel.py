import os
import re


_X_COM_PATTERN = re.compile(r"^https?://(?:www\.)?x\.com/(.+)$", re.IGNORECASE)
_ENABLED_VALUES = {"1", "true", "yes", "on"}


def xcancel_alternatives_enabled():
    """Return whether XCancel link suggestions are explicitly enabled."""
    return os.getenv("XCANCEL_ALTERNATIVES_ENABLED", "false").strip().lower() in (
        _ENABLED_VALUES
    )


def extract_urls(text):
    """Estrae tutti gli URL HTTP/HTTPS da un testo."""
    url_pattern = r'https?://[^\s<>":{}|\\^`\[\]]+'
    urls = re.findall(url_pattern, text, flags=re.IGNORECASE)
    return [url.rstrip(".,;:!?") for url in urls]


def build_xcancel_response_text(text, *, enabled=None):
    """Costruisce la risposta xcancel attesa per un testo Slack."""
    if enabled is None:
        enabled = xcancel_alternatives_enabled()
    if not enabled:
        return None

    if not text:
        return None

    urls = extract_urls(text)
    if not urls:
        return None

    xcancel_links = set()
    for url in urls:
        match = _X_COM_PATTERN.match(url)
        if match:
            path = match.group(1)
            xcancel_url = f"https://xcancel.com/{path}"
            if xcancel_url.lower() not in text.lower():
                xcancel_links.add(xcancel_url)

    if not xcancel_links:
        return None

    xcancel_list = sorted(xcancel_links)
    if len(xcancel_list) == 1:
        return f"🔗 Link senza Shitler: {xcancel_list[0]}"

    links_formatted = "\n".join(f"• {link}" for link in xcancel_list)
    return f"🔗 Link senza Shitler:\n{links_formatted}"
