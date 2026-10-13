"""Mirrors src/lib/vendorUrl.js: normalizes a vendor link to the registry key, its host."""
from urllib.parse import urlparse


def host_for(link: str) -> str:
    """Matches Node's `new URL(link).host.toLowerCase()`.

    JS's `.host` includes the port when present (e.g. "127.0.0.1:4599"), so we
    use `netloc` (NOT `.hostname`, which drops the port and would break the
    registry key vs. what the extension/tests use). `new URL()` throws for a
    string with no scheme/authority (e.g. "not-a-url"); urlparse doesn't throw
    but leaves `netloc` empty in that case, so an empty netloc is treated the
    same as JS's catch branch: fall back to the lowercased raw string.
    """
    text = str(link)
    try:
        netloc = urlparse(text).netloc
    except ValueError:
        netloc = ""
    if netloc:
        return netloc.lower()
    return text.lower()
