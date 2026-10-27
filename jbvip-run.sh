#!/usr/bin/env bash
# jbvip-run.sh — run a Jackbox game with room-code interception.
#
# Interception is fully scoped to the game, and the mitmproxy CA is injected ONLY
# into the game's own mount namespace (via bubblewrap), so the rest of the system
# never trusts it and nothing persists after the game exits.
#
# Requires a per-session transparent redirect, set up once with:
#     sudo ./jbvip-redirect-up.sh
# It redirects ONLY this game's :443 + :38203 to mitmproxy (covers ecast and both
# Blobcast variants, including websocket-only packs like Party Pack 1). Tear it
# down with: sudo ./jbvip-redirect-down.sh
#
# When interception can't run (no redirect, no system CA bundle, the proxy port
# taken, mitmdump failing to start) the game still starts, just without room
# codes, and a desktop notification says why. See run_plain.
#
# Use as a Steam launch option (one line):
#     /ABSOLUTE/PATH/TO/jbvip-run.sh %command%
#
# Requirements: bubblewrap, mitmproxy, and the redirect above (needs sudo once
# per session). The game itself is never modified or injected into.
set -euo pipefail

# The game's command line. An array because "$@" inside a function is that
# function's own arguments, and run_plain needs the game's.
GAME=("$@")

# Anything that stops us capturing room codes ends here instead of in `exit 1`:
# a launcher that keeps the game from starting is worse than none, and a missing
# redirect, a taken port or a broken mitmdump must never cost a game night.
# Under Steam stderr goes nowhere anyone looks, so the reason is also sent as a
# desktop notification.
#
# Runs the game directly (no redirect, proxy or bwrap) in the foreground, and
# exits with ITS status (what Steam sees; Jackbox games exit non-zero routinely).
# `exit` and not `exec`, so the EXIT trap still stops mitmdump and removes the
# state dir. The status is captured rather than left to `set -e`, which is off
# inside an `if` condition or a `||` list, so it is the game's wherever this is
# called from.
run_plain() {
  echo "jbvip: $1; starting the game without room-code capture" >&2
  if command -v notify-send >/dev/null 2>&1; then
    notify-send -a jbvip "Jackbox: no room codes this time" "$1" || true
  fi
  local rc=0
  "${GAME[@]}" || rc=$?
  exit "$rc"
}

PORT="${JBVIP_PROXY_PORT:-8081}"
REPO="$(dirname "$(readlink -f "$0")")"
export PYTHONPATH="$REPO${PYTHONPATH:+:$PYTHONPATH}"
# Optional relay config. Steam launch options inherit the Steam client's
# environment, not a shell's, so a terminal `export` would never reach us.
# Only a URL now — every credential is minted per session at launch and dies
# with it, so there is no pre-shared secret on this machine any more.
RELAY_ENV="${XDG_CONFIG_HOME:-$HOME/.config}/jbvip/relay.env"
# shellcheck source=/dev/null
[ -r "$RELAY_ENV" ] && . "$RELAY_ENV"
# Normalise unset to empty so `set -u` stays safe, and so a run invoked directly
# from a shell (rather than by Steam) still honours a real environment variable.
JBVIP_RELAY_URL="${JBVIP_RELAY_URL:-}"
CGROUP=/sys/fs/cgroup/net_cls/jbvip

# The transparent redirect must be active, or the game's traffic never reaches us.
if [ ! -w "$CGROUP/cgroup.procs" ]; then
  run_plain "redirect not set up (run: sudo $REPO/jbvip-redirect-up.sh $PORT)"
fi

