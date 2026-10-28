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

# Long enough not to cut a game night short, short enough that an abandoned
# session and every credential it issued are gone by the next day.
SESSION_IDLE_SECONDS = 4 * 3600
# A session may be kept alive by activity, but not forever: a subscriber
# holding a stream open touches it every keepalive, so without a ceiling one
# client could pin a slot indefinitely and the relay would never recover its
# capacity. Comfortably longer than any game night.
MAX_SESSION_LIFETIME_SECONDS = 12 * 3600
# How long a session may sit without a room (cleared, or never given one)
# before the night counts as over. Measured from the clear or from creation,
# never from activity: a phone listening to the empty session touches it on
# every keepalive, and that must not keep a finished night alive until the
# lifetime ceiling.
ROOMLESS_SECONDS = 2 * 3600
MAX_SESSIONS = 500
# A Jackbox room holds about ten players.
MAX_SUBSCRIBERS_PER_SESSION = 16
# 12 url-safe characters: short enough to sit in a QR code alongside a token,
# and it is never a credential on its own.
SESSION_ID_BYTES = 9
TOKEN_BYTES = 32


@dataclass(frozen=True)
class CodeRecord:
    """A published code and the stamp the host gave it.

    `ts` is when the host PC observed the code; it is passed through to clients
    unchanged. Nothing on the relay gates on age any more: a code stays current
    until the host publishes another or clears it.
    """

    code: str
    ts: float


@dataclass
class Session:
    """One game night. Mutable: `last_activity`, `record`, `roomless_since` and
    `qr_hidden` change over its life.

    `session_id` is carried on the record so callers never have to pair it back
    up with its key in the session table.

    `roomless_since` is when the session last had no room: its creation until
    the first publish, None while a code is published, then the moment of a
    clear that removed one. Only those events move it, never activity. It has
    no default so that a session can't be built without saying which it is.

    `qr_hidden` is whether the TV's QR card is hidden for the rest of the
    night. It is not activity: setting it moves neither `last_activity` nor
    `roomless_since`.
    """

    session_id: str
    publisher_token: str
    subscriber_token: str
    created_at: float
    last_activity: float
    roomless_since: float | None
    record: CodeRecord | None = None
    qr_hidden: bool = False


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
            roomless_since=now,
        )
        self._sessions[session.session_id] = session
        return session

    def _is_expired(self, session: Session) -> bool:
        now = self._now()
        return (
            session.last_activity <= now - SESSION_IDLE_SECONDS
            or session.created_at <= now - MAX_SESSION_LIFETIME_SECONDS
            or (
                session.roomless_since is not None
                and session.roomless_since <= now - ROOMLESS_SECONDS
            )
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
        # json.loads accepts a bare NaN, and json.dumps would re-emit it as
        # JSON no client can parse.
        if not math.isfinite(ts):
            raise ValueError("ts must be a finite number")
        now = self._now()
        changed = session.record is None or session.record.code != code
        session.record = CodeRecord(code=code, ts=ts)
        session.roomless_since = None
        session.last_activity = now
        return changed

    def clear_code(self, session_id) -> bool:
        """Forget the session's code: the host has no room right now.

        Returns True when this removed a code. A clear with nothing to remove
        changes nothing, so a launcher that clears on every exit cannot push a
        roomless session's deadline out. Raises KeyError for an unknown
        session, like publish.
        """
        session = self.session(session_id)
        if session is None:
            raise KeyError(session_id)
        if session.record is None:
            return False
        now = self._now()
        session.record = None
        session.roomless_since = now
        session.last_activity = now
        return True

    def current_code(self, session_id) -> CodeRecord | None:
        """The session's code, or None when there is no such session or it has
        no room right now.

        Served at any age. A code stays current until the host replaces or
        clears it, and the launcher clears it when its pack exits, so an old
        code is one whose pack is still running.
        """
        session = self.session(session_id)
        return None if session is None else session.record

    # --- the TV's QR card ---
    def set_qr_hidden(self, session_id, hidden: bool) -> bool:
        """Hide or show the QR card for the night. True when this changed it.

        Hiding twice is not a change, so phones are told once. Deliberately not
        activity: a phone tapping Hide must not push the session's deadlines
        out. Raises KeyError for an unknown session, like publish.
        """
        session = self.session(session_id)
        if session is None:
            raise KeyError(session_id)
        changed = session.qr_hidden != hidden
        session.qr_hidden = hidden
        return changed

    # --- activity ---
    def touch(self, session_id) -> None:
        """Mark the session live. Subscribing counts as activity, not just publishing."""
        session = self.session(session_id)
        if session is not None:
            session.last_activity = self._now()

    def expires_at(self, session_id) -> float | None:
        """The deadline after which the session dies "unless something happens".

        The earliest of three: the idle deadline, the absolute ceiling and,
        while the session has no room, the roomless deadline. Usually it is
        the idle one, but late in a session kept alive by a held subscribe
        stream the ceiling binds, and a session with no room dies at the
        roomless deadline however often it is touched. The API response should
        show whichever one will really end the session.
        """
        session = self.session(session_id)
        if session is None:
            return None
        deadlines = [
            session.last_activity + SESSION_IDLE_SECONDS,
            session.created_at + MAX_SESSION_LIFETIME_SECONDS,
        ]
        if session.roomless_since is not None:
            deadlines.append(session.roomless_since + ROOMLESS_SECONDS)
        return min(deadlines)
