import json
from pathlib import Path

from extractor.roomcode import (
    extract_code_from_url,
    code_from_create_response,
    looks_like_code,
)

FIXTURE = Path(__file__).parent / "fixtures" / "create_room_response.json"


# --- primary: REST create-room response body ---

def test_code_from_real_create_response_fixture():
    body = json.loads(FIXTURE.read_text())
    url = "https://ecast.jackboxgames.com/api/v2/rooms"
    assert code_from_create_response(url, body) == "NBHT"


def test_code_from_create_response_ignores_non_create_url():
    body = {"ok": True, "body": {"code": "ABCD"}}
    # a play/get URL is not the create endpoint
    assert code_from_create_response(
        "https://ecast.jackboxgames.com/api/v2/rooms/ABCD/play", body
    ) is None


def test_code_from_create_response_handles_missing_fields():
    url = "https://ecast.jackboxgames.com/api/v2/rooms"
    assert code_from_create_response(url, {"ok": True, "body": {}}) is None
    assert code_from_create_response(url, {"ok": True}) is None
    assert code_from_create_response(url, None) is None
    assert code_from_create_response(url, {"ok": False, "body": {"code": "ABCD"}}) == "ABCD"  # ok flag not required


def test_create_response_rejects_non_string_code():
    url = "https://ecast.jackboxgames.com/api/v2/rooms"
    # malformed body: code is an int, not a string -> graceful None, not a crash
    assert code_from_create_response(url, {"ok": True, "body": {"code": 1234}}) is None
    assert not looks_like_code(1234)
    assert not looks_like_code(None)


def test_create_response_rejects_non_ascii_code():
    url = "https://ecast.jackboxgames.com/api/v2/rooms"
    # accented/non-ASCII alnum must not pass as a real ASCII wire code
    assert code_from_create_response(url, {"body": {"code": "héll"}}) is None


def test_create_response_accepts_unversioned_and_alternate_paths():
    body = {"body": {"code": "WXYZ"}}
    # version envelope is not required; anchor is the '/rooms' segment
    assert code_from_create_response("https://ecast.jackboxgames.com/rooms", body) == "WXYZ"
    assert code_from_create_response("https://ecast.jackboxgames.com/v3beta/rooms", body) == "WXYZ"
    assert code_from_create_response("https://ecast.jackboxgames.com/api/rooms", body) == "WXYZ"


# --- fallback: WebSocket / room URL ---

def test_extracts_code_from_host_websocket_url():
    url = "wss://ecast-prod-use2.jackboxgames.com/api/v2/rooms/ABCD/play?role=host"
    assert extract_code_from_url(url) == "ABCD"


def test_extracts_code_from_plain_room_url():
    assert extract_code_from_url("https://ecast.jackboxgames.com/api/v2/rooms/WXYZ") == "WXYZ"


def test_url_returns_none_for_create_url_without_code():
    assert extract_code_from_url("https://ecast.jackboxgames.com/api/v2/rooms") is None


def test_url_returns_none_when_no_room_path():
    assert extract_code_from_url("https://ecast.jackboxgames.com/api/v2/health") is None
    assert extract_code_from_url("") is None


def test_extracts_code_from_unversioned_room_url():
    # version/prefix routing changes must not break extraction
    assert extract_code_from_url("https://ecast.jackboxgames.com/rooms/ABCD") == "ABCD"
    assert extract_code_from_url("https://ecast.jackboxgames.com/rooms/ABCD/play") == "ABCD"
    assert extract_code_from_url("https://ecast.jackboxgames.com/v3beta/rooms/WXYZ/play") == "WXYZ"


def test_url_rejects_classrooms_and_audiencerooms():
    # the leading-boundary anchor must not match these as '/rooms/...'
    assert extract_code_from_url("https://ecast.jackboxgames.com/classrooms/ABCD") is None
    assert extract_code_from_url("https://ecast.jackboxgames.com/audiencerooms/WXYZ") is None


# --- sanity check ---

def test_looks_like_code():
    assert looks_like_code("NBHT")
    assert looks_like_code("abcd1")
    assert not looks_like_code("")
    assert not looks_like_code("a")            # too short
    assert not looks_like_code("TOOLONGCODE")  # too long
    assert not looks_like_code("AB-CD")        # non-alnum
    assert not looks_like_code("héll")         # non-ASCII alnum
    assert not looks_like_code("ⅫⅫ")           # Roman numerals
    assert not looks_like_code(1234)           # non-string
