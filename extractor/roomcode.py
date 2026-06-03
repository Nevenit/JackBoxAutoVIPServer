"""Extract a Jackbox room code from intercepted ecast traffic.

Two pure entry points, no I/O:
- code_from_create_response: PRIMARY. The host's POST /api/v2/rooms response
  body carries {"body": {"code": ...}} (proven in the Phase 0 spike).
- extract_code_from_url: FALLBACK. Pull <CODE> from a /rooms/<CODE>/play URL.
"""
import re

# room-id segment, e.g. /rooms/ABCD/play  or  /api/v2/rooms/ABCD.
# Anchored on the '/rooms/' segment (leading slash or string start) so it is
# resilient to version/prefix routing changes, while the leading boundary still
# rejects 'classrooms'/'audiencerooms'.
_ROOM_PATH = re.compile(r"(?:^|/)rooms/([A-Za-z0-9]+)(?:[/?#]|$)")
# the create endpoint itself: /rooms  (no code segment)
_CREATE_PATH = re.compile(r"(?:^|/)rooms/?(?:[?#]|$)")


def looks_like_code(code: str) -> bool:
    """Sanity check: short and ASCII-alphanumeric. NOT hardcoded to 4/uppercase.

    str.isalnum() is Unicode-aware, so the extra isascii() guard rejects
    fullwidth Latin, accented letters, Roman numerals, and non-Latin digits
    that would otherwise be broadcast as a confidently-wrong room code.
    """
    return (
        isinstance(code, str)
        and 2 <= len(code) <= 8
        and code.isascii()
        and code.isalnum()
    )


def code_from_create_response(url: str, body) -> str | None:
    """Return the room code from a POST /rooms JSON response body.

    Precondition: the caller MUST have already confirmed this is the *create*
    POST. A GET/listing/poll on the same path can also carry body.code, so the
    HTTP method cannot be inferred from the body shape here. The mitmproxy addon
    guards method == "POST" before calling this function.
    """
    if not url or not _CREATE_PATH.search(url):
        return None
    if not isinstance(body, dict):
        return None
    inner = body.get("body")
    if not isinstance(inner, dict):
        return None
    code = inner.get("code")
    return code if isinstance(code, str) and looks_like_code(code) else None


def extract_code_from_url(url: str) -> str | None:
    """Return the room code embedded in a /rooms/<CODE>[/...] URL, else None."""
    if not url:
        return None
    match = _ROOM_PATH.search(url)
    if not match:
        return None
    code = match.group(1)
    return code if looks_like_code(code) else None
