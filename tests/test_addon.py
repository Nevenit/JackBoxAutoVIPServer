import asyncio
import json
from types import SimpleNamespace

from mitmproxy.test import tflow, tutils

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


# --- real mitmproxy flow contract tests (pin the integration boundary) ---
def test_real_rest_flow_contract():
    ext = RoomCodeExtractor(RoomCodeServer(port=0))
    req = tutils.treq(method=b"POST", host="ecast.jackboxgames.com",
                      port=443, scheme=b"https", path=b"/api/v2/rooms")
    resp = tutils.tresp(content=json.dumps({"ok": True, "body": {"code": "ABCD"}}).encode())
    f = tflow.tflow(req=req, resp=resp)
    assert ext.code_from_response_flow(f) == "ABCD"


def test_real_ws_flow_contract():
    ext = RoomCodeExtractor(RoomCodeServer(port=0))
    # NOTE: twebsocketflow takes NO req= kwarg in mitmproxy 12.2.3
    f = tflow.twebsocketflow()
    f.request = tutils.treq(method=b"GET", host="ecast-prod-use2.jackboxgames.com",
                            port=443, scheme=b"https",
                            path=b"/api/v2/rooms/WXYZ/play")
    assert ext.code_from_ws_flow(f) == "WXYZ"


# --- end-to-end hook -> _broadcast -> ensure_future -> set_code -> TCP client ---
async def test_response_hook_drives_set_code_end_to_end():
    server = RoomCodeServer(port=0)
    await server.start()
    ext = RoomCodeExtractor(server)
    reader, writer = await asyncio.open_connection("127.0.0.1", server.bound_port)
    await asyncio.sleep(0.05)
    flow = _response_flow(
        "ecast.jackboxgames.com",
        "https://ecast.jackboxgames.com/api/v2/rooms",
        "POST",
        {"ok": True, "body": {"code": "NBHT"}},
    )
    ext.response(flow)            # real event hook, not the pure helper
    await asyncio.sleep(0.05)     # let the ensure_future task run
    data = await asyncio.wait_for(reader.read(100), timeout=1)
    assert data == b"RoomCode:NBHT"
    writer.close()


async def test_websocket_start_hook_drives_set_code_end_to_end():
    server = RoomCodeServer(port=0)
    await server.start()
    ext = RoomCodeExtractor(server)
    reader, writer = await asyncio.open_connection("127.0.0.1", server.bound_port)
    await asyncio.sleep(0.05)
    flow = _ws_flow(
        "ecast-prod-use2.jackboxgames.com",
        "wss://ecast-prod-use2.jackboxgames.com/api/v2/rooms/WXYZ/play?role=host",
    )
    ext.websocket_start(flow)
    await asyncio.sleep(0.05)
    data = await asyncio.wait_for(reader.read(100), timeout=1)
    assert data == b"RoomCode:WXYZ"
    writer.close()


async def test_running_starts_server():
    server = RoomCodeServer(port=0)
    ext = RoomCodeExtractor(server)
    await ext.running()
    assert server.bound_port > 0
    reader, writer = await asyncio.open_connection("127.0.0.1", server.bound_port)
    writer.close()
