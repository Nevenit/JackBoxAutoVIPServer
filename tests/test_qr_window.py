"""The pairing window's glue: which launches get a QR and who gives a session back.

Everything main() collaborates with is replaced, so no Tk window opens and no
request goes out. What is left is main()'s own decisions.
"""
from types import SimpleNamespace

from host import qr_window
from host.session import (
    PAIRING_TIMEOUT_SECONDS,
    REUSED_NIGHT_PAIRING_TIMEOUT_SECONDS,
    Night,
    PairingWaiter,
    SessionCredentials,
)

CREDENTIALS = SessionCredentials(
    base_url="https://relay.example",
    session_id="sess-1",
    publisher_token="pub-token",
    subscriber_token="sub-token",
)
SESSION_ENV = "/run/user/1000/jbvip.x/session.env"
NIGHT_FILE = "/home/me/.local/state/jbvip/night.json"
ARGV = [
    "--base-url", "https://relay.example",
    "--session-env", SESSION_ENV,
    "--night-file", NIGHT_FILE,
]


def arrange(monkeypatch, night, *, env_write_fails=False):
    """Replace main()'s collaborators. The result records what it did with them."""
    did = SimpleNamespace(resumed=[], written=[], shown=[], timeouts=[], deleted=[])

    def resume_or_create(base_url, night_file):
        did.resumed.append((base_url, night_file))
        return night

    def write_session_env(path, credentials):
        if env_write_fails:
            raise OSError("read-only file system")
        did.written.append((path, credentials))

    def show(credentials, *, timeout=PAIRING_TIMEOUT_SECONDS):  # show()'s default
        did.shown.append(credentials)
        did.timeouts.append(timeout)
        return PairingWaiter.PAIRED

    monkeypatch.setattr(qr_window, "resume_or_create", resume_or_create)
    monkeypatch.setattr(qr_window, "write_session_env", write_session_env)
    monkeypatch.setattr(qr_window, "show", show)
    monkeypatch.setattr(qr_window, "delete_session", did.deleted.append)
    return did


def test_the_window_is_shown_when_no_phone_is_connected(monkeypatch):
    did = arrange(monkeypatch, Night(CREDENTIALS, subscribers=0, created=True))
    assert qr_window.main(ARGV) == 0
    assert did.resumed == [("https://relay.example", NIGHT_FILE)]
    assert did.written == [(SESSION_ENV, CREDENTIALS)]
    assert did.shown == [CREDENTIALS]
    assert did.deleted == []


def test_a_new_night_waits_the_full_pairing_time(monkeypatch):
    did = arrange(monkeypatch, Night(CREDENTIALS, subscribers=0, created=True))
    assert qr_window.main(ARGV) == 0
    assert did.timeouts == [PAIRING_TIMEOUT_SECONDS]


def test_a_reused_night_with_no_phone_connected_gives_up_quickly(monkeypatch):
    # Most likely a pack switch with every phone locked: the page holds its
    # stream only while it is on screen. A phone that wakes up follows the
    # session anyway, so the window must not hold the game for long.
    did = arrange(monkeypatch, Night(CREDENTIALS, subscribers=0, created=False))
    assert qr_window.main(ARGV) == 0
    assert did.shown == [CREDENTIALS]
    assert did.timeouts == [REUSED_NIGHT_PAIRING_TIMEOUT_SECONDS] == [15.0]


def test_no_window_when_phones_are_already_connected(monkeypatch, capsys):
    did = arrange(monkeypatch, Night(CREDENTIALS, subscribers=1, created=False))
    assert qr_window.main(ARGV) == 0
    # Asserted on what show() was asked, not on show() raising: main() catches
    # whatever the window throws and exits 0 regardless, so a raising stub
    # would pass even when the window had been opened.
    assert did.shown == []
    assert "1 phone(s) already connected; no QR needed" in capsys.readouterr().err
    # The game still publishes into the session, so it still gets the credentials.
    assert did.written == [(SESSION_ENV, CREDENTIALS)]


def test_a_reused_night_is_not_deleted_when_the_env_write_fails(monkeypatch):
    # Phones may be following it, and the night file still names it.
    night = Night(CREDENTIALS, subscribers=0, created=False)
    did = arrange(monkeypatch, night, env_write_fails=True)
    assert qr_window.main(ARGV) == 1
    assert did.deleted == []
    assert did.shown == []


def test_a_new_night_is_given_back_when_the_env_write_fails(monkeypatch):
    night = Night(CREDENTIALS, subscribers=0, created=True)
    did = arrange(monkeypatch, night, env_write_fails=True)
    assert qr_window.main(ARGV) == 1
    assert did.deleted == [CREDENTIALS]
    assert did.shown == []


def test_a_relay_that_cannot_be_reached_is_exit_one_with_no_credentials(
    monkeypatch, capsys
):
    did = arrange(monkeypatch, None)

    def unreachable(base_url, night_file):
        raise OSError("no route to host")

    monkeypatch.setattr(qr_window, "resume_or_create", unreachable)
    assert qr_window.main(ARGV) == 1
    assert did.written == []
    assert "the game will run LAN-only" in capsys.readouterr().err
