"""ASGI relay: many game nights at once, each blind to the others.

Thin by design — every decision lives in relay.state. This layer owns only the
two things that cannot live there: the live SSE queues and the rate limiters.
Room codes and tokens are never logged here or anywhere else in the relay.
"""
import asyncio
import json
import logging
import time
from pathlib import Path

from starlette.applications import Starlette
from starlette.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    Response,
    StreamingResponse,
)
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

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
# Hiding the QR card is open to every phone, so it gets a budget of its own,
# also per session: a phone tapping Hide must never be able to use up the
# publish budget, or room codes would stop reaching anyone.
QR_LIMIT_PER_MINUTE = 30
# Checking an admin key is open to every subscriber, so guessing is capped per
# session; a stranger's guesses must not spend another night's budget.
ADMIN_GUESS_LIMIT_PER_MINUTE = 10

STATIC_DIR = Path(__file__).parent / "static"

_LANDING_PAGE = (
    "<!doctype html><meta charset=utf-8>"
    "<meta name=viewport content='width=device-width,initial-scale=1'>"
    "<title>Jackbox night</title>"
    "<p style='font:1rem/1.5 system-ui;margin:2rem'>"
    "Scan the QR code on the host's screen to join."
)

_UNAUTHORIZED = {"error": "unauthorized"}


def _bearer(request) -> str:
    header = request.headers.get("authorization", "")
    prefix = "Bearer "
    return header[len(prefix):] if header.startswith(prefix) else ""


