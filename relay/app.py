"""ASGI relay: many game nights at once, each blind to the others.

Thin by design — every decision lives in relay.state. This layer owns only the
two things that cannot live there: the live SSE queues and the rate limiters.
Room codes and tokens are never logged here or anywhere else in the relay.
"""
import asyncio
import json
import logging
import time

from starlette.applications import Starlette
from starlette.responses import (
    HTMLResponse,
    JSONResponse,
    Response,
    StreamingResponse,
)
from starlette.routing import Route

from relay.state import (
    MAX_SUBSCRIBERS_PER_SESSION,
    RateLimiter,
    RelayState,
    SessionCapacityError,
)

logger = logging.getLogger(__name__)

KEEPALIVE_SECONDS = 20.0
# A subscriber that has stopped draining is dropped once this many payloads
# have backed up. Also the backstop for a stream cancelled before its generator
# body runs, whose `finally` never executes and so never unregisters the queue.
SUBSCRIBER_QUEUE_SIZE = 64
# POST /sessions is unauthenticated by design, so its budget can only be
# global. The reverse proxy in front does per-client limiting; this caps the
# total damage one bad afternoon can do.
CREATE_LIMIT_PER_MINUTE = 30
# Publishing is authenticated, so the budget is per session: one noisy host
# must never be able to lock another game night out.
PUBLISH_LIMIT_PER_MINUTE = 60

# The subscriber token lives in the URL fragment, which browsers never
# transmit, so nothing server-side — including this page — can see it.
_JOIN_PAGE = (
    "<!doctype html><meta charset=utf-8>"
    "<meta name=viewport content='width=device-width,initial-scale=1'>"
    "<title>jbvip</title>"
    "<p style='font:1rem/1.5 system-ui;margin:2rem'>"
    "This QR code pairs a phone with a Jackbox host. Open it in the jbvip app."
)

_UNAUTHORIZED = {"error": "unauthorized"}


def _bearer(request) -> str:
    header = request.headers.get("authorization", "")
    prefix = "Bearer "
    return header[len(prefix):] if header.startswith(prefix) else ""


