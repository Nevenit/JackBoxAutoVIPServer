import json
from pathlib import Path

import pytest

from extractor.roomcode import (
    extract_code_from_url,
    code_from_create_response,
    code_from_blobcast_request,
    code_from_blobcast_socketio,
    looks_like_code,
    joined_room_from_blobcast_socketio,
    joined_room_from_ecast,
)

FIXTURE = Path(__file__).parent / "fixtures" / "create_room_response.json"
BLOBCAST_FIXTURE = Path(__file__).parent / "fixtures" / "blobcast_accesstoken_request.json"
SOCKETIO_FIXTURE = Path(__file__).parent / "fixtures" / "blobcast_socketio_createroom.txt"


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


# --- legacy Blobcast: POST /accessToken request body carries roomId ---

def test_code_from_real_blobcast_accesstoken_fixture():
    body = json.loads(BLOBCAST_FIXTURE.read_text())
    url = "https://blobcast.jackboxgames.com/accessToken"
    assert code_from_blobcast_request(url, body) == "PFTC"


def test_blobcast_ignores_non_accesstoken_url():
    # the /room call has no roomId; only /accessToken carries it
    assert code_from_blobcast_request(
        "https://blobcast.jackboxgames.com/room", {"roomId": "PFTC"}
    ) is None


def test_blobcast_handles_missing_or_bad_roomid():
    url = "https://blobcast.jackboxgames.com/accessToken"
    assert code_from_blobcast_request(url, {"appId": "x"}) is None
    assert code_from_blobcast_request(url, None) is None
    assert code_from_blobcast_request(url, {"roomId": 1234}) is None        # non-string
    assert code_from_blobcast_request(url, {"roomId": "TOOLONG12"}) is None  # fails sanity check


# --- legacy Blobcast websocket: socket.io CreateRoom result frame carries roomId ---

def test_code_from_real_blobcast_socketio_fixture():
    msg = SOCKETIO_FIXTURE.read_text().strip()
    assert code_from_blobcast_socketio(msg) == "WEWW"


def test_blobcast_socketio_ignores_non_event_frames():
    assert code_from_blobcast_socketio("1::") is None    # socket.io connect
    assert code_from_blobcast_socketio("2::") is None    # heartbeat
    assert code_from_blobcast_socketio("") is None
    assert code_from_blobcast_socketio(None) is None


def test_blobcast_socketio_ignores_event_without_roomid():
    # the client's CreateRoom *request* has no roomId yet
    assert code_from_blobcast_socketio('5:::{"args":{"action":"CreateRoom","appId":"x"}}') is None


def test_blobcast_socketio_tolerates_bad_json():
    assert code_from_blobcast_socketio("5:::not json") is None


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


# The length boundaries are pinned at exactly 1/2/8/9 here and identically in
# tests/test_relay_state.py. relay.state._looks_like_code duplicates this
# function on purpose — the relay deploys to a VPS without the extractor
# package — so these two sets of cases are what stops them drifting apart.
@pytest.mark.parametrize("code", ["AB", "ABCDEFGH"])
def test_looks_like_code_accepts_the_length_boundaries(code):
    assert looks_like_code(code) is True


@pytest.mark.parametrize("code", ["A", "ABCDEFGHI"])
def test_looks_like_code_rejects_just_outside_the_length_boundaries(code):
    assert looks_like_code(code) is False


ECAST_URL = "wss://ecast.jackboxgames.com/api/v2/rooms/ABCD/play?role=host&name=x"
ECAST_JOIN = (
    '{"pc":42,"opcode":"client/connected","result":{"id":2,"userId":"u1",'
    '"name":"ALICE","role":"player","reconnect":false}}'
)


@pytest.mark.parametrize("message", [
    ECAST_JOIN,
    '{"opcode":"client/connected","result":{"id":2}}',
    '{"opcode":"client/connected"}',
])
def test_ecast_join_gives_the_room(message):
    assert joined_room_from_ecast(ECAST_URL, message) == "ABCD"


@pytest.mark.parametrize("url,message", [
    (ECAST_URL, ECAST_JOIN.replace('"player"', '"audience"')),
    (ECAST_URL, '{"opcode":"client/welcome","result":{"role":"player"}}'),
    (ECAST_URL, "not json"),
    (ECAST_URL, "[1, 2]"),
    ("wss://ecast.jackboxgames.com/api/v2/other", ECAST_JOIN),
])
def test_ecast_non_joins_give_none(url, message):
    assert joined_room_from_ecast(url, message) is None


BLOBCAST_JOIN = (
    '5:::{"name":"msg","args":[{"type":"Event","event":"CustomerJoinedRoom",'
    '"roomId":"ABCD","customerUserId":"u1","customerName":"ALICE",'
    '"options":{"roomcode":"","name":"ALICE"}}]}'
)


def test_blobcast_join_gives_the_room():
    assert joined_room_from_blobcast_socketio(BLOBCAST_JOIN) == "ABCD"


@pytest.mark.parametrize("message", [
    '5:::{"name":"msg","args":[{"action":"CreateRoom","roomId":"ABCD"}]}',
    BLOBCAST_JOIN.replace('"ABCD"', '"!!"'),
    BLOBCAST_JOIN[1:],
])
def test_blobcast_non_joins_give_none(message):
    assert joined_room_from_blobcast_socketio(message) is None
