"""ASGI relay: takes room codes from the host PC, pushes them to subscribers.

Thin by design — every decision lives in relay.state. Room codes are never
logged here or anywhere else in the relay.
"""
import asyncio
import json
import os
import time

from starlette.applications import Starlette
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from relay.state import RateLimiter, RelayState
from relay.tokens import load_tokens, save_tokens

KEEPALIVE_SECONDS = 20.0
DEFAULT_TOKENS_PATH = "/var/lib/jbvip/tokens.json"


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

    def _fan_out(self, payload: dict) -> None:
        for queue in list(self._subscribers):
            queue.put_nowait(payload)

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

        queue: asyncio.Queue = asyncio.Queue()
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
                    except TimeoutError:
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
        if not self._pair_limiter.allow():
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
        save_tokens(self.tokens_path, self.state.subscriber_tokens)
        return JSONResponse({"subscriber_token": token})


def build_app(
    state: RelayState, tokens_path, *, publish_limiter=None, pair_limiter=None
) -> Starlette:
    relay = Relay(
        state,
        tokens_path,
        publish_limiter=publish_limiter,
        pair_limiter=pair_limiter,
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
