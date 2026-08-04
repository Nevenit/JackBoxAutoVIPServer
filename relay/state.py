"""Relay decisions with no HTTP and no disk, so they test as plain calls.

A *session* is the unit of tenancy: one host PC's game night. It owns its own
publisher and subscriber tokens and expires on its own, so two sessions can
never see each other's codes.

The room-code plausibility check duplicates `extractor.roomcode.looks_like_code`
on purpose: the relay deploys to a VPS on its own and must not import the
extractor package.
"""
import math
import secrets
import time
from dataclasses import dataclass

FRESH_SECONDS = 120.0
# Long enough not to cut a game night short, short enough that an abandoned
# session and every credential it issued are gone by the next day.
SESSION_IDLE_SECONDS = 4 * 3600
# A session may be kept alive by activity, but not forever: a subscriber
# holding a stream open touches it every keepalive, so without a ceiling one
# client could pin a slot indefinitely and the relay would never recover its
# capacity. Comfortably longer than any game night.
MAX_SESSION_LIFETIME_SECONDS = 12 * 3600
MAX_SESSIONS = 500
# A Jackbox room holds about ten players.
MAX_SUBSCRIBERS_PER_SESSION = 16
# 12 url-safe characters: short enough to sit in a QR code alongside a token,
# and it is never a credential on its own.
SESSION_ID_BYTES = 9
TOKEN_BYTES = 32


@dataclass(frozen=True)
class CodeRecord:
    """A published code and the two clocks that saw it.

    `ts` is when the host PC observed the code; it travels to clients, which
    run their own end-to-end freshness check against it. `received_at` is the
    relay's own clock and is what the freshness window here uses, so skew on
    the host's clock — a dual-boot PC with Windows writing localtime to the RTC
    is hours out — cannot make every code look stale or every code look fresh.
    """

    code: str
    ts: float
    received_at: float


@dataclass
class Session:
    """One game night. Mutable: `last_activity` and `record` change over its life.

    `session_id` is carried on the record so callers never have to pair it back
    up with its key in the session table.
    """

    session_id: str
    publisher_token: str
    subscriber_token: str
    created_at: float
    last_activity: float
    record: CodeRecord | None = None


class SessionCapacityError(RuntimeError):
    """Raised by create_session when MAX_SESSIONS live sessions already exist.

    Deliberately not "evict the oldest": a live game night must never be ended
    to admit a stranger (spec §8).
    """


def _looks_like_code(code) -> bool:
    return (
        isinstance(code, str)
        and 2 <= len(code) <= 8
        and code.isascii()
        and code.isalnum()
    )


def _token_matches(candidate, known: str) -> bool:
    # Starlette decodes headers as latin-1 per the ASGI spec, so any byte >=
    # 0x80 in an Authorization header arrives as a non-ASCII str — which
    # secrets.compare_digest raises TypeError on. Unauthenticated callers must
    # get a 401, not a traceback, so the ASCII check is part of the guard.
    if not isinstance(candidate, str) or not candidate or not candidate.isascii():
        return False
    return secrets.compare_digest(candidate, known)


class RateLimiter:
    """Sliding-window counter. `now` is injected so tests need not sleep."""

    def __init__(self, limit: int, window_seconds: float, now=time.monotonic):
        self._limit = limit
        self._window = window_seconds
        self._now = now
        self._hits: list[float] = []

    def allow(self) -> bool:
        cutoff = self._now() - self._window
        self._hits = [hit for hit in self._hits if hit > cutoff]
        if len(self._hits) >= self._limit:
            return False
        self._hits.append(self._now())
        return True


