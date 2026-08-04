"""Host-side relay client: create a session, watch for a phone, tidy up.

Stdlib only, with an injected transport and clock, so all of it tests without
a network and without a display. Deliberately imports no GUI toolkit: the
window in host/qr_window.py is glue over this, not the other way round.
"""
import argparse
import json
import os
import shlex
import sys
import time
import urllib.request
from dataclasses import dataclass

REQUEST_TIMEOUT_SECONDS = 5.0
PAIRING_TIMEOUT_SECONDS = 120.0
POLL_SECONDS = 1.0


@dataclass(frozen=True)
class SessionCredentials:
    """One game night's relay identity. `base_url` is stored normalised."""

    base_url: str
    session_id: str
    publisher_token: str
    subscriber_token: str


def _normalise(base_url: str) -> str:
    if not isinstance(base_url, str) or not base_url.startswith("https://"):
        raise ValueError(
            "relay base_url must be https:// — TLS is terminated in front of "
            "the relay, so this is the only place it can be enforced"
        )
    return base_url.rstrip("/")


def _checked(value, what: str) -> str:
    """Refuse anything the relay sends that is not a plain opaque token.

    The relay chooses these strings and the host then interpolates them into
    a URL and writes them into a file the launcher sources. `shlex.quote`
    makes the shell side safe on its own, but a value containing `/`, `?` or
    `#` would still rewrite the publish URL. Both are `secrets.token_urlsafe`
    output, so this rejects nothing legitimate.
    """
    if not isinstance(value, str) or not value:
        raise ValueError(f"relay returned an empty {what}")
    # `.isascii()` first: str.isalnum() is true for plenty of non-ASCII.
    if not value.isascii() or not all(
        character.isalnum() or character in "-_" for character in value
    ):
        raise ValueError(f"relay returned an implausible {what}")
    return value


def _urllib_call(url: str, method: str, token: str | None) -> dict | None:
    """One JSON round trip. Returns None for an empty body (a 204)."""
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
        body = response.read()
    return json.loads(body) if body else None


def create_session(base_url: str, *, call=_urllib_call) -> SessionCredentials:
    """Ask the relay for a session. Raises if it cannot be reached.

    The caller degrades — the launcher logs and starts the game LAN-only. This
    function does not, because "no session" and "a session that silently does
    nothing" are very different things to the layer above.
    """
    base = _normalise(base_url)
    body = call(f"{base}/sessions", "POST", None)
    return SessionCredentials(
        base_url=base,
        session_id=_checked(body["session_id"], "session id"),
        publisher_token=_checked(body["publisher_token"], "publisher token"),
        subscriber_token=_checked(body["subscriber_token"], "subscriber token"),
    )


def qr_payload(credentials: SessionCredentials) -> str:
    """The URL the phone scans.

    The subscriber token sits in the fragment, which browsers never transmit,
    so it cannot reach the relay's access log, a proxy log or a Referer header.
    The publisher token never leaves the host PC.
    """
    return (
        f"{credentials.base_url}/j"
        f"#{credentials.session_id}.{credentials.subscriber_token}"
    )


def subscriber_count(credentials: SessionCredentials, *, call=_urllib_call) -> int:
    body = call(
        f"{credentials.base_url}/sessions/{credentials.session_id}",
        "GET",
        credentials.publisher_token,
    )
    return int(body["subscribers"])


def delete_session(credentials: SessionCredentials, *, call=_urllib_call) -> bool:
    """Free the session's capacity. Never raises: this runs on an exit path."""
    try:
        call(
            f"{credentials.base_url}/sessions/{credentials.session_id}",
            "DELETE",
            credentials.publisher_token,
        )
    except Exception:
        return False
    return True


def write_session_env(path, credentials: SessionCredentials) -> None:
    """Write the game's credentials at mode 600, for the launcher to source.

    A file rather than arguments: a command line is world-readable through
    `ps`, and this token can publish to the session. Only the *publisher*
    token goes in — the extractor publishes and has no business subscribing.
    """
    text = (
        f"JBVIP_SESSION_ID={shlex.quote(credentials.session_id)}\n"
        f"JBVIP_SESSION_TOKEN={shlex.quote(credentials.publisher_token)}\n"
    )
    handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(handle, "w") as file:
        file.write(text)


def credentials_from_env(environ=None) -> SessionCredentials | None:
    """Rebuild credentials the launcher exported. None if any part is missing.

    The subscriber token is not in the environment and is not needed here —
    only `delete_session` uses this, and deleting is a publisher action.
    """
    environ = os.environ if environ is None else environ
    base_url = environ.get("JBVIP_RELAY_URL", "")
    session_id = environ.get("JBVIP_SESSION_ID", "")
    token = environ.get("JBVIP_SESSION_TOKEN", "")
    if not (base_url and session_id and token):
        return None
    try:
        base = _normalise(base_url)
    except ValueError:
        return None
    return SessionCredentials(
        base_url=base,
        session_id=session_id,
        publisher_token=token,
        subscriber_token="",
    )


class PairingWaiter:
    """Poll until a phone subscribes, or give up.

    Owns no clock, no display and no sleep of its own: `now` and `count` are
    injected, so the whole poll-and-timeout behaviour is a unit test.
    """

    WAITING = "waiting"
    PAIRED = "paired"
    TIMED_OUT = "timed-out"

    def __init__(self, count, *, now=time.monotonic,
                 timeout: float = PAIRING_TIMEOUT_SECONDS):
        self._count = count
        self._now = now
        self._deadline = now() + timeout
        self.state = self.WAITING
        self.subscribers = 0
        self.error: str | None = None

    def poll(self) -> str:
        """One tick. WAITING means call again later."""
        if self.state != self.WAITING:
            return self.state
        try:
            self.subscribers = self._count()
        except Exception as exc:
            # A blip must not end the wait; only the deadline does.
            self.error = str(exc)
        else:
            self.error = None
            if self.subscribers >= 1:
                self.state = self.PAIRED
                return self.state
        if self._now() >= self._deadline:
            self.state = self.TIMED_OUT
        return self.state

    def remaining(self) -> float:
        return max(0.0, self._deadline - self._now())


def main(argv=None) -> int:
    """`python3 -m host.session --delete`, called from the launcher's cleanup.

    Reads the credentials from the environment rather than arguments, so the
    token never appears in a process listing.
    """
    parser = argparse.ArgumentParser(description="jbvip relay session helper")
    parser.add_argument(
        "--delete", action="store_true",
        help="delete the session named by JBVIP_SESSION_ID and exit",
    )
    args = parser.parse_args(argv)
    if not args.delete:
        parser.error("nothing to do; pass --delete")
    credentials = credentials_from_env()
    if credentials is not None:
        delete_session(credentials)
    # Always 0. This runs inside the launcher's EXIT trap, where a non-zero
    # status would change the exit code the game reported to Steam. The
    # session self-expires anyway, so a failed delete costs nothing.
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by the launcher
    sys.exit(main())
