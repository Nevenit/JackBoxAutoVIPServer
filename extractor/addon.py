"""mitmproxy addon: watch ecast traffic, extract the room code, broadcast it.

Run via:  mitmdump --listen-port <port> -s extractor/addon.py
(PYTHONPATH must include the project root so `extractor` is importable.)
"""
import asyncio
import json
import logging

from mitmproxy import ctx

from extractor.roomcode import code_from_create_response, extract_code_from_url
from extractor.server import RoomCodeServer

_JACKBOX = "jackboxgames.com"

logger = logging.getLogger(__name__)


class RoomCodeExtractor:
    def __init__(self, server: RoomCodeServer):
        self.server = server
        self._tasks: set[asyncio.Task] = set()

    def _spawn(self, coro) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)

        def _done(t: asyncio.Task) -> None:
            self._tasks.discard(t)
            if not t.cancelled() and t.exception() is not None:
                logger.error("background task failed", exc_info=t.exception())

        task.add_done_callback(_done)

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
            self._spawn(self.server.set_code(code))

    # --- mitmproxy event hooks ---
    async def running(self):
        try:
            await self.server.start()
        except OSError as e:
            logger.error(
                "RoomCodeServer could not bind %s:%d (%s); shutting down.",
                self.server._host, self.server._port, e,
            )
            ctx.master.shutdown()
            raise

    def response(self, flow):
        self._broadcast(self.code_from_response_flow(flow))

    def websocket_start(self, flow):
        self._broadcast(self.code_from_ws_flow(flow))


addons = [RoomCodeExtractor(RoomCodeServer(port=38469))]
