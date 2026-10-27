"""Host-side relay client: keep one session for the night, watch for a phone,
clear the room.

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
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

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


def clear_room(credentials: SessionCredentials, *, call=_urllib_call) -> bool:
    """Tell the phones this pack is over, keeping the night's session.

    Never raises: this runs on the launcher's exit path, like delete_session.
    """
    try:
        call(
            f"{credentials.base_url}/sessions/{credentials.session_id}/code",
            "DELETE",
            credentials.publisher_token,
        )
    except Exception:
        return False
    return True


def _write_private(path, text: str) -> None:
    """Write `text` to `path` at mode 600, whatever the file was before."""
    handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    # The mode argument above applies only on creation. fchmod the descriptor
    # so a pre-existing file cannot leave the token world-readable, and do it
    # on the fd rather than the path so there is no window to race.
    os.fchmod(handle, 0o600)
    with os.fdopen(handle, "w") as file:
        file.write(text)


def write_session_env(path, credentials: SessionCredentials) -> None:
    """Write the game's credentials at mode 600, for the launcher to source.

    A file rather than arguments: a command line is world-readable through
    `ps`, and this token can publish to the session. Only the *publisher*
    token goes in — the extractor publishes and has no business subscribing.
    """
    _write_private(
        path,
        f"JBVIP_SESSION_ID={shlex.quote(credentials.session_id)}\n"
        f"JBVIP_SESSION_TOKEN={shlex.quote(credentials.publisher_token)}\n",
    )


def credentials_from_env(environ=None) -> SessionCredentials | None:
    """Rebuild credentials the launcher exported. None if any part is missing.

    The subscriber token is not in the environment and is not needed here —
    only `clear_room` uses this, and clearing is a publisher action.
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


def save_night(path, credentials: SessionCredentials) -> None:
    """Remember tonight's session, for the next pack launch to find.

    Unlike the session env file this holds both tokens: the publisher one to
    ask the relay whether the session still lives, the subscriber one for the
    QR when nobody is connected. So it is written at mode 600, in a directory
    created at 700. It lives outside the launcher's per-run state, which is
    deleted on every exit.
    """
    Path(path).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _write_private(
        path,
        json.dumps({
            "base_url": credentials.base_url,
            "session_id": credentials.session_id,
            "publisher_token": credentials.publisher_token,
            "subscriber_token": credentials.subscriber_token,
        }),
    )


def load_night(path) -> SessionCredentials | None:
    """Tonight's saved session, or None when there is no usable one.

    Every way the file can be unusable means the same thing — mint a fresh
    session, which overwrites it — so none of them is an error. Its fields get
    the checks a relay response gets: they are about to go into a URL.
    """
    try:
        with open(path) as file:
            saved = json.load(file)
        return SessionCredentials(
            base_url=_normalise(saved["base_url"]),
            session_id=_checked(saved["session_id"], "session id"),
            publisher_token=_checked(saved["publisher_token"], "publisher token"),
            subscriber_token=_checked(saved["subscriber_token"], "subscriber token"),
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None


@dataclass(frozen=True)
class Night:
    """What a launch found for tonight: the session, how many phones are
    already on it, and whether this launch minted it. A launch may only give
    back a session it minted; a reused one has phones following it."""

    credentials: SessionCredentials
    subscribers: int
    created: bool


def resume_or_create(base_url: str, night_path, *, call=_urllib_call) -> Night:
    """Tonight's saved session if the relay still has it, else a new one.

    Only 404 (the relay restarted, or the night aged out) and 401 (the id is
    there but the token is not ours) say the saved session is gone. Anything
    else — unreachable, a 5xx, a timeout — says nothing about it, so it
    propagates and the file stays: replacing a session the relay merely
    failed to answer for would strand every phone still following it.
    """
    base = _normalise(base_url)
    saved = load_night(night_path)
    if saved is not None and saved.base_url == base:
        try:
            return Night(saved, subscriber_count(saved, call=call), created=False)
        except urllib.error.HTTPError as exc:
            if exc.code not in (401, 404):
                raise
    credentials = create_session(base, call=call)
    # shortcut: if this save raises, the new session is left to expire on the
    # relay (roomless, 2 h) instead of being deleted; delete it here if that
    # is ever seen in practice.
    save_night(night_path, credentials)
    return Night(credentials, 0, created=True)


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
    """`python3 -m host.session --clear`, called from the launcher's cleanup.

    Reads the credentials from the environment rather than arguments, so the
    token never appears in a process listing.

    Every *documented* invocation returns 0, including bare `--clear` with
    nothing to clear: this runs inside the launcher's EXIT trap, where a
    non-zero status would change the exit code the game reported to Steam.
    That contract does not reach an *undocumented* flag — argparse's own
    `parse_args` still raises `SystemExit(2)` on one, and preventing that
    would mean subclassing `ArgumentParser` for no real benefit. The
    launcher's invocation is a fixed string, additionally guarded by
    `|| true`, so that path is never actually hit.
    """
    parser = argparse.ArgumentParser(description="jbvip relay session helper")
    parser.add_argument(
        "--clear", action="store_true",
        help="clear the room of the session named by JBVIP_SESSION_ID and exit",
    )
    args = parser.parse_args(argv)
    if not args.clear:
        print("jbvip: nothing to do; pass --clear", file=sys.stderr)
        return 0
    credentials = credentials_from_env()
    if credentials is not None:
        clear_room(credentials)
    # Always 0. This runs inside the launcher's EXIT trap, where a non-zero
    # status would change the exit code the game reported to Steam. A failed
    # clear costs only that phones keep showing the finished room until the
    # next pack publishes.
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by the launcher
    sys.exit(main())
