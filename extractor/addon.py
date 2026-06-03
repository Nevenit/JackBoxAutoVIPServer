"""mitmproxy addon: watch ecast traffic, extract the room code, broadcast it.

Run via:  mitmdump --listen-port <port> -s extractor/addon.py
(PYTHONPATH must include the project root so `extractor` is importable.)
"""
import asyncio
import json

from extractor.roomcode import code_from_create_response, extract_code_from_url
from extractor.server import RoomCodeServer

_JACKBOX = "jackboxgames.com"


class RoomCodeExtractor:
    def __init__(self, server: RoomCodeServer):
        self.server = server

    # --- pure-ish extraction (host-filtered), unit tested ---
    def _is_jackbox(self, flow) -> bool:
        host = getattr(flow.request, "host", "") or ""
        return _JACKBOX in host

    def code_from_response_flow(self, flow) -> str | None:
        if not self._is_jackbox(flow):
            return None
        if getattr(flow.request, "method", "") != "POST":
            return None
        try:
            body = json.loads(flow.response.content)
        except (ValueError, TypeError, AttributeError):
            return None
        return code_from_create_response(flow.request.pretty_url, body)

    def code_from_ws_flow(self, flow) -> str | None:
        if not self._is_jackbox(flow):
            return None
        return extract_code_from_url(flow.request.pretty_url)

    def _broadcast(self, code: str | None) -> None:
        if code:
            asyncio.ensure_future(self.server.set_code(code))

    # --- mitmproxy event hooks ---
    def running(self):
        asyncio.ensure_future(self.server.start())

    def response(self, flow):
        self._broadcast(self.code_from_response_flow(flow))

    def websocket_start(self, flow):
        self._broadcast(self.code_from_ws_flow(flow))


addons = [RoomCodeExtractor(RoomCodeServer(port=38469))]