class Relay:
    def __init__(self, state: RelayState, *, create_limiter=None,
                 publish_limiter_factory=None):
        self.state = state
        # A dict, not a set, for its insertion order: the first key is the
        # session's oldest stream. Values are unused.
        self._queues: dict[str, dict[asyncio.Queue, None]] = {}
        self._publish_limiters: dict[str, RateLimiter] = {}
        self._create_limiter = create_limiter or RateLimiter(
            limit=CREATE_LIMIT_PER_MINUTE, window_seconds=60.0
        )
        self._new_publish_limiter = publish_limiter_factory or (
            lambda: RateLimiter(limit=PUBLISH_LIMIT_PER_MINUTE, window_seconds=60.0)
        )

    # --- bookkeeping ---
    def _reap(self) -> None:
        """Drop idle sessions and close whatever was still listening to them."""
        for session_id in self.state.evict_expired():
            self._release(session_id)

    @staticmethod
    def _close_queue(queue: asyncio.Queue) -> None:
        """Signal a stream to end. Works even on a queue nobody is draining."""
        try:
            queue.put_nowait(None)  # sentinel: the stream generator returns
        except asyncio.QueueFull:
            # Displace a payload rather than leave the generator alive until
            # the client's TCP connection eventually dies.
            queue.get_nowait()
            queue.put_nowait(None)

    def _release(self, session_id: str) -> None:
        """Forget a dead session's queues and limiter, ending its streams.

        Idempotent: eviction and an explicit DELETE can both reach the same id.
        """
        self._publish_limiters.pop(session_id, None)
        for queue in self._queues.pop(session_id, ()):
            self._close_queue(queue)

    def _fan_out(self, session_id: str, payload: dict) -> None:
        for queue in list(self._queues.get(session_id, ())):
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                # It is not draining and never will. Unregistering it is not
                # enough — an unregistered queue can no longer be reached by
                # _release, so its generator would keepalive forever and
                # survive both eviction and DELETE, and it would have freed a
                # slot under MAX_SUBSCRIBERS_PER_SESSION for a replacement.
                # Close it properly so the socket actually goes away.
                self._queues.get(session_id, {}).pop(queue, None)
                self._close_queue(queue)

    # --- routes ---
    def _refuse(self, request, session_id, *, subscriber=False):
        """None when the caller may proceed, else the response to return.

        Spec §8 distinguishes two failures. A session that no longer exists is
        a **404**: the publisher must be able to tell "my session expired,
        stop trying forever" from "my token is wrong". A session that exists
        but was handed the wrong token is a **401**, because a session id is
        not a capability on its own — confirming one exists gives away
        nothing that the QR code did not already publish.
        """
        if self.state.session(session_id) is None:
            return JSONResponse({"error": "no such session"}, status_code=404)
        holds = self.state.is_subscriber if subscriber else self.state.is_publisher
        if not holds(session_id, _bearer(request)):
            return JSONResponse(_UNAUTHORIZED, status_code=401)
        return None

    async def create_session(self, request):
        if not self._create_limiter.allow():
            return JSONResponse({"error": "rate limited"}, status_code=429)
        self._reap()
        try:
            session = self.state.create_session()
        except SessionCapacityError:
            # Spec §8: never end a live game night to admit a stranger.
            return JSONResponse({"error": "at capacity"}, status_code=503)
        return JSONResponse(
            {
                "session_id": session.session_id,
                "publisher_token": session.publisher_token,
                "subscriber_token": session.subscriber_token,
                "expires_at": self.state.expires_at(session.session_id),
            },
            status_code=201,
            headers={"Cache-Control": "no-store"},
        )

    async def get_session(self, request):
        session_id = request.path_params["session_id"]
        refusal = self._refuse(request, session_id)
        if refusal is not None:
            return refusal
        # Deliberately does not touch(): the QR window polls this once a
        # second, and polling must not be able to keep a session alive.
        return JSONResponse(
            {
                "subscribers": len(self._queues.get(session_id, ())),
                "expires_at": self.state.expires_at(session_id),
            },
            headers={"Cache-Control": "no-store"},
        )

    async def delete_session(self, request):
        session_id = request.path_params["session_id"]
        refusal = self._refuse(request, session_id)
        if refusal is not None:
            return refusal
        self.state.delete_session(session_id)
        self._release(session_id)
        return Response(status_code=204)

    async def publish(self, request):
        session_id = request.path_params["session_id"]
        refusal = self._refuse(request, session_id)
        if refusal is not None:
            return refusal
        limiter = self._publish_limiters.setdefault(
            session_id, self._new_publish_limiter()
        )
        if not limiter.allow():
            return JSONResponse({"error": "rate limited"}, status_code=429)
        try:
            body = await request.json()
            code = body["code"]
            raw_ts = body.get("ts")
            ts = float(raw_ts) if raw_ts is not None else time.time()
        except (ValueError, TypeError, KeyError):
            return JSONResponse({"error": "bad request"}, status_code=400)
        try:
            changed = self.state.publish(session_id, code, ts)
        except ValueError:
            return JSONResponse({"error": "bad request"}, status_code=400)
        except KeyError:
            # `await request.json()` above is a real suspension point: another
            # request's reap can evict this session inside it. Without this the
            # host would get a 500 for something it did nothing wrong.
            return JSONResponse({"error": "no such session"}, status_code=404)
        if changed:
            self._fan_out(session_id, {"code": code, "ts": ts})
        self._reap()
        return Response(status_code=204)

    async def clear_code(self, request):
        """The host has no room right now: forget the code and tell the phones."""
        session_id = request.path_params["session_id"]
        refusal = self._refuse(request, session_id)
        if refusal is not None:
            return refusal
        # One budget for everything the publisher token does, as in publish.
        limiter = self._publish_limiters.setdefault(
            session_id, self._new_publish_limiter()
        )
        if not limiter.allow():
            return JSONResponse({"error": "rate limited"}, status_code=429)
        try:
            cleared = self.state.clear_code(session_id)
        except KeyError:
            # Expiry follows the clock, not the event loop, so the session can
            # pass its deadline between _refuse's check and this call.
            return JSONResponse({"error": "no such session"}, status_code=404)
        if cleared:
            self._fan_out(session_id, {"code": None, "ts": time.time()})
        # 204 whether or not there was a code to remove: the launcher clears on
        # every exit and has no use for the difference.
        return Response(status_code=204)

    async def subscribe(self, request):
        session_id = request.path_params["session_id"]
        refusal = self._refuse(request, session_id, subscriber=True)
        if refusal is not None:
            return refusal
        queues = self._queues.setdefault(session_id, {})
        if len(queues) >= MAX_SUBSCRIBERS_PER_SESSION:
            # A phone that sleeps or changes network leaves a stream that stays
            # registered until a keepalive write fails, so a full session is
            # likelier to hold ghosts than a full room of live phones. Drop the
            # oldest rather than lock the newcomer out of the night.
            oldest = next(iter(queues))
            del queues[oldest]
            self._close_queue(oldest)

        # NO `await` from here to the return. A code published in that window
        # would be lost: too late for the seed, too early for the fan-out.
        queue: asyncio.Queue = asyncio.Queue(maxsize=SUBSCRIBER_QUEUE_SIZE)
        queues[queue] = None
        self.state.touch(session_id)
        current = self.state.current_code(session_id)
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
                        if self.state.session(session_id) is None:
                            # The session is dead and nothing has reaped it (a
                            # night with no room ends on the clock alone), so
                            # the touch below would do nothing and this stream
                            # would keepalive forever. End it; reaping closes
                            # the session's other streams too.
                            self._reap()
                            return
                        # Touch on every keepalive, not just on connect. A
                        # whole game night is normally ONE publish — the
                        # publisher de-duplicates and a pack keeps one room
                        # code across many games — so a session with a phone
                        # listening all evening would otherwise be evicted out
                        # from under it at the four-hour mark. Spec §5.1
                        # promises the opposite; this is what makes it true.
                        self.state.touch(session_id)
                        yield b": keepalive\n\n"
                        continue
                    if payload is None:  # the session went away
                        return
                    yield f"data: {json.dumps(payload)}\n\n".encode()
            finally:
                self._queues.get(session_id, {}).pop(queue, None)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    async def join_page(self, request):
        return HTMLResponse(_JOIN_PAGE, headers={"Cache-Control": "no-store"})