# Private, per-run state dir (0700) so the trust bundle / log can't be pre-created
# as attacker symlinks.
umask 077
STATE="$(mktemp -d "${XDG_RUNTIME_DIR:-/tmp}/jbvip.XXXXXX")"
CA="$HOME/.mitmproxy/mitmproxy-ca-cert.pem"
BUNDLE="$STATE/cabundle.pem"
# $STATE is deleted on every exit path, which used to take the proxy log with
# it — and that log is the only place a relay problem is ever reported.
#
# Kept only when the log actually says something about the relay (the addon
# marks those lines with LOG_MARKER, see extractor/relay_publisher.py). Not on
# every failed run: Jackbox games exit non-zero routinely, so that would keep a
# log per launch, almost always with nothing in it worth reading.
#
# Redacted on the way out. The log is mitmdump's own output and at
# flow_detail=1 it prints the request URL — and the ecast play websocket URL is
# /api/v2/rooms/<CODE>/play, once per frame. Room codes are never written to
# disk anywhere else in this project and must not start here.
LAST_LOG="${XDG_STATE_HOME:-$HOME/.local/state}/jbvip/last-run.log"
# Must match extractor.relay_publisher.LOG_MARKER; tests/test_launcher.py
# fails if the two drift apart.
LOG_MARKER="jbvip-relay:"
# Anchored on the '/rooms/' segment and deliberately not requiring a trailing
# delimiter, because flow_detail=1 truncates long URLs mid-code.
REDACT_CODES='s#(/rooms/)[A-Za-z0-9]+#\1<redacted>#g'
MITM=""

cleanup() {
  # Clear the room, not the session: the session is tonight's and the next pack
  # reuses it (see NIGHT_FILE), but this pack has quit, so phones still
  # following it go back to "waiting" instead of showing a dead code.
  # `${SESSION_ID:-}` because the trap can fire before that line ran.
  if [ -n "${SESSION_ID:-}" ]; then
    JBVIP_RELAY_URL="$JBVIP_RELAY_URL" \
    JBVIP_SESSION_ID="$SESSION_ID" \
    JBVIP_SESSION_TOKEN="$SESSION_TOKEN" \
      python3 -m host.session --clear >/dev/null 2>&1 || true
  fi
  if [ -f "$STATE/mitm.log" ] && grep -qF "$LOG_MARKER" "$STATE/mitm.log"; then
    if mkdir -p "$(dirname "$LAST_LOG")" 2>/dev/null &&
       sed -E "$REDACT_CODES" "$STATE/mitm.log" >"$LAST_LOG" 2>/dev/null; then
      chmod 600 "$LAST_LOG" 2>/dev/null || true
      echo "jbvip: the relay reported a problem; log kept at $LAST_LOG" >&2
    fi
  fi
  if [ -n "$MITM" ]; then
    kill "$MITM" 2>/dev/null || true
  fi
  rm -rf "$STATE"
}
trap cleanup EXIT INT TERM

# The CA bundle and port checks come before the pairing window below, like the
# redirect check above: on a night that can't capture a room code there is
# nothing to ask anyone to scan.

# The CA file the game's statically-linked libcurl reads (it ignores SSL_CERT_FILE).
SYS_CA=""
for f in /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt /etc/ssl/cert.pem; do
  [ -f "$f" ] && SYS_CA="$f" && break
done
[ -n "$SYS_CA" ] || run_plain "no system CA bundle found"
# bubblewrap 0.13 refuses to mount onto a symlink, and Arch's CA path is one, so
# mount over the file it points to. The game still opens it through the link.
CA_TARGET="$(readlink -f "$SYS_CA")"

# Something else owns the proxy port. JBVIP_PROXY_PORT picks another one, and
# jbvip-redirect-up.sh has to be re-run with it.
port_up() { ss -ltn 2>/dev/null | grep -qE "[:.]$PORT[[:space:]]"; }
if port_up; then
  run_plain "port $PORT is already in use"
fi

