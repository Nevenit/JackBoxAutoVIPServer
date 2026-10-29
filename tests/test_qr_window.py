"""The QR card's glue: setup mode, card mode, and the pure helpers around the window.

Everything main() collaborates with is replaced, so no Tk window opens and no
request goes out. What is left is main()'s own decisions and the helpers that
need no display.
"""
import subprocess
import tkinter as tk
import urllib.error
from types import SimpleNamespace

import pytest

from host import qr_window
from host.session import Night, SessionCredentials, SessionStatus

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
CARD_ARGV = ["--card", "--night-file", NIGHT_FILE]
STATUS = SessionStatus(subscribers=2, qr_hidden=False)

# `xrandr --listactivemonitors` on the dev PC: three monitors in one X screen,
# the primary one at the left.
SAMPLE = (
    "Monitors: 3\n"
    " 0: +*DP-2 3840/699x2160/395+0+900  DP-2\n"
    " 1: +DP-1 2880/518x1800/324+3840+0  DP-1\n"
    " 2: +HDMI-A-2 2880/518x1800/324+3840+1800  HDMI-A-2\n"
)


# --- setup mode -------------------------------------------------------------

def arrange(monkeypatch, night, *, env_write_fails=False):
    """Replace setup mode's collaborators. The result records what it did with them."""
    did = SimpleNamespace(resumed=[], written=[], cards=[], statuses=[], deleted=[])

    def resume_or_create(base_url, night_file):
        did.resumed.append((base_url, night_file))
        return night

    def write_session_env(path, credentials):
        if env_write_fails:
            raise OSError("read-only file system")
        did.written.append((path, credentials))

    monkeypatch.setattr(qr_window, "resume_or_create", resume_or_create)
    monkeypatch.setattr(qr_window, "write_session_env", write_session_env)
    # Recorded, not made to raise: main() would swallow a raise from the card,
    # so only an empty list proves setup mode never opened a window.
    monkeypatch.setattr(qr_window, "show_card", did.cards.append)
    monkeypatch.setattr(qr_window, "session_status", did.statuses.append)
    monkeypatch.setattr(qr_window, "delete_session", did.deleted.append)
    did.registered = []
    did.register_result = True

    def register_admin(credentials, key, head_start):
        did.registered.append((credentials, key, head_start))
        return did.register_result

    monkeypatch.setattr(qr_window, "register_admin", register_admin)
    monkeypatch.delenv("JBVIP_ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("JBVIP_ADMIN_HEAD_START", raising=False)
    return did


@pytest.mark.parametrize("created", [True, False], ids=["new-night", "reused-night"])
@pytest.mark.parametrize("subscribers", [0, 3], ids=["no-phone", "phones"])
def test_setup_writes_the_session_env_and_opens_no_window(
    monkeypatch, created, subscribers
):
    did = arrange(monkeypatch, Night(CREDENTIALS, subscribers, created=created))
    assert qr_window.main(ARGV) == 0
    assert did.resumed == [("https://relay.example", NIGHT_FILE)]
    assert did.written == [(SESSION_ENV, CREDENTIALS)]
    assert did.cards == []
    assert did.statuses == []
    assert did.deleted == []


@pytest.mark.parametrize("argv", [
    ["--night-file", NIGHT_FILE],
    ["--base-url", "https://relay.example", "--night-file", NIGHT_FILE],
    ["--session-env", SESSION_ENV, "--night-file", NIGHT_FILE],
], ids=["neither", "no-session-env", "no-base-url"])
def test_setup_mode_still_needs_its_arguments(monkeypatch, argv):
    arrange(monkeypatch, None)
    with pytest.raises(SystemExit) as stop:
        qr_window.main(argv)
    assert stop.value.code == 2


def test_a_reused_night_is_not_deleted_when_the_env_write_fails(monkeypatch):
    # Phones may be following it, and the night file still names it.
    night = Night(CREDENTIALS, subscribers=0, created=False)
    did = arrange(monkeypatch, night, env_write_fails=True)
    assert qr_window.main(ARGV) == 1
    assert did.deleted == []
    assert did.cards == []


def test_a_new_night_is_given_back_when_the_env_write_fails(monkeypatch):
    night = Night(CREDENTIALS, subscribers=0, created=True)
    did = arrange(monkeypatch, night, env_write_fails=True)
    assert qr_window.main(ARGV) == 1
    assert did.deleted == [CREDENTIALS]
    assert did.cards == []


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


# --- card mode --------------------------------------------------------------

def arrange_card(monkeypatch, *, night=CREDENTIALS, status=STATUS, wanted=True,
                 window_error=None):
    """Replace card mode's collaborators. An exception given as `status` is
    raised by session_status, one given as `window_error` by show_card."""
    did = SimpleNamespace(
        loaded=[], statuses=[], asked=[], cards=[], launchers=[], setup=[]
    )

    def load_night(path):
        did.loaded.append(path)
        return night

    def session_status(credentials):
        did.statuses.append(credentials)
        if isinstance(status, BaseException):
            raise status
        return status

    def card_wanted(credentials, found):
        did.asked.append((credentials, found))
        return wanted

    def show_card(credentials, *, launcher=None):
        did.cards.append(credentials)
        did.launchers.append(launcher)
        if window_error is not None:
            raise window_error

    monkeypatch.setattr(qr_window, "load_night", load_night)
    monkeypatch.setattr(qr_window, "session_status", session_status)
    monkeypatch.setattr(qr_window, "card_wanted", card_wanted)
    monkeypatch.setattr(qr_window, "show_card", show_card)
    # Card mode must never reach setup mode's collaborators.
    monkeypatch.setattr(qr_window, "resume_or_create", did.setup.append)
    monkeypatch.setattr(qr_window, "write_session_env", did.setup.append)
    return did


def test_card_mode_needs_only_the_night_file(monkeypatch):
    did = arrange_card(monkeypatch)
    assert qr_window.main(["--card", "--night-file", NIGHT_FILE]) == 0
    assert did.loaded == [NIGHT_FILE]
    assert did.setup == []


def test_card_mode_with_no_usable_night_does_nothing(monkeypatch):
    did = arrange_card(monkeypatch, night=None)
    assert qr_window.main(CARD_ARGV) == 0
    assert did.statuses == []
    assert did.asked == []
    assert did.cards == []


def test_card_mode_shows_the_card_when_it_is_wanted(monkeypatch):
    did = arrange_card(monkeypatch, wanted=True)
    assert qr_window.main(CARD_ARGV) == 0
    # The decision is made on the status the relay just gave.
    assert did.asked == [(CREDENTIALS, STATUS)]
    assert did.cards == [CREDENTIALS]


def test_card_mode_shows_nothing_when_it_is_not_wanted(monkeypatch):
    did = arrange_card(monkeypatch, wanted=False)
    assert qr_window.main(CARD_ARGV) == 0
    assert did.asked == [(CREDENTIALS, STATUS)]
    assert did.cards == []


def test_card_mode_survives_an_unreachable_relay(monkeypatch):
    did = arrange_card(monkeypatch, status=urllib.error.URLError("down"))
    assert qr_window.main(CARD_ARGV) == 0
    assert did.statuses == [CREDENTIALS]
    assert did.asked == []
    assert did.cards == []


def test_card_mode_survives_a_broken_display(monkeypatch, capsys):
    did = arrange_card(monkeypatch, window_error=tk.TclError("no display"))
    assert qr_window.main(CARD_ARGV) == 0
    assert did.cards == [CREDENTIALS]
    err = capsys.readouterr().err
    assert "no display" in err
    assert "Traceback" not in err


def test_card_mode_reads_the_launchers_pid_before_asking_the_relay(monkeypatch):
    # The status call can take seconds. A launcher killed without its cleanup in
    # that window goes unnoticed if its pid is only read afterwards: the card is
    # then adopted by init and compares against init's pid.
    did = arrange_card(monkeypatch)
    order = []
    pids = iter(range(4000, 4010))  # a new answer on every call

    def getppid():
        pid = next(pids)
        order.append(("getppid", pid))
        return pid

    asked_the_relay = qr_window.session_status

    def session_status(credentials):
        order.append(("status", None))
        return asked_the_relay(credentials)

    monkeypatch.setattr(qr_window, "os", SimpleNamespace(getppid=getppid))
    monkeypatch.setattr(qr_window, "session_status", session_status)
    assert qr_window.main(CARD_ARGV) == 0
    assert order == [("getppid", 4000), ("status", None)]
    assert did.launchers == [4000]


# --- where the card goes and how big it is ------------------------------------

def test_the_primary_monitor_is_the_one_marked_with_a_star():
    assert qr_window.primary_monitor(SAMPLE) == (3840, 2160, 0, 900)


def test_a_monitor_left_of_the_origin_keeps_its_negative_offset():
    text = "Monitors: 1\n 0: +*DP-1 1920/527x1080/296-1920+0  DP-1\n"
    assert qr_window.primary_monitor(text) == (1920, 1080, -1920, 0)


def test_a_monitor_of_unknown_physical_size_is_still_found():
    # What XWayland reports for an output whose size nobody knows, as KWin's
    # virtual output does: -1 where the millimetres go.
    text = "Monitors: 1\n 0: +*Virtual-0 1920/-1x1080/-1+0+0  Virtual-0\n"
    assert qr_window.primary_monitor(text) == (1920, 1080, 0, 0)


def test_no_primary_monitor_is_none():
    assert qr_window.primary_monitor(SAMPLE.replace("*", "")) is None


def test_no_xrandr_output_is_no_primary_monitor():
    assert qr_window.primary_monitor("") is None


def test_with_no_primary_monitor_the_card_uses_the_whole_x_screen():
    assert qr_window.card_monitor("", 1920, 1080) == (1920, 1080, 0, 0)
    assert qr_window.card_monitor(SAMPLE, 6720, 3600) == (3840, 2160, 0, 900)


def test_the_card_sits_in_the_top_right_corner_with_a_margin():
    # Margin: 3% of 2160, rounded, is 65.
    assert qr_window.card_position((3840, 2160, 0, 900), 800, 900) == (2975, 965)


def test_the_qr_is_about_a_third_of_the_monitor_height():
    assert qr_window.qr_zoom(45, 2160) == 16
    assert qr_window.qr_zoom(45, 1080) == 8


def test_the_qr_is_never_zoomed_out_of_existence():
    assert qr_window.qr_zoom(500, 300) == 1


def test_the_launcher_is_gone_once_the_card_is_adopted_by_someone_else():
    assert qr_window.launcher_gone(41, getppid=lambda: 41) is False
    assert qr_window.launcher_gone(41, getppid=lambda: 1) is True


# --- the QR image -------------------------------------------------------------

def test_qr_png_sends_the_payload_on_stdin_not_the_command_line(monkeypatch):
    payload = "https://relay.example/j#sess-1.sub-token"
    ran = {}

    def run(args, **kwargs):
        ran["args"] = args
        ran["input"] = kwargs.get("input")
        return SimpleNamespace(stdout=b"\x89PNG-ish bytes")

    monkeypatch.setattr(qr_window.subprocess, "run", run)
    assert qr_window.qr_png(payload) == b"\x89PNG-ish bytes"
    # A command line is world-readable through /proc, and this is a credential.
    assert not any(payload in part or "sub-token" in part for part in ran["args"])
    assert ran["input"] == payload.encode()
    # The image comes back on stdout, so it is never written to disk.
    output = ran["args"].index("-o")
    assert ran["args"][output:output + 2] == ["-o", "-"]


def test_qr_png_without_qrencode_is_none(monkeypatch):
    def missing(args, **kwargs):
        raise FileNotFoundError("qrencode")

    monkeypatch.setattr(qr_window.subprocess, "run", missing)
    assert qr_window.qr_png("https://relay.example/j#x") is None


def test_qr_png_is_none_when_qrencode_fails(monkeypatch):
    def failing(args, **kwargs):
        raise subprocess.CalledProcessError(1, args)

    monkeypatch.setattr(qr_window.subprocess, "run", failing)
    assert qr_window.qr_png("https://relay.example/j#x") is None


def test_setup_registers_the_admin_key_when_a_password_is_set(monkeypatch):
    did = arrange(monkeypatch, Night(CREDENTIALS, 0, created=True))
    monkeypatch.setenv("JBVIP_ADMIN_PASSWORD", "hunter2")
    monkeypatch.setenv("JBVIP_ADMIN_HEAD_START", "1.5")
    assert qr_window.main(ARGV) == 0
    assert did.registered == [(CREDENTIALS, qr_window.admin_key("hunter2"), 1.5)]


def test_setup_registers_nothing_without_a_password(monkeypatch):
    did = arrange(monkeypatch, Night(CREDENTIALS, 0, created=True))
    assert qr_window.main(ARGV) == 0
    assert did.registered == []


def test_a_failed_registration_keeps_exit_zero_and_prints_no_key(monkeypatch, capsys):
    did = arrange(monkeypatch, Night(CREDENTIALS, 0, created=True))
    did.register_result = False
    monkeypatch.setenv("JBVIP_ADMIN_PASSWORD", "hunter2")
    assert qr_window.main(ARGV) == 0
    err = capsys.readouterr().err
    assert "admin" in err
    assert "hunter2" not in err and qr_window.admin_key("hunter2") not in err