class RelayState:
    """The session table. Everything the relay decides, and nothing it does."""

    def __init__(self, now=time.time):
        self._now = now
        self._sessions: dict[str, Session] = {}

    # --- lifecycle ---
    def create_session(self) -> Session:
        """Mint a session. Raises SessionCapacityError when full.

        Does not evict internally: evicting a session means closing its live
        SSE queues, which only the app layer can do. Callers run
        `evict_expired()` first and close whatever it hands back.
        """
        if len(self._sessions) >= MAX_SESSIONS:
            raise SessionCapacityError("session capacity reached")
        now = self._now()
        # Astronomically unlikely at 72 bits of entropy with at most
        # MAX_SESSIONS live sessions, but a collision would hand a brand new
        # session's key to an old one's still-registered queues — the one
        # remaining way session isolation could break. Cheap to close.
        session_id = secrets.token_urlsafe(SESSION_ID_BYTES)
        while session_id in self._sessions:
            session_id = secrets.token_urlsafe(SESSION_ID_BYTES)
        session = Session(
            session_id=session_id,
            publisher_token=secrets.token_urlsafe(TOKEN_BYTES),
            subscriber_token=secrets.token_urlsafe(TOKEN_BYTES),
            created_at=now,
            last_activity=now,
        )
        self._sessions[session.session_id] = session
        return session

    def _is_expired(self, session: Session) -> bool:
        now = self._now()
        return (
            session.last_activity <= now - SESSION_IDLE_SECONDS
            or session.created_at <= now - MAX_SESSION_LIFETIME_SECONDS
        )

    def session(self, session_id) -> Session | None:
        if not isinstance(session_id, str):
            return None
        session = self._sessions.get(session_id)
        if session is None or self._is_expired(session):
            return None
        return session

    def delete_session(self, session_id) -> bool:
        """Drop a session. True when there was one to drop."""
        if not isinstance(session_id, str):
            return False
        return self._sessions.pop(session_id, None) is not None

    def evict_expired(self) -> list[str]:
        """Drop expired sessions and return their ids so their queues can be closed.

        Uses the same _is_expired predicate as session(), so a record that
        the read path has already started refusing can never linger here
        indefinitely, and the two can never disagree about what is alive.
        """
        dead = [
            session_id
            for session_id, session in self._sessions.items()
            if self._is_expired(session)
        ]
        for session_id in dead:
            del self._sessions[session_id]
        return dead

    # --- authentication ---
    def is_publisher(self, session_id, token) -> bool:
        session = self.session(session_id)
        return session is not None and _token_matches(token, session.publisher_token)

    def is_subscriber(self, session_id, token) -> bool:
        session = self.session(session_id)
        return session is not None and _token_matches(token, session.subscriber_token)

    # --- room codes ---
    def publish(self, session_id, code, ts: float) -> bool:
        """Store the code. Returns True when this changed the session's code.

        Raises KeyError for an unknown session: callers authenticate first, and
        authenticating already proves the session exists.
        """
        session = self.session(session_id)
        if session is None:
            raise KeyError(session_id)
        if not _looks_like_code(code):
            raise ValueError("implausible room code")
        ts = float(ts)
        # json.loads accepts a bare NaN and `now - nan > window` is False, so a
        # NaN stamp would be permanently fresh and json.dumps would re-emit it
        # as JSON no client can parse.
        if not math.isfinite(ts):
            raise ValueError("ts must be a finite number")
        now = self._now()
        changed = session.record is None or session.record.code != code
        session.record = CodeRecord(code=code, ts=ts, received_at=now)
        session.last_activity = now
        return changed

    def current_code(self, session_id) -> CodeRecord | None:
        """The session's code, or None when absent or older than the window.

        Gated on the relay's own receipt time, never the publisher's stamp —
        see CodeRecord.
        """
        session = self.session(session_id)
        if session is None or session.record is None:
            return None
        if self._now() - session.record.received_at > FRESH_SECONDS:
            return None
        return session.record

    # --- activity ---
    def touch(self, session_id) -> None:
        """Mark the session live. Subscribing counts as activity, not just publishing."""
        session = self.session(session_id)
        if session is not None:
            session.last_activity = self._now()

    def expires_at(self, session_id) -> float | None:
        """The deadline after which the session dies "unless something happens".

        The earlier of the two: usually the idle deadline, but late in a
        session kept alive by a held subscribe stream the absolute ceiling
        becomes the one that actually binds, and callers (the QR window, the
        API response) should see whichever one will really end the session.
        """
        session = self.session(session_id)
        if session is None:
            return None
        return min(
            session.last_activity + SESSION_IDLE_SECONDS,
            session.created_at + MAX_SESSION_LIFETIME_SECONDS,
        )
