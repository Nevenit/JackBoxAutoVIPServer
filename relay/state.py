"""Relay decisions with no HTTP and no disk, so they test as plain calls.

The room-code plausibility check duplicates `extractor.roomcode.looks_like_code`
on purpose: the relay deploys to a VPS on its own and must not import the
extractor package.
"""
import math
import secrets
import time
from dataclasses import dataclass
from typing import Iterable

FRESH_SECONDS = 120.0
PAIRING_TTL_SECONDS = 300.0
PAIRING_CODE_LENGTH = 6
# No O/0 or I/1 — this gets read off a screen and typed on a phone.
PAIRING_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


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


def _looks_like_code(code) -> bool:
    return (
        isinstance(code, str)
        and 2 <= len(code) <= 8
        and code.isascii()
        and code.isalnum()
    )


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
    def __init__(
        self,
        publisher_token: str,
        subscriber_tokens: Iterable[str] = (),
        now=time.time,
    ):
        if not publisher_token:
            raise ValueError("publisher_token must not be empty")
        self._publisher_token = publisher_token
        self._subscriber_tokens: set[str] = set(subscriber_tokens)
        self._now = now
        self._record: CodeRecord | None = None
        self._pairing_codes: dict[str, float] = {}

    # --- authentication ---
    # Starlette decodes headers as latin-1 per the ASGI spec, so any byte >=
    # 0x80 in an Authorization header arrives as a non-ASCII str — which
    # secrets.compare_digest raises TypeError on. Unauthenticated callers must
    # get a 401, not a traceback, so the ASCII check is part of the guard.
    def is_publisher(self, token) -> bool:
        if not isinstance(token, str) or not token or not token.isascii():
            return False
        return secrets.compare_digest(token, self._publisher_token)

    def is_subscriber(self, token) -> bool:
        if not isinstance(token, str) or not token or not token.isascii():
            return False
        return any(
            secrets.compare_digest(token, known) for known in self._subscriber_tokens
        )

    @property
    def subscriber_tokens(self) -> set[str]:
        return set(self._subscriber_tokens)

    # --- room codes ---
    def publish(self, code: str, ts: float) -> bool:
        """Store the code. Returns True when this changed the current code."""
        if not _looks_like_code(code):
            raise ValueError("implausible room code")
        ts = float(ts)
        # json.loads accepts a bare NaN and `now - nan > window` is False, so a
        # NaN stamp would be permanently fresh and json.dumps would re-emit it
        # as JSON no client can parse.
        if not math.isfinite(ts):
            raise ValueError("ts must be a finite number")
        changed = self._record is None or self._record.code != code
        self._record = CodeRecord(code=code, ts=ts, received_at=self._now())
        return changed

    def current_code(self) -> CodeRecord | None:
        """The current code, or None when absent or older than the window.

        Gated on the relay's own receipt time, never the publisher's stamp —
        see CodeRecord. Spec §5.1 stores `{code, received_at}` for this reason.
        """
        if self._record is None:
            return None
        if self._now() - self._record.received_at > FRESH_SECONDS:
            return None
        return self._record

    # --- pairing ---
    def mint_pairing_code(self) -> str:
        self._expire_pairing_codes()
        code = "".join(
            secrets.choice(PAIRING_ALPHABET) for _ in range(PAIRING_CODE_LENGTH)
        )
        self._pairing_codes[code] = self._now() + PAIRING_TTL_SECONDS
        return code

    def redeem_pairing_code(self, code: str) -> str | None:
        """Burn a valid pairing code and return a fresh subscriber token."""
        self._expire_pairing_codes()
        if not isinstance(code, str) or code not in self._pairing_codes:
            return None
        del self._pairing_codes[code]
        token = secrets.token_urlsafe(32)
        self._subscriber_tokens.add(token)
        return token

    def _expire_pairing_codes(self) -> None:
        now = self._now()
        expired = [code for code, exp in self._pairing_codes.items() if exp <= now]
        for code in expired:
            del self._pairing_codes[code]
