#!/usr/bin/env bash
# Manual check, not part of pytest: the real QR card over a fake fullscreen
# "game", inside a headless nested KWin, so nothing appears on the real desktop.
#
#   bash tests/visual/card_in_kwin.sh OUT.png
#
# Writes a screenshot of the nested screen to OUT.png and prints which window is
# active there. Expect the white card in the top-right corner with a margin, and
# the fake game still the active window and still _NET_WM_STATE_FULLSCREEN: the
# card sits over the game without taking its keyboard.
set -euo pipefail

SELF="$(readlink -f "${BASH_SOURCE[0]}")"

if [ -z "${CARD_IN_KWIN_INNER:-}" ]; then
  # --- Outside: start the nested KWin, which runs this script inside itself.
  [ $# -eq 1 ] || { echo "usage: bash $0 OUT.png" >&2; exit 2; }
  # KWin starts this script itself, and waits forever if it can't.
  [ -x "$SELF" ] || { echo "$SELF must be executable (chmod +x)" >&2; exit 2; }
  OUT="$(realpath -m "$1")"
  mkdir -p "$(dirname "$OUT")"
  WORK="$(mktemp -d)"
  trap 'rm -rf "$WORK"' EXIT
  mkdir -p "$WORK/config" "$WORK/cache" "$WORK/state"
  # KWin, spectacle and fontconfig keep config and caches here, so none of it
  # reaches the real ~/.config or ~/.cache.
  export XDG_CONFIG_HOME="$WORK/config" XDG_CACHE_HOME="$WORK/cache" XDG_STATE_HOME="$WORK/state"
  # Whatever this shell was pointed at, the real desktop included, the inner
  # session must get its displays from the nested KWin or have none.
  unset DISPLAY WAYLAND_DISPLAY
  export CARD_IN_KWIN_INNER=1 CARD_IN_KWIN_OUT="$OUT"
  export CARD_IN_KWIN_REPO="$(cd "$(dirname "$SELF")/../.." && pwd)"
  export CARD_IN_KWIN_SOCKET="jbvip-card-$$"
  # The timeout wraps KWin itself: wrapped around dbus-run-session it would
  # leave KWin running when it fired.
  dbus-run-session -- timeout --kill-after=5 120 kwin_wayland --virtual --xwayland \
    --no-lockscreen --no-global-shortcuts --width 1920 --height 1080 \
    --socket "$CARD_IN_KWIN_SOCKET" --exit-with-session "$SELF"
  [ -s "$OUT" ] || { echo "no screenshot was written" >&2; exit 1; }
  echo "screenshot: $OUT"
  exit 0
fi

# --- Inside the nested KWin.
cd "$CARD_IN_KWIN_REPO"
[ "${WAYLAND_DISPLAY:-}" = "$CARD_IN_KWIN_SOCKET" ] && [ -n "${DISPLAY:-}" ] || {
  echo "not inside the nested KWin (WAYLAND_DISPLAY=${WAYLAND_DISPLAY:-} DISPLAY=${DISPLAY:-}); stopping" >&2
  exit 1
}
echo "nested session: WAYLAND_DISPLAY=$WAYLAND_DISPLAY DISPLAY=$DISPLAY"
echo "monitors: $(xrandr --listactivemonitors | tr '\n' ' ')"
trap 'kill $(jobs -p) 2>/dev/null || true' EXIT

# A fullscreen X11 window that re-raises and re-focuses itself every second,
# like a game does.
python3 - <<'PY' &
import tkinter as tk
root = tk.Tk()
root.title("fake game")
root.configure(bg="#203040")
root.attributes("-fullscreen", True)
tk.Label(root, text="FAKE GAME (fullscreen, re-raises every 1 s)",
         fg="white", bg="#203040", font=("sans", 40)).pack(expand=True)

def grab():
    root.lift()
    root.focus_force()
    root.after(1000, grab)

root.after(300, grab)
root.after(10000, root.destroy)
root.mainloop()
PY
GAME=$!
sleep 2

# The real card, with credentials that point nowhere. It stays for about 6 s and
# "×" would report success; nothing here talks to a relay.
python3 - <<'PY' &
import string
import time

from host.qr_window import show_card
from host.session import SessionCredentials

token = (string.ascii_letters + string.digits + "-_")[:43]
credentials = SessionCredentials(
    base_url="https://relay.invalid",
    session_id="abcdefghijklmnopqrstuv",
    publisher_token=token,
    subscriber_token=token[::-1],
)
started = time.monotonic()
show_card(
    credentials,
    keeps_showing=lambda: time.monotonic() - started < 6,
    hide=lambda credentials: True,
)
PY
CARD=$!
sleep 3

ACTIVE="$(xprop -root _NET_ACTIVE_WINDOW | awk '{print $NF}')"
echo "active window: $(xprop -id "$ACTIVE" WM_NAME _NET_WM_STATE | tr '\n' ' ')"
spectacle -b -n -f -o "$CARD_IN_KWIN_OUT" >/dev/null 2>&1 || echo "spectacle failed" >&2

wait "$CARD" && echo "card exited cleanly" || echo "card FAILED (exit $?)" >&2
wait "$GAME" || true
