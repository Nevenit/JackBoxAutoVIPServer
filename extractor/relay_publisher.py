"""Forward room codes to the relay. Best-effort: never raises into mitmproxy.

Uses stdlib urllib on a worker thread so the extractor picks up no new runtime
dependency — it runs inside mitmdump on the host at game-launch time.
"""
import asyncio
import json
import logging
import time
import urllib.request

logger = logging.getLogger(__name__)

DEFAULT_ATTEMPTS = 3
DEFAULT_BACKOFF_SECONDS = 0.5
REQUEST_TIMEOUT_SECONDS = 5.0


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
        if not token:
            raise ValueError("relay token must not be empty")
        self._url = base_url.rstrip("/") + "/publish"
        self._token = token
        self._post = post
        self._attempts = attempts
        self._backoff = backoff
        self._sleep = sleep
        self._now = now
        self._last_sent: str | None = None

    async def start(self) -> None:
        return None

    async def set_code(self, code: str) -> None:
        """Publish best-effort. Never raises: a dead relay must not disturb a game.

        Mirrors RoomCodeServer.set_code's repeat guard. One session yields the
        same code from several hooks — the create response and websocket_start
        on ecast, one per frame on Blobcast — and each duplicate costs up to
        three attempts x a 5s timeout on the shared executor, plus one of the
        relay's 60/min publishes. Only a *delivered* code counts as sent, so a
        transient outage cannot suppress that code for the rest of the session.
        """
        if code == self._last_sent:
            return
        try:
            if await self._publish(code):
                self._last_sent = code
        except Exception:
            # Backstop so nothing from this sink can escape into mitmproxy.
            # Deliberately does not log the code itself.
            logger.exception("relay publish failed unexpectedly")

    async def _publish(self, code: str) -> bool:
        """True once the relay has accepted the code, False once attempts run out."""
        payload = {"code": code, "ts": self._now()}
        delay = self._backoff
        for attempt in range(1, self._attempts + 1):
            try:
                await asyncio.to_thread(self._post, self._url, self._token, payload)
                return True
            except Exception as exc:
                if attempt >= self._attempts:
                    # Deliberately does not log the code itself.
                    logger.warning(
                        "relay publish failed after %d attempts: %s",
                        self._attempts,
                        exc,
                    )
                    return False
                await self._sleep(delay)
                delay *= 2
        return False
