"""Host-side relay client: keep one session for the night, decide when the QR
card shows and stays, clear the room.

Stdlib only, with an injected transport, so all of it tests without a network
and without a display. Deliberately imports no GUI toolkit: the card in
host/qr_window.py is glue over this, not the other way round.
"""
import argparse
import hashlib
import json
import math
import os
import shlex
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

REQUEST_TIMEOUT_SECONDS = 5.0
# How often an open QR card asks the relay whether it should still be up.
CARD_POLL_SECONDS = 2.0


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


def _urllib_call(
    url: str, method: str, token: str | None, body: dict | None = None
) -> dict | None:
    """One JSON round trip. Returns None for an empty body (a 204)."""
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
        reply = response.read()
    return json.loads(reply) if reply else None


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


@dataclass(frozen=True)
class SessionStatus:
    """What the relay says about tonight's session right now."""

    subscribers: int
    qr_hidden: bool


def session_status(
    credentials: SessionCredentials, *, call=_urllib_call
) -> SessionStatus:
    body = call(
        f"{credentials.base_url}/sessions/{credentials.session_id}",
        "GET",
        credentials.publisher_token,
    )
    # No flag (an older relay) means nobody can have hidden the QR.
    return SessionStatus(int(body["subscribers"]), bool(body.get("qr_hidden", False)))


def subscriber_count(credentials: SessionCredentials, *, call=_urllib_call) -> int:
    return session_status(credentials, call=call).subscribers


def _quietly(credentials: SessionCredentials, method: str, suffix: str, call) -> bool:
    """`method` under the session with the publisher token. Never raises.

    Every caller is tidying up on an exit path or doing something the game can
    do without, and a relay that is down must not get in the game's way.
    """
    try:
        call(
            f"{credentials.base_url}/sessions/{credentials.session_id}{suffix}",
            method,
            credentials.publisher_token,
        )
    except Exception:
        return False
    return True


ADMIN_HEAD_START_DEFAULT = 0.5


def admin_key(password: str) -> str:
    """The relay never sees the password, only this."""
    return hashlib.sha256(("jbvip-admin:" + password).encode("utf-8")).hexdigest()


def admin_head_start(raw: str | None) -> float:
    """Seconds admins get each new room before guests; the relay clamps it."""
    if not raw:
        return ADMIN_HEAD_START_DEFAULT
    try:
        value = float(raw)
        if math.isfinite(value):
            return value
    except ValueError:
        pass
    print(
        f"jbvip: warning: JBVIP_ADMIN_HEAD_START is not a number; using "
        f"{ADMIN_HEAD_START_DEFAULT}",
        file=sys.stderr,
    )
    return ADMIN_HEAD_START_DEFAULT


def register_admin(
    credentials: SessionCredentials, key: str, head_start: float, *, call=_urllib_call
) -> bool:
    """Give the relay tonight's admin key. Never raises."""
    try:
        call(
            f"{credentials.base_url}/sessions/{credentials.session_id}/admin",
            "PUT",
            credentials.publisher_token,
            {"key": key, "head_start": head_start},
        )
    except Exception:
        return False
    return True


def delete_session(credentials: SessionCredentials, *, call=_urllib_call) -> bool:
    """Free the session's capacity. Never raises."""
    return _quietly(credentials, "DELETE", "", call)


def clear_room(credentials: SessionCredentials, *, call=_urllib_call) -> bool:
    """Tell the phones this pack is over, keeping the night's session.

    Never raises, like delete_session.
    """
    return _quietly(credentials, "DELETE", "/code", call)


def hide_qr(credentials: SessionCredentials, *, call=_urllib_call) -> bool:
    """Hide the TV's QR card for the rest of the night. Never raises."""
    return _quietly(credentials, "POST", "/qr/hide", call)


def show_qr(credentials: SessionCredentials, *, call=_urllib_call) -> bool:
    """Bring the TV's QR card back. Never raises; True means the relay agreed."""
    return _quietly(credentials, "POST", "/qr/show", call)


def card_wanted(
    credentials: SessionCredentials, status: SessionStatus, *, show=show_qr
) -> bool:
    """Should this launch put the QR card up?

    Yes while nobody has hidden the QR. A hidden QR comes back when no phone is
    connected, since then nobody is following the night. If the relay won't
    show it again there is no card: it would close on its first poll, finding
    the QR still hidden.
    """
    if not status.qr_hidden:
        return True
    if status.subscribers == 0:
        return show(credentials)
    return False


def card_should_stay(credentials: SessionCredentials, *, call=_urllib_call) -> bool:
    """One poll of an open card: should it stay up?

    It closes when a phone hides the QR, or when the night is gone (404, or 401
    for a token that is not ours). Anything else (unreachable, a 5xx, a garbled
    body) says nothing about the QR, so the card stays: a blip must never take
    it off the TV.
    """
    try:
        return not session_status(credentials, call=call).qr_hidden
    except urllib.error.HTTPError as exc:
        return exc.code not in (401, 404)
    except Exception:
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
