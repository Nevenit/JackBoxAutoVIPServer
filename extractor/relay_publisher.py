"""Forward room codes to the relay. Best-effort: never raises into mitmproxy.

Uses stdlib urllib on a worker thread so the extractor picks up no new runtime
dependency — it runs inside mitmdump on the host at game-launch time.
"""
import asyncio
import json
import logging
import time
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

DEFAULT_ATTEMPTS = 3
DEFAULT_BACKOFF_SECONDS = 0.5
REQUEST_TIMEOUT_SECONDS = 5.0

# Every log line that means "the relay is not working" starts with this.
# jbvip-run.sh greps the proxy log for it to decide whether the run produced
# anything worth keeping — the relay is the one part of this that otherwise
# fails in total silence. Changing it means changing jbvip-run.sh too, and
# tests/test_launcher.py pins the two together.
LOG_MARKER = "jbvip-relay:"

# The relay cannot recover from any of these, and neither can we: the session
# is gone or the credential is wrong. Retrying costs a 5s timeout per attempt
# on the shared executor, once per websocket frame.
PERMANENT_STATUSES = frozenset({401, 403, 404, 410})


def _urllib_post(url: str, token: str, payload: dict) -> None:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS):
        pass


class RelayPublisher:
    """A code sink that forwards to the relay, mirroring RoomCodeServer's shape.

    Not critical: if it cannot start or cannot deliver, the extractor carries on
    and the LAN TCP server keeps serving. Losing the relay must never interrupt
    a game in progress.
    """

    critical = False

    def __init__(
        self,
        base_url: str,
        session_id: str,
        token: str,
        *,
        post=_urllib_post,
        attempts: int = DEFAULT_ATTEMPTS,
        backoff: float = DEFAULT_BACKOFF_SECONDS,
        sleep=asyncio.sleep,
        now=time.time,
    ):
        if not base_url.startswith("https://"):
            raise ValueError(
                "relay base_url must be https:// — TLS is terminated in front "
                "of the relay, so this is the only place it can be enforced"
            )
        if not session_id:
            raise ValueError("relay session_id must not be empty")
        if not token:
            raise ValueError("relay token must not be empty")
        self._url = f"{base_url.rstrip('/')}/sessions/{session_id}/publish"
        self._release_url = f"{base_url.rstrip('/')}/sessions/{session_id}/release"
        self._token = token
        self._post = post
        self._attempts = attempts
        self._backoff = backoff
        self._sleep = sleep
        self._now = now
        self._last_sent: str | None = None
        self._dead = False
        self._released: set[str] = set()

    async def start(self) -> None:
        return None

    async def set_code(self, code: str) -> None:
        """Publish best-effort. Never raises: a dead relay must not disturb a game.

        Mirrors RoomCodeServer.set_code's repeat guard. One session yields the
        same code from several hooks — the create response and websocket_start
        on ecast, one per frame on Blobcast — and each duplicate costs up to
        three attempts x a 5s timeout on the shared executor.
        """
        if self._dead or code == self._last_sent:
            return
        try:
            if await self._deliver(code):
                self._last_sent = code
        except Exception:
            # Backstop so nothing from this sink can escape into mitmproxy.
            # Deliberately does not log the code itself.
            logger.exception("%s publish failed unexpectedly", LOG_MARKER)

    async def release(self, code: str) -> None:
        """Tell the relay the first player joined. One attempt: the hold's timer
        is the fallback. Never raises."""
        if self._dead or code in self._released:
            return
        self._released.add(code)
        try:
            await asyncio.to_thread(
                self._post, self._release_url, self._token, {"code": code}
            )
        except Exception as exc:
            # Deliberately does not log the code itself.
            logger.warning(
                "%s release failed: %s", LOG_MARKER, type(exc).__name__
            )

    async def _deliver(self, code: str) -> bool:
        """True when this code should not be offered again.

        That covers the happy path *and* a code the relay refused outright:
        re-offering a code it called implausible would fail identically, once
        per websocket frame. It deliberately does NOT cover a transient
        failure, so one blip cannot suppress a code for the rest of a session.
        """
        payload = {"code": code, "ts": self._now()}
        delay = self._backoff
        for attempt in range(1, self._attempts + 1):
            try:
                await asyncio.to_thread(self._post, self._url, self._token, payload)
                return True
            except urllib.error.HTTPError as exc:
                if exc.code in PERMANENT_STATUSES:
                    self._dead = True
                    # Deliberately does not log the code itself.
                    logger.warning(
                        "%s the relay session is no longer usable (HTTP %s); "
                        "room codes stay LAN-only for the rest of this game",
                        LOG_MARKER,
                        exc.code,
                    )
                    return False
                if exc.code == 400:
                    # This code was refused, not the session. A different code
                    # may still be fine, so do not latch — but never re-offer
                    # this one.
                    logger.warning(
                        "%s the relay refused a room code (HTTP 400)", LOG_MARKER
                    )
                    return True
                failure = exc  # 429 and 5xx are transient; fall through
            except Exception as exc:
                failure = exc
            if attempt >= self._attempts:
                # Deliberately does not log the code itself.
                logger.warning(
                    "%s publish failed after %d attempts: %s",
                    LOG_MARKER,
                    self._attempts,
                    failure,
                )
                return False
            await self._sleep(delay)
            delay *= 2
        return False
