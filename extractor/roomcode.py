"""Extract a Jackbox room code from intercepted traffic (ecast + legacy Blobcast).

Pure entry points, no I/O:
- code_from_create_response: ecast PRIMARY. The host's POST /api/v2/rooms
  response body carries {"body": {"code": ...}} (proven in the Phase 0 spike).
- extract_code_from_url: ecast FALLBACK. Pull <CODE> from a /rooms/<CODE>/play URL.
- code_from_blobcast_request: legacy Blobcast (older packs, e.g. Party Pack 5).
  The host's POST /accessToken request body carries {"roomId": "<CODE>"}.
- code_from_blobcast_socketio: legacy Blobcast websocket-only packs (e.g. Party
  Pack 1). The code arrives as a socket.io 0.9 event frame over the websocket.
"""
import json
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


def code_from_blobcast_request(url: str, body) -> str | None:
    """Return the room code from a legacy Blobcast POST /accessToken request body.

    Older packs use Blobcast: the host posts {"appId", "roomId", "userId"} to
    blobcast.jackboxgames.com/accessToken, where roomId is the room code (it is
    assigned over the socket.io channel first, then echoed here). Precondition:
    the caller confirms host + method == "POST" (the mitmproxy addon does).
    """
    if not url or "/accessToken" not in url:
        return None
    if not isinstance(body, dict):
        return None
    code = body.get("roomId")
    return code if isinstance(code, str) and looks_like_code(code) else None


def code_from_blobcast_socketio(message) -> str | None:
    """Return the room code from a legacy Blobcast socket.io websocket frame.

    Websocket-only packs (e.g. Party Pack 1) never expose the code over REST; it
    arrives as a socket.io 0.9 event frame ``5:::<json>`` whose args carry the
    CreateRoom result, e.g.
        5:::{"name":"msg","args":[{"action":"CreateRoom","roomId":"WEWW"}]}
    """
    if not isinstance(message, str) or not message.startswith("5:::"):
        return None
    try:
        payload = json.loads(message[4:])
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    args = payload.get("args")
    if not isinstance(args, list):
        return None
    for arg in args:
        if isinstance(arg, dict):
            code = arg.get("roomId")
            if isinstance(code, str) and looks_like_code(code):
                return code
    return None


def joined_room_from_ecast(url: str, message: str) -> str | None:
    """Return the room code when `message` is an ecast frame for a player joining.

    The frame is JSON with opcode "client/connected"; an audience member does not
    count. A missing result or role still does (the frame shape is unconfirmed).
    """
    code = extract_code_from_url(url)
    if code is None or not isinstance(message, str):
        return None
    try:
        frame = json.loads(message)
    except (ValueError, TypeError):
        return None
    if not isinstance(frame, dict) or frame.get("opcode") != "client/connected":
        return None
    result = frame.get("result")
    if isinstance(result, dict) and result.get("role") == "audience":
        return None
    return code


def joined_room_from_blobcast_socketio(message) -> str | None:
    """Return the roomId of a Blobcast ``CustomerJoinedRoom`` socket.io frame."""
    if not isinstance(message, str) or not message.startswith("5:::"):
        return None
    try:
        payload = json.loads(message[4:])
    except (ValueError, TypeError):
        return None
    args = payload.get("args") if isinstance(payload, dict) else None
    if not isinstance(args, list):
        return None
    for arg in args:
        if isinstance(arg, dict) and arg.get("event") == "CustomerJoinedRoom":
            code = arg.get("roomId")
            if isinstance(code, str) and looks_like_code(code):
                return code
    return None
