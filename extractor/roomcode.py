"""Extract a Jackbox room code from intercepted ecast traffic.

Two pure entry points, no I/O:
- code_from_create_response: PRIMARY. The host's POST /api/v2/rooms response
  body carries {"body": {"code": ...}} (proven in the Phase 0 spike).
- extract_code_from_url: FALLBACK. Pull <CODE> from a /rooms/<CODE>/play URL.
"""
import re

# room-id segment, e.g. /api/v2/rooms/ABCD/play  or  /api/v2/rooms/ABCD
_ROOM_PATH = re.compile(r"/api/v\d+/rooms/([A-Za-z0-9]+)(?:[/?]|$)")
# the create endpoint itself: /api/v2/rooms  (no code segment)
_CREATE_PATH = re.compile(r"/api/v\d+/rooms/?(?:\?|$)")


def looks_like_code(code: str) -> bool:
    """Sanity check: short and alphanumeric. NOT hardcoded to 4/uppercase."""
    return isinstance(code, str) and 2 <= len(code) <= 8 and code.isalnum()


def code_from_create_response(url: str, body) -> str | None:
    """Return the room code from a POST /api/v2/rooms JSON response body."""
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
