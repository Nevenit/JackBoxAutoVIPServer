"""ASGI relay: takes room codes from the host PC, pushes them to subscribers.

Thin by design — every decision lives in relay.state. Room codes are never
logged here or anywhere else in the relay.
"""
import asyncio
import json
import logging
import os
import time

from starlette.applications import Starlette
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from relay.state import RateLimiter, RelayState
from relay.tokens import load_tokens, save_tokens

logger = logging.getLogger(__name__)

KEEPALIVE_SECONDS = 20.0
DEFAULT_TOKENS_PATH = "/var/lib/jbvip/tokens.json"
# A subscriber that has stopped draining is dropped once this many payloads
# have backed up. Also the backstop for a stream cancelled before its generator
# body runs, whose `finally` never executes and so never unregisters the queue.
SUBSCRIBER_QUEUE_SIZE = 64


def _bearer(request) -> str:
    header = request.headers.get("authorization", "")
    prefix = "Bearer "
    return header[len(prefix):] if header.startswith(prefix) else ""


class Relay:
    def __init__(
        self,
        state: RelayState,
        tokens_path,
        *,
        publish_limiter=None,
        pair_limiter=None,
        mint_limiter=None,
    ):
        self.state = state
        self.tokens_path = tokens_path
        self._subscribers: set[asyncio.Queue] = set()
        self._publish_limiter = publish_limiter or RateLimiter(
            limit=60, window_seconds=60.0
        )
        self._pair_limiter = pair_limiter or RateLimiter(
            limit=10, window_seconds=60.0
        )
        self._mint_limiter = mint_limiter or RateLimiter(
            limit=10, window_seconds=60.0
        )

    def _fan_out(self, payload: dict) -> None:
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                # It is not draining and never will; unbounded growth is the
                # only thing worse than losing it. Nothing is logged: a
                # subscriber's existence is not worth a log line, and the
                # payload holds a room code.
                self._subscribers.discard(queue)

    async def publish(self, request):
        if not self.state.is_publisher(_bearer(request)):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        if not self._publish_limiter.allow():
            return JSONResponse({"error": "rate limited"}, status_code=429)
        try:
            body = await request.json()
            code = body["code"]
            ts = float(body.get("ts") or time.time())
        except (ValueError, TypeError, KeyError):
            return JSONResponse({"error": "bad request"}, status_code=400)
        try:
            changed = self.state.publish(code, ts)
        except ValueError:
            return JSONResponse({"error": "bad request"}, status_code=400)
        if changed:
            self._fan_out({"code": code, "ts": ts})
        return Response(status_code=204)

    async def subscribe(self, request):
        if not self.state.is_subscriber(_bearer(request)):
            return JSONResponse({"error": "unauthorized"}, status_code=401)

        queue: asyncio.Queue = asyncio.Queue(maxsize=SUBSCRIBER_QUEUE_SIZE)
        self._subscribers.add(queue)
        current = self.state.current_code()
        if current is not None:
            queue.put_nowait({"code": current.code, "ts": current.ts})

        async def stream():
            try:
                while True:
                    try:
                        payload = await asyncio.wait_for(
                            queue.get(), KEEPALIVE_SECONDS
                        )
                    except asyncio.TimeoutError:
                        yield b": keepalive\n\n"
                        continue
                    yield f"data: {json.dumps(payload)}\n\n".encode()
            finally:
                self._subscribers.discard(queue)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    async def mint_pairing_code(self, request):
        if not self.state.is_publisher(_bearer(request)):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        if not self._mint_limiter.allow():
            return JSONResponse({"error": "rate limited"}, status_code=429)
        return JSONResponse({"pairing_code": self.state.mint_pairing_code()})

    async def pair(self, request):
        if not self._pair_limiter.allow():
            return JSONResponse({"error": "rate limited"}, status_code=429)
        try:
            pairing_code = (await request.json())["pairing_code"]
        except (ValueError, TypeError, KeyError):
            return JSONResponse({"error": "bad request"}, status_code=400)
        token = self.state.redeem_pairing_code(pairing_code)
        if token is None:
            return JSONResponse(
                {"error": "invalid or expired pairing code"}, status_code=400
            )
        try:
            save_tokens(self.tokens_path, self.state.subscriber_tokens)
        except OSError as exc:
            # The pairing code is already burned and the token is already live
            # in memory. Returning 500 here would leave the phone with nothing
            # and the code unusable, and re-running pair.py would hit the same
            # wall. A subscriber that works until the next restart is strictly
            # better (spec §7: every failure degrades). Never logs the token.
            logger.warning(
                "could not persist subscriber tokens to %s (%s); the new "
                "subscriber will stop working when the relay restarts",
                self.tokens_path,
                exc,
            )
        return JSONResponse({"subscriber_token": token})


def build_app(
    state: RelayState,
    tokens_path,
    *,
    publish_limiter=None,
    pair_limiter=None,
    mint_limiter=None,
) -> Starlette:
    relay = Relay(
        state,
        tokens_path,
        publish_limiter=publish_limiter,
        pair_limiter=pair_limiter,
        mint_limiter=mint_limiter,
    )
    app = Starlette(
        routes=[
            Route("/publish", relay.publish, methods=["POST"]),
            Route("/subscribe", relay.subscribe, methods=["GET"]),
            Route("/pairing-codes", relay.mint_pairing_code, methods=["POST"]),
            Route("/pair", relay.pair, methods=["POST"]),
        ]
    )
    app.state.relay = relay
    return app


def create_app() -> Starlette:
    publisher_token = os.environ.get("JBVIP_PUBLISHER_TOKEN", "")
    if not publisher_token:
        raise RuntimeError("JBVIP_PUBLISHER_TOKEN is not set")
    tokens_path = os.environ.get("JBVIP_TOKENS_FILE", DEFAULT_TOKENS_PATH)
    state = RelayState(publisher_token, load_tokens(tokens_path))
    return build_app(state, tokens_path)
