"""mitmproxy addon: watch ecast traffic, extract the room code, broadcast it.

Run via:  mitmdump --listen-port <port> -s extractor/addon.py
(PYTHONPATH must include the project root so `extractor` is importable.)
"""
import asyncio
import json
import logging
import os

from mitmproxy import ctx

from extractor.roomcode import (
    code_from_create_response,
    extract_code_from_url,
    code_from_blobcast_request,
    code_from_blobcast_socketio,
)
from extractor.relay_publisher import RelayPublisher
from extractor.server import RoomCodeServer

_JACKBOX = "jackboxgames.com"

logger = logging.getLogger(__name__)


class RoomCodeExtractor:
    def __init__(self, *sinks):
        if not sinks:
            raise ValueError("RoomCodeExtractor requires at least one sink")
        self.sinks = list(sinks)
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
        # pretty_host prefers the Host header/SNI, so it yields the hostname in
        # transparent mode where request.host is the original-destination IP.
        req = flow.request
        host = getattr(req, "pretty_host", "") or getattr(req, "host", "") or ""
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

    def code_from_blobcast_flow(self, flow) -> str | None:
        # Legacy Blobcast: the host's POST /accessToken request body carries roomId.
        if not self._is_jackbox(flow):
            return None
        if getattr(flow.request, "method", "") != "POST":
            return None
        try:
            body = json.loads(flow.request.content)
        except (ValueError, TypeError, AttributeError):
            return None
        return code_from_blobcast_request(flow.request.pretty_url, body)

    def code_from_websocket_message(self, flow) -> str | None:
        # Websocket-only Blobcast: the code arrives as a socket.io frame.
        if not self._is_jackbox(flow):
            return None
        ws = getattr(flow, "websocket", None)
        if not ws or not ws.messages:
            return None
        content = getattr(ws.messages[-1], "content", None)
        if content is None:
            return None
        try:
            text = content.decode("utf-8", "replace")
        except (AttributeError, UnicodeDecodeError):
            return None
        return code_from_blobcast_socketio(text)

    def _broadcast(self, code: str | None) -> None:
        if not code:
            return
        for sink in self.sinks:
            self._spawn(self._set_code_safely(sink, code))

    async def _set_code_safely(self, sink, code: str) -> None:
        try:
            await sink.set_code(code)
        except Exception:
            # Deliberately does not log the code itself.
            logger.exception("sink %s rejected the room code", type(sink).__name__)

    # --- mitmproxy event hooks ---
    async def running(self):
        for sink in self.sinks:
            try:
                await sink.start()
            except Exception as exc:
                if getattr(sink, "critical", False):
                    logger.error(
                        "critical sink %s failed to start (%s); shutting down.",
                        type(sink).__name__,
                        exc,
                    )
                    ctx.master.shutdown()
                    raise
                logger.warning(
                    "sink %s failed to start (%s); continuing without it.",
                    type(sink).__name__,
                    exc,
                )

    def request(self, flow):
        # Blobcast carries the code in the request body (POST /accessToken).
        self._broadcast(self.code_from_blobcast_flow(flow))

    def response(self, flow):
        self._broadcast(self.code_from_response_flow(flow))

    def websocket_start(self, flow):
        self._broadcast(self.code_from_ws_flow(flow))

    def websocket_message(self, flow):
        # Fires per ws frame; the code arrives in the socket.io CreateRoom result.
        self._broadcast(self.code_from_websocket_message(flow))


def _build_sinks():
    """The LAN server always; the relay publisher when it is configured."""
    sinks = [RoomCodeServer(port=38469)]
    base_url = os.environ.get("JBVIP_RELAY_URL", "")
    token = os.environ.get("JBVIP_RELAY_TOKEN", "")
    if base_url and token:
        try:
            sinks.append(RelayPublisher(base_url, token))
        except ValueError as exc:
            logger.error("relay publishing disabled: %s", exc)
    return sinks


addons = [RoomCodeExtractor(*_build_sinks())]