def build_app(state: RelayState, *, create_limiter=None,
              publish_limiter_factory=None) -> Starlette:
    relay = Relay(
        state,
        create_limiter=create_limiter,
        publish_limiter_factory=publish_limiter_factory,
    )
    app = Starlette(
        routes=[
            Route("/sessions", relay.create_session, methods=["POST"]),
            # Two routes on one path: Starlette records a partial match on a
            # method mismatch and keeps looking, so GET and DELETE both
            # resolve and anything else still gets a 405.
            Route("/sessions/{session_id}", relay.get_session, methods=["GET"]),
            Route("/sessions/{session_id}", relay.delete_session,
                  methods=["DELETE"]),
            Route("/sessions/{session_id}/publish", relay.publish,
                  methods=["POST"]),
            Route("/sessions/{session_id}/code", relay.clear_code,
                  methods=["DELETE"]),
            Route("/sessions/{session_id}/subscribe", relay.subscribe,
                  methods=["GET"]),
            Route("/j", relay.join_page, methods=["GET"]),
        ]
    )
    app.state.relay = relay
    return app


def create_app() -> Starlette:
    """Entry point for `uvicorn --factory relay.app:create_app`.

    Takes no configuration at all: there is no longer a secret to inject and
    nothing to load from disk.
    """
    return build_app(RelayState())