class Relay:
    def __init__(self, state: RelayState, *, create_limiter=None,
                 publish_limiter_factory=None, qr_limiter_factory=None,
                 admin_guess_limiter_factory=None):
        self.state = state
        # A dict, not a set, for its insertion order: the first key is the
        # session's oldest stream. Values are the stream's role: "admin" or
        # "guest".
        self._queues: dict[str, dict[asyncio.Queue, str]] = {}
        self._publish_limiters: dict[str, RateLimiter] = {}
        self._qr_limiters: dict[str, RateLimiter] = {}
        self._admin_guess_limiters: dict[str, RateLimiter] = {}
        # A head start in progress: the room guests are still on, and the timer
        # that hands them the current one.
        self._guest_room: dict[str, tuple] = {}
        self._guest_timer: dict[str, asyncio.TimerHandle] = {}
        self._create_limiter = create_limiter or RateLimiter(
            limit=CREATE_LIMIT_PER_MINUTE, window_seconds=60.0
        )
        self._new_publish_limiter = publish_limiter_factory or (
            lambda: RateLimiter(limit=PUBLISH_LIMIT_PER_MINUTE, window_seconds=60.0)
        )
        self._new_qr_limiter = qr_limiter_factory or (
            lambda: RateLimiter(limit=QR_LIMIT_PER_MINUTE, window_seconds=60.0)
        )
        self._new_admin_guess_limiter = admin_guess_limiter_factory or (
            lambda: RateLimiter(
                limit=ADMIN_GUESS_LIMIT_PER_MINUTE, window_seconds=60.0
            )
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
        """Forget a dead session's queues and limiters, ending its streams.

        Idempotent: eviction and an explicit DELETE can both reach the same id.
        """
        self._publish_limiters.pop(session_id, None)
        self._qr_limiters.pop(session_id, None)
        self._admin_guess_limiters.pop(session_id, None)
        self._end_window(session_id)
        for queue in self._queues.pop(session_id, ()):
            self._close_queue(queue)

    def _end_window(self, session_id: str) -> None:
        """Forget a pending head start without delivering anything."""
        self._guest_room.pop(session_id, None)
        timer = self._guest_timer.pop(session_id, None)
        if timer is not None:
            timer.cancel()

    def _tell_room(self, session_id: str, *roles: str) -> None:
        """Send each role its own view of the room (see _room_event)."""
        for role in roles:
            self._fan_out(session_id,
                          self._room_event(session_id, admin=role == "admin"), role)

    def _guests_catch_up(self, session_id: str) -> None:
        """The head start is over: guests get the current room."""
        self._guest_timer.pop(session_id, None)
        if self._guest_room.pop(session_id, None) is not None:
            self._tell_room(session_id, "guest")

    def _fan_out(self, session_id: str, payload: dict, role=None) -> None:
        """Queue the payload on every stream, or only those with this role."""
        for queue, queue_role in list(self._queues.get(session_id, {}).items()):
            if role is not None and queue_role != role:
                continue
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

    def _qr_hidden(self, session_id) -> bool:
        """The session's QR flag; False for one that has just expired."""
        session = self.state.session(session_id)
        return session is not None and session.qr_hidden

    def _event(self, session_id, code, ts) -> dict:
        """The payload of every SSE event: a room (or None) and the QR flag.

        Every event names the room because the page reads `code` off each one
        and leaves the frame alone only when it equals the room already framed.
        News about the QR therefore repeats the room (see _room_event) rather
        than leaving `code` out, which the page would take as a closed room.
        """
        return {"code": code, "ts": ts, "qr_hidden": self._qr_hidden(session_id)}

    def _room_event(self, session_id, *, admin: bool) -> dict:
        """The room this stream should see as an event; `code` is None with none.

        A guest inside a head start still sees the room it was on.
        """
        if not admin and session_id in self._guest_room:
            return self._event(session_id, *self._guest_room[session_id])
        current = self.state.current_code(session_id)
        if current is None:
            return self._event(session_id, None, time.time())
        return self._event(session_id, current.code, current.ts)

    # --- routes ---
    def _refuse(self, request, session_id, *, subscriber=False, either=False):
        """None when the caller may proceed, else the response to return.

        The caller must hold the publisher token, or with `subscriber` the
        subscriber token, or with `either` whichever of the two.

        Spec §8 distinguishes two failures. A session that no longer exists is
        a **404**: the publisher must be able to tell "my session expired,
        stop trying forever" from "my token is wrong". A session that exists
        but was handed the wrong token is a **401**, because a session id is
        not a capability on its own — confirming one exists gives away
        nothing that the QR code did not already publish.
        """
        if self.state.session(session_id) is None:
            return JSONResponse({"error": "no such session"}, status_code=404)
        token = _bearer(request)
        if either:
            allowed = (self.state.is_publisher(session_id, token)
                       or self.state.is_subscriber(session_id, token))
        else:
            holds = self.state.is_subscriber if subscriber else self.state.is_publisher
            allowed = holds(session_id, token)
        if not allowed:
            return JSONResponse(_UNAUTHORIZED, status_code=401)
        return None

    def _over_budget(self, session_id, *, qr=False):
        """None while the caller is within budget, else the 429 to return.

        Charges one request to the session's budget. Publishing and clearing
        share one; hiding and showing the QR (`qr=True`) share another, so a
        phone tapping Hide can't spend the budget room codes need. Reading or
        deleting the session is not charged. Run it after _refuse, never
        before: junk credentials must not be able to spend a real host's
        budget.
        """
        limiters, new_limiter = (
            (self._qr_limiters, self._new_qr_limiter)
            if qr
            else (self._publish_limiters, self._new_publish_limiter)
        )
        if session_id not in limiters:
            limiters[session_id] = new_limiter()
        if limiters[session_id].allow():
            return None
        return JSONResponse({"error": "rate limited"}, status_code=429)

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
        # Deliberately does not touch(): the QR card polls this every couple
        # of seconds, and polling must not be able to keep a session alive.
        return JSONResponse(
            {
                "subscribers": len(self._queues.get(session_id, ())),
                "expires_at": self.state.expires_at(session_id),
                "qr_hidden": self._qr_hidden(session_id),
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

    def _publish_news(self, session_id, previous) -> None:
        """Tell the streams about a changed room, admins first if a head start applies."""
        session = self.state.session(session_id)
        admins = "admin" in self._queues.get(session_id, {}).values()
        if (session_id not in self._guest_room and admins and session
                and session.admin_key is not None and session.head_start > 0):
            # (None, now) when guests were on no room: they get a null event.
            self._guest_room[session_id] = (
                (previous.code, previous.ts) if previous else (None, time.time())
            )
            self._guest_timer[session_id] = asyncio.get_running_loop().call_later(
                session.head_start, self._guests_catch_up, session_id
            )
        # An open window keeps its timer: a later room replaces the pending one.
        self._tell_room(session_id, "admin",
                        *(() if session_id in self._guest_room else ("guest",)))

    async def publish(self, request):
        session_id = request.path_params["session_id"]
        refusal = self._refuse(request, session_id)
        if refusal is not None:
            return refusal
        throttled = self._over_budget(session_id)
        if throttled is not None:
            return throttled
        try:
            body = await request.json()
            code = body["code"]
            raw_ts = body.get("ts")
            ts = float(raw_ts) if raw_ts is not None else time.time()
        except (ValueError, TypeError, KeyError):
            return JSONResponse({"error": "bad request"}, status_code=400)
        previous = self.state.current_code(session_id)
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
            self._publish_news(session_id, previous)
        self._reap()
        return Response(status_code=204)

    async def clear_code(self, request):
        """The host has no room right now: forget the code and tell the phones."""
        session_id = request.path_params["session_id"]
        refusal = self._refuse(request, session_id)
        if refusal is not None:
            return refusal
        throttled = self._over_budget(session_id)
        if throttled is not None:
            return throttled
        try:
            cleared = self.state.clear_code(session_id)
        except KeyError:
            # Expiry follows the clock, not the event loop, so the session can
            # pass its deadline between _refuse's check and this call.
            return JSONResponse({"error": "no such session"}, status_code=404)
        self._end_window(session_id)
        if cleared:
            self._tell_room(session_id, "admin", "guest")
        # 204 whether or not there was a code to remove: the launcher clears on
        # every exit and has no use for the difference.
        return Response(status_code=204)

    def _set_qr_hidden(self, request, hidden, *, either):
        """Shared by hide and show, which differ in the flag and in who may ask."""
        session_id = request.path_params["session_id"]
        refusal = self._refuse(request, session_id, either=either)
        if refusal is not None:
            return refusal
        throttled = self._over_budget(session_id, qr=True)
        if throttled is not None:
            return throttled
        try:
            changed = self.state.set_qr_hidden(session_id, hidden)
        except KeyError:
            # Expiry follows the clock, not the event loop, so the session can
            # pass its deadline between _refuse's check and this call.
            return JSONResponse({"error": "no such session"}, status_code=404)
        if changed:
            # The current room, unchanged: this is news about the QR only, and
            # the page must not mistake it for a new or closed room.
            self._tell_room(session_id, "admin", "guest")
        # 204 whether or not the flag changed: both calls are idempotent.
        return Response(status_code=204)

    async def hide_qr(self, request):
        """Any phone, or the host, hides the TV's QR card for the rest of the night."""
        return self._set_qr_hidden(request, True, either=True)

    async def show_qr(self, request):
        """Only the host brings the QR card back."""
        return self._set_qr_hidden(request, False, either=False)

    async def register_admin(self, request):
        """The host PC registers tonight's admin key (a hash, never the password)."""
        session_id = request.path_params["session_id"]
        refusal = self._refuse(request, session_id)
        if refusal is not None:
            return refusal
        throttled = self._over_budget(session_id)
        if throttled is not None:
            return throttled
        try:
            body = await request.json()
            key, head_start = body["key"], body["head_start"]
            self.state.register_admin(session_id, key, head_start)
        except (ValueError, TypeError, KeyError) as exc:
            if isinstance(exc, KeyError) and exc.args == (session_id,):
                # The session expired inside the await above.
                return JSONResponse({"error": "no such session"}, status_code=404)
            return JSONResponse({"error": "bad request"}, status_code=400)
        return Response(status_code=204)

    def _admin_status(self, request, session_id) -> str:
        """What the request's X-Jbvip-Admin key makes this stream.

        Run after the subscriber-token check. Only a wrong key spends the
        guess budget, so the admin phone's own reconnects never lock it out.
        Once the budget is spent no key is checked at all: the real one gets
        `limited` too until the minute rolls, which keeps brute force stopped.
        """
        key = request.headers.get("x-jbvip-admin") or None
        session = self.state.session(session_id)
        if key is None or session is None or session.admin_key is None:
            return self.state.admin_status(session_id, key)
        limiter = self._admin_guess_limiters.setdefault(
            session_id, self._new_admin_guess_limiter()
        )
        if limiter.exhausted():
            return "limited"
        status = self.state.admin_status(session_id, key)
        if status == "wrong":
            limiter.allow()
        return status

    async def subscribe(self, request):
        session_id = request.path_params["session_id"]
        refusal = self._refuse(request, session_id, subscriber=True)
        if refusal is not None:
            return refusal
        status = self._admin_status(request, session_id)
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
        role = "admin" if status == "admin" else "guest"
        queues[queue] = role
        self.state.touch(session_id)
        # "No room" is news too: a phone that slept or reconnected through a
        # clear still has the closed room in its frame. The QR flag rides on
        # the same event, so a phone learns it on connecting too.
        queue.put_nowait(self._room_event(session_id, admin=role == "admin"))

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
            headers={
                "Cache-Control": "no-store",
                "X-Accel-Buffering": "no",
                "X-Jbvip-Admin": status,
            },
        )


async def landing_page(request):
    return HTMLResponse(_LANDING_PAGE, headers={"Cache-Control": "no-store"})


async def player_page(request):
    # The subscriber token lives in the URL fragment, which browsers never
    # transmit, so nothing server-side — including this route — can see it.
    return FileResponse(STATIC_DIR / "player.html",
                        headers={"Cache-Control": "no-store"})


def build_app(state: RelayState, *, create_limiter=None,
              publish_limiter_factory=None, qr_limiter_factory=None,
              admin_guess_limiter_factory=None) -> Starlette:
    relay = Relay(
        state,
        create_limiter=create_limiter,
        publish_limiter_factory=publish_limiter_factory,
        qr_limiter_factory=qr_limiter_factory,
        admin_guess_limiter_factory=admin_guess_limiter_factory,
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
            Route("/sessions/{session_id}/qr/hide", relay.hide_qr,
                  methods=["POST"]),
            Route("/sessions/{session_id}/qr/show", relay.show_qr,
                  methods=["POST"]),
            Route("/sessions/{session_id}/admin", relay.register_admin,
                  methods=["PUT"]),
            Route("/sessions/{session_id}/subscribe", relay.subscribe,
                  methods=["GET"]),
            Route("/", landing_page, methods=["GET"]),
            Route("/j", player_page, methods=["GET"]),
            Mount("/static", StaticFiles(directory=STATIC_DIR)),
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