# --- pair a phone, if a relay is configured ---------------------------------
# Deliberately before mitmdump and the game: fighting a fullscreen game for
# stacking order under Wayland is unreliable, and there is a natural gap here.
# Not exported — see the mitmdump line below.
SESSION_ID=""
SESSION_TOKEN=""
SESSION_ENV="$STATE/session.env"
# The session is tonight's, not this launch's: the QR window resumes the one
# saved here (or makes one and saves it), and only shows its QR code when no
# phone is connected yet, so switching packs doesn't mean scanning again.
NIGHT_FILE="${XDG_STATE_HOME:-$HOME/.local/state}/jbvip/night.json"
if [ -n "$JBVIP_RELAY_URL" ]; then
  # The `if` matters: this exits non-zero when the relay is unreachable, and
  # under `set -e` a bare call would end the script and the game would never
  # launch. A dead VPS must never cost a game night.
  if python3 -m host.qr_window --base-url "$JBVIP_RELAY_URL" \
       --session-env "$SESSION_ENV" \
       --night-file "$NIGHT_FILE"; then
    if [ -r "$SESSION_ENV" ]; then
      # shellcheck source=/dev/null
      . "$SESSION_ENV"
      SESSION_ID="${JBVIP_SESSION_ID:-}"
      SESSION_TOKEN="${JBVIP_SESSION_TOKEN:-}"
      unset JBVIP_SESSION_ID JBVIP_SESSION_TOKEN
    fi
  else
    echo "jbvip: no relay session; the room code stays LAN-only" >&2
  fi
fi

# Transparent proxy: the kernel redirect (jbvip-redirect-up.sh) sends the game's
# :443/:38203 here; mitmproxy reads SO_ORIGINAL_DST. The addon also opens the
# RoomCode:<CODE> server on :38469.
# flow_detail is pinned rather than left at its default: at 3 and above mitmdump
# prints websocket message contents and response bodies, which is where every
# other protocol's room code lives (Blobcast's roomId frames, the ecast create
# response). Pinned to 1, the URL is the only place a code can appear, and the
# redaction in cleanup() covers it.
#
# It MUST be --flow-detail and not `--set flow_detail=1`. mitmproxy applies --set
# options *before* it loads ~/.mitmproxy/config.yaml, and the config load then
# overwrites them, so a user with `flow_detail: 4` there silently wins and the
# redaction stops being sufficient. --flow-detail is applied in process_options,
# which runs after the config load, so it is the one that holds. (See
# mitmproxy/tools/main.py: opts.set(..., defer=True) precedes load_paths(),
# process_options() follows it.) This project guarantees ~/.mitmproxy exists,
# because the CA lives there. Do not "simplify" this back to --set.
JBVIP_RELAY_URL="$JBVIP_RELAY_URL" \
JBVIP_SESSION_ID="$SESSION_ID" \
JBVIP_SESSION_TOKEN="$SESSION_TOKEN" \
mitmdump --mode transparent --listen-port "$PORT" --flow-detail 1 \
  -s "$REPO/extractor/addon.py" \
  >"$STATE/mitm.log" 2>&1 &
MITM=$!

ready=0
for _ in $(seq 1 60); do
  if ! kill -0 "$MITM" 2>/dev/null; then
    echo "jbvip: mitmdump exited during startup; see $STATE/mitm.log" >&2
    tail -n 20 "$STATE/mitm.log" >&2
    run_plain "mitmdump exited during startup"
  fi
  if [ -f "$CA" ] && port_up; then ready=1; break; fi
  sleep 0.1
done
if [ "$ready" != 1 ]; then
  echo "jbvip: proxy did not come up on 127.0.0.1:$PORT within 6s; see $STATE/mitm.log" >&2
  tail -n 20 "$STATE/mitm.log" >&2
  run_plain "proxy did not come up on 127.0.0.1:$PORT within 6s"
fi

# Per-game trust bundle: system CAs + mitmproxy CA.
tmpbundle="$(mktemp "$STATE/cabundle.XXXXXX")"
cat "$SYS_CA" "$CA" > "$tmpbundle"
mv -f "$tmpbundle" "$BUNDLE"

# Only the game joins the net_cls cgroup whose :443/:38203 the kernel redirects to
# mitmproxy: a subshell joins, then execs bwrap. The launcher stays out, so its own
# HTTPS (the relay DELETE in cleanup) isn't redirected into the proxy. mitmdump
# (started above) is outside too, so its upstream connections aren't re-redirected.
# NOTHING is injected into the game process, so the AIR runtime is untouched.
#
# Launch the game in a private mount namespace, binding our bundle over the CA
# path it reads (scoped to the game only). bwrap shares the host network ns.
(
  echo "$BASHPID" > "$CGROUP/cgroup.procs"
  exec bwrap --dev-bind / / \
    --bind "$BUNDLE" "$CA_TARGET" \
    -- "$@"
)
