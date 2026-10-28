"""The QR card: a white card over the game, top-right, until a phone hides it.

Two modes. Setup (--base-url, --session-env, --night-file) resumes or creates
tonight's relay session and writes the game's credentials; it opens no window.
Card (--card, --night-file) is started in the background beside the game and
puts the QR on the TV.

Deliberately thin. Every decision — whether the card shows, whether it stays,
what the payload looks like — lives in host/session.py, which tests without a
display. This file is Tk plumbing and a few small pure helpers; the window
itself is verified by running it (tests/visual/card_in_kwin.sh).

The card is an unmanaged (override-redirect) window, which KWin leaves alone:
it stays above a fullscreen game at the position it asks for, and never takes
the keyboard from it. A managed window, even a -topmost one, gets centred by
KWin and steals focus, so it could not sit over a game.
"""
import argparse
import base64
import os
import re
import subprocess
import sys
import tkinter as tk

from host.session import (
    CARD_POLL_SECONDS,
    card_should_stay,
    card_wanted,
    delete_session,
    hide_qr,
    load_night,
    qr_payload,
    resume_or_create,
    session_status,
    write_session_env,
)

# The QR's height as a share of the monitor's, and the card's margin from the
# monitor's edges as a share of the monitor's height.
QR_SCREEN_FRACTION = 1 / 3
CARD_MARGIN_FRACTION = 0.03

# One line of `xrandr --listactivemonitors`, from the `*` that marks the
# primary monitor: "*DP-2 3840/699x2160/395+0+900", read as width, height, x, y.
# The millimetres are -1 when the output's physical size is unknown.
_PRIMARY_MONITOR = re.compile(r"\*\S+\s+(\d+)/-?\d+x(\d+)/-?\d+([+-]\d+)([+-]\d+)")


def qr_png(payload: str) -> bytes | None:
    """A PNG of `payload`, one pixel per module. None when qrencode is missing
    or fails.

    Tk 8.6 reads PNG natively and zooms it in whole pixels, so this needs no
    Pillow — one binary dependency instead of a Python one.

    The payload goes in on **stdin**, never as an argument: it carries the
    subscriber token, and an argument would be world-readable in
    /proc/<pid>/cmdline for as long as qrencode ran. The image comes back on
    stdout, so it is never written to disk either.
    """
    try:
        return subprocess.run(
            ["qrencode", "-t", "PNG", "-s", "1", "-m", "2", "-o", "-"],
            input=payload.encode(),
            check=True, capture_output=True, timeout=10,
        ).stdout
    except (subprocess.SubprocessError, OSError):
        return None


def primary_monitor(xrandr_text: str) -> tuple[int, int, int, int] | None:
    """(width, height, x, y) of the `*` monitor in `xrandr --listactivemonitors`
    output, or None when none is marked."""
    found = _PRIMARY_MONITOR.search(xrandr_text)
    return tuple(int(part) for part in found.groups()) if found else None


def card_monitor(
    xrandr_text: str, screen_w: int, screen_h: int
) -> tuple[int, int, int, int]:
    """The monitor the card goes on: the primary one, else the whole X screen.

    XWayland shows every monitor as one X screen, so on a PC with several the
    screen's top-right corner is on the wrong monitor. On a single display the
    two are the same.
    """
    return primary_monitor(xrandr_text) or (screen_w, screen_h, 0, 0)


def card_position(
    monitor: tuple[int, int, int, int], card_w: int, card_h: int
) -> tuple[int, int]:
    """Where the card's top-left goes to sit in the monitor's top-right corner.

    `card_h` is not needed for a card hanging from the top edge; the signature
    is the same for every corner.
    """
    width, height, x, y = monitor
    margin = round(height * CARD_MARGIN_FRACTION)
    return x + width - card_w - margin, y + margin


def qr_zoom(png_side: int, monitor_h: int) -> int:
    """The whole-pixel zoom that makes a `png_side` QR about a third of the
    monitor's height, and never below 1."""
    return max(1, round(monitor_h * QR_SCREEN_FRACTION / png_side))


def launcher_gone(original_ppid: int, *, getppid=os.getppid) -> bool:
    """Has the launcher that started the card exited? An orphan is adopted by
    init or a subreaper, so its parent changes; that covers a crash and a
    SIGKILL, where no cleanup runs."""
    return getppid() != original_ppid


def _active_monitors() -> str:
    """`xrandr --listactivemonitors`, or "" when it can't be had: the card then
    uses the whole X screen."""
    try:
        return subprocess.run(
            ["xrandr", "--listactivemonitors"],
            capture_output=True, text=True, timeout=3,
        ).stdout
    except Exception:
        return ""


