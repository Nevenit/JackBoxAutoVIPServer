import json
from types import SimpleNamespace
from extractor.addon import RoomCodeExtractor
from extractor.server import RoomCodeServer


def _response_flow(host, url, method, body_obj):
    content = json.dumps(body_obj).encode() if body_obj is not None else b""
    return SimpleNamespace(
        request=SimpleNamespace(host=host, pretty_url=url, method=method),
        response=SimpleNamespace(content=content),
    )


def _ws_flow(host, url):
    return SimpleNamespace(request=SimpleNamespace(host=host, pretty_url=url, method="GET"))


def test_code_from_create_response_flow():
    ext = RoomCodeExtractor(RoomCodeServer(port=0))
    flow = _response_flow(
        "ecast.jackboxgames.com",
        "https://ecast.jackboxgames.com/api/v2/rooms",
        "POST",
        {"ok": True, "body": {"code": "ABCD"}},
    )
    assert ext.code_from_response_flow(flow) == "ABCD"


def test_response_flow_ignores_non_jackbox_host():
    ext = RoomCodeExtractor(RoomCodeServer(port=0))
    flow = _response_flow(
        "evil.com", "https://evil.com/api/v2/rooms", "POST",
        {"ok": True, "body": {"code": "ABCD"}},
    )
    assert ext.code_from_response_flow(flow) is None


def test_response_flow_ignores_non_post():
    ext = RoomCodeExtractor(RoomCodeServer(port=0))
    flow = _response_flow(
        "ecast.jackboxgames.com", "https://ecast.jackboxgames.com/api/v2/rooms", "GET",
        {"ok": True, "body": {"code": "ABCD"}},
    )
    assert ext.code_from_response_flow(flow) is None


def test_response_flow_tolerates_non_json_body():
    ext = RoomCodeExtractor(RoomCodeServer(port=0))
    flow = SimpleNamespace(
        request=SimpleNamespace(
            host="ecast.jackboxgames.com",
            pretty_url="https://ecast.jackboxgames.com/api/v2/rooms",
            method="POST",
        ),
        response=SimpleNamespace(content=b"not json"),
    )
    assert ext.code_from_response_flow(flow) is None


def test_code_from_ws_flow_fallback():
    ext = RoomCodeExtractor(RoomCodeServer(port=0))
    flow = _ws_flow(
        "ecast-prod-use2.jackboxgames.com",
        "wss://ecast-prod-use2.jackboxgames.com/api/v2/rooms/WXYZ/play",
    )
    assert ext.code_from_ws_flow(flow) == "WXYZ"


def test_ws_flow_ignores_non_jackbox_host():
    ext = RoomCodeExtractor(RoomCodeServer(port=0))
    flow = _ws_flow("evil.com", "wss://evil.com/api/v2/rooms/WXYZ/play")
    assert ext.code_from_ws_flow(flow) is None
