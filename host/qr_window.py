"""The pairing window: show a QR, wait for a phone, get out of the way.

Deliberately thin. Every decision — when to stop waiting, what the payload
looks like, what a failed poll means — lives in host/session.py, which tests
without a display. This file is Tk plumbing and is verified by running it.

Shown before the game launches rather than over it: fighting a fullscreen
game for stacking order under Wayland is unreliable, and the launcher already
has a natural gap here while mitmproxy starts.
"""
import argparse
import shutil
import subprocess
import sys
import tempfile
import tkinter as tk
from pathlib import Path

from host.session import (
    PAIRING_TIMEOUT_SECONDS,
    POLL_SECONDS,
    PairingWaiter,
    create_session,
    delete_session,
    qr_payload,
    subscriber_count,
    write_session_env,
)

QR_MODULE_PIXELS = 8
QR_MARGIN_MODULES = 2


def render_qr_png(payload: str, path) -> bool:
    """Write a PNG of `payload` to `path`. False when qrencode is unavailable.

    Tk 8.6 reads PNG natively, so this needs no Pillow — one binary
    dependency instead of a Python one.

    The payload goes in on **stdin**, never as an argument: it carries the
    subscriber token, and an argument would be world-readable in
    /proc/<pid>/cmdline for as long as qrencode ran. qrencode reads standard
    input when given no string operand.
    """
    if shutil.which("qrencode") is None:
        return False
    try:
        subprocess.run(
            [
                "qrencode", "-t", "PNG",
                "-s", str(QR_MODULE_PIXELS),
                "-m", str(QR_MARGIN_MODULES),
                "-o", str(path),
            ],
            input=payload.encode(),
            check=True, capture_output=True, timeout=10,
        )
    except (subprocess.SubprocessError, OSError):
        return False
    return True


def show(credentials, *, count=None, timeout=PAIRING_TIMEOUT_SECONDS) -> str:
    """Display the QR until a phone pairs, the user skips, or time runs out.

    Returns the PairingWaiter state it finished in. Never raises for a
    pairing failure — only Tk itself can fail here, and that is the caller's
    problem to log.
    """
    counter = count or (lambda: subscriber_count(credentials))
    waiter = PairingWaiter(counter, timeout=timeout)
    payload = qr_payload(credentials)

    root = tk.Tk()
    root.title("jbvip — scan to pair your phone")
    root.attributes("-topmost", True)
    frame = tk.Frame(root, padx=24, pady=24)
    frame.pack()

    # The PNG *is* the subscriber credential in visual form.
    # TemporaryDirectory creates it at mode 0700 and removes the whole tree
    # on the way out, so it is never readable by another user and never
    # outlives the window.
    with tempfile.TemporaryDirectory(prefix="jbvip-qr.") as workspace:
        image = None
        png = Path(workspace) / "pair.png"
        if render_qr_png(payload, png):
            image = tk.PhotoImage(file=str(png))
            tk.Label(frame, image=image).pack()
        else:
            # Worse, but not broken: the user can still type it in.
            tk.Label(
                frame, text="qrencode is not installed — open this URL:",
                font=("TkDefaultFont", 10),
            ).pack()
            entry = tk.Entry(frame, width=52, justify="center")
            entry.insert(0, payload)
            entry.configure(state="readonly")
            entry.pack(pady=(4, 0))

        status = tk.Label(frame, text="", font=("TkDefaultFont", 11))
        status.pack(pady=(12, 8))
        tk.Button(frame, text="Skip", width=12, command=root.destroy).pack()

        def tick():
            state = waiter.poll()
            if state == PairingWaiter.WAITING:
                seconds = int(waiter.remaining())
                trouble = " (relay not responding)" if waiter.error else ""
                status.configure(
                    text=f"Waiting for a phone… {seconds}s{trouble}"
                )
                root.after(int(POLL_SECONDS * 1000), tick)
                return
            root.destroy()

        root.protocol("WM_DELETE_WINDOW", root.destroy)
        root.after(0, tick)
        # `image` must stay referenced for as long as the window lives: Tk
        # does not hold a reference and the PNG is garbage collected out from
        # under it otherwise, leaving a blank label.
        root.image = image
        root.mainloop()

    return waiter.state


def main(argv=None) -> int:
    """`python3 -m host.qr_window --base-url <URL> --session-env <PATH>`.

    Exits non-zero only when the game will not have working relay
    credentials on disk: the session could not be created, or it was created
    but write_session_env could not put its credentials where the launcher
    reads them. Once those credentials are written, nothing that happens
    next can turn the exit code non-zero again — paired, skipped, timed out,
    no display, or the pairing window itself blowing up (a full /tmp, a
    dead Tk) are all exit 0, because the session is live and publishing will
    work regardless of whether anyone ever saw the QR code.
    """
    parser = argparse.ArgumentParser(
        description="Create a relay session and show its pairing QR code"
    )
    parser.add_argument("--base-url", required=True, help="https:// relay URL")
    parser.add_argument(
        "--session-env", required=True,
        help="path to write JBVIP_SESSION_ID/JBVIP_SESSION_TOKEN into",
    )
    args = parser.parse_args(argv)

    try:
        credentials = create_session(args.base_url)
    except Exception as exc:
        # A dead VPS must never cost a game night.
        print(
            f"jbvip: no relay session ({exc}); the game will run LAN-only",
            file=sys.stderr,
        )
        return 1

    try:
        write_session_env(args.session_env, credentials)
    except OSError as exc:
        # The session is live but the game will never see its credentials, so
        # it would sit on the relay until it idled out. Give it back rather
        # than leak a slot for four hours.
        delete_session(credentials)
        print(
            f"jbvip: could not write {args.session_env} ({exc}); "
            "the game will run LAN-only",
            file=sys.stderr,
        )
        return 1

    try:
        state = show(credentials)
    except Exception as exc:
        # The credentials are already on disk, so the game will publish
        # normally — only the pairing window failed. A cosmetic failure must
        # never cost a game night, so this is exit 0, exactly like Skip or a
        # timeout. tk.TclError is the expected case (no display), but
        # TemporaryDirectory raises plain OSError on a full /tmp, and neither
        # should reach the user as a traceback.
        print(f"jbvip: could not show the pairing window ({exc})", file=sys.stderr)
        return 0
    if state != PairingWaiter.PAIRED:
        print(f"jbvip: no phone paired ({state}); starting anyway", file=sys.stderr)
    return 0


if __name__ == "__main__":  # pragma: no cover - a window, verified by hand
    sys.exit(main())