def show_card(credentials, *, keeps_showing=None, hide=None) -> None:
    """Put the QR card on the TV and keep it there until it should go.

    It goes when `keeps_showing()` says no (a phone hid the QR, or the night is
    gone), when the launcher is gone, or when × is clicked, which first tells
    the relay to hide the QR for the night. Raises only when Tk itself fails;
    that is the caller's problem to log.

    Nothing here takes the keyboard: no focus_force, and × is a Label, not a
    Button, so there is nothing on the card to tab to.
    """
    keeps_showing = keeps_showing or (lambda: card_should_stay(credentials))
    hide = hide or hide_qr
    # shortcut: taken after run_card's status call (up to 5 s), so a launcher
    # that died during it goes unnoticed; pass the pid in from run_card if that
    # is ever seen.
    launcher = os.getppid()

    root = tk.Tk()
    root.overrideredirect(True)
    monitor = card_monitor(
        _active_monitors(), root.winfo_screenwidth(), root.winfo_screenheight()
    )
    monitor_h = monitor[1]
    pad = round(monitor_h * 0.02)
    payload = qr_payload(credentials)

    card = tk.Frame(root, bg="white", padx=pad, pady=pad)
    card.pack()
    png = qr_png(payload)
    if png is not None:
        data = base64.b64encode(png).decode("ascii")
        image = tk.PhotoImage(data=data, format="png")
        image = image.zoom(qr_zoom(image.width(), monitor_h))
        picture = tk.Label(card, image=image, bg="white")
        # Tk keeps no reference of its own: the image is garbage collected out
        # from under the label otherwise, leaving it blank.
        picture.image = image
        picture.pack()
    else:
        # Worse, but not broken: the room can still type it in.
        tk.Label(
            card, text=payload, bg="white", justify="center",
            wraplength=round(monitor_h * QR_SCREEN_FRACTION),
            font=("monospace", -round(monitor_h * 0.018)),
        ).pack(pady=(pad, 0))  # clear of the × in the corner
    tk.Label(
        card, text="Scan to join", bg="white", fg="black",
        font=("sans", -round(monitor_h * 0.03), "bold"),
    ).pack(pady=(pad // 2, 0))
    tk.Label(
        card, text="Hide it from ⋯ on your phone", bg="white", fg="#555555",
        font=("sans", -round(monitor_h * 0.022)),
    ).pack()

    # shortcut: keeps_showing and hide block the Tk loop for up to a request
    # timeout (5 s). The card is static, so nothing visibly stops; use a thread
    # if it ever animates.
    def close(_event):
        hide(credentials)  # whatever it answers: this card is done
        root.destroy()

    # A child of the root, not the padded frame: `place` measures from the
    # frame's padding, which would put the × against the QR's corner.
    cross = tk.Label(
        root, text="×", bg="white", fg="#888888",
        font=("sans", -round(monitor_h * 0.03)), cursor="hand2",
    )
    cross.bind("<Button-1>", close)
    cross.place(relx=1.0, x=-(pad // 2), anchor="ne")

    root.update_idletasks()
    x, y = card_position(monitor, root.winfo_reqwidth(), root.winfo_reqheight())
    root.geometry(f"+{x}+{y}")

    interval = round(CARD_POLL_SECONDS * 1000)

    def poll():
        if launcher_gone(launcher) or not keeps_showing():
            root.destroy()
        else:
            root.after(interval, poll)

    root.after(interval, poll)
    root.mainloop()


def run_card(night_file) -> int:
    """Card mode. Always 0: the game does not wait for this, and a relay or
    display problem costs only the card."""
    credentials = load_night(night_file)
    if credentials is None:
        return 0
    try:
        if card_wanted(credentials, session_status(credentials)):
            show_card(credentials)
    except Exception as exc:
        # tk.TclError (no display) and an unreachable relay are the expected
        # cases; neither should reach the user as a traceback.
        print(f"jbvip: no QR card ({exc})", file=sys.stderr)
    return 0


def main(argv=None) -> int:
    """Setup: `python3 -m host.qr_window --base-url <URL> --session-env <PATH>
    --night-file <PATH>`. Card: `python3 -m host.qr_window --card
    --night-file <PATH>`.

    Setup exits non-zero only when the game will not have working relay
    credentials on disk: the night's session could not be resumed or created,
    or it could but write_session_env could not put its credentials where the
    launcher reads them. It opens no window, so nothing after those
    credentials are written can turn the exit code non-zero again.

    The card always exits 0: see run_card.
    """
    parser = argparse.ArgumentParser(
        description="Resume tonight's relay session, or create one, and write "
                    "the game's credentials; or, with --card, show the QR "
                    "card over the game"
    )
    parser.add_argument(
        "--card", action="store_true",
        help="show the QR card (needs only --night-file)",
    )
    parser.add_argument("--base-url", help="https:// relay URL (setup)")
    parser.add_argument(
        "--session-env",
        help="path to write JBVIP_SESSION_ID/JBVIP_SESSION_TOKEN into (setup)",
    )
    parser.add_argument(
        "--night-file", required=True,
        help="path where tonight's session is kept between pack launches",
    )
    args = parser.parse_args(argv)
    if args.card:
        return run_card(args.night_file)
    if not (args.base_url and args.session_env):
        parser.error("setup needs --base-url and --session-env")

    try:
        night = resume_or_create(args.base_url, args.night_file)
    except Exception as exc:
        # A dead VPS must never cost a game night.
        print(
            f"jbvip: no relay session ({exc}); the game will run LAN-only",
            file=sys.stderr,
        )
        return 1

    try:
        write_session_env(args.session_env, night.credentials)
    except OSError as exc:
        # A session minted just now is live but the game will never see its
        # credentials, so it would hold a slot on the relay until it had gone
        # 2 hours without a room (the relay's ROOMLESS_SECONDS). Give it back
        # now; the night file still names it, so a later launch's check gets
        # a 404 and replaces it. A reused one is tonight's: phones may be
        # following it, and the night file still names it, so it stays.
        if night.created:
            delete_session(night.credentials)
        print(
            f"jbvip: could not write {args.session_env} ({exc}); "
            "the game will run LAN-only",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - a window, verified by hand
    sys.exit(main())
