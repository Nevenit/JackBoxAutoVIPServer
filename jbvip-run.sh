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
# Use as a Steam launch option (one line):
#     /ABSOLUTE/PATH/TO/jbvip-run.sh %command%
#
# Requirements: bubblewrap, mitmproxy, and the redirect above (needs sudo once
# per session). The game itself is never modified or injected into.
set -euo pipefail

PORT="${JBVIP_PROXY_PORT:-8081}"
REPO="$(dirname "$(readlink -f "$0")")"
export PYTHONPATH="$REPO${PYTHONPATH:+:$PYTHONPATH}"
# Optional relay config. Steam launch options inherit the Steam client's
# environment, not a shell's, so a terminal `export` would never reach us.
RELAY_ENV="${XDG_CONFIG_HOME:-$HOME/.config}/jbvip/relay.env"
# shellcheck source=/dev/null
[ -r "$RELAY_ENV" ] && . "$RELAY_ENV"
# Normalise unset to empty so `set -u` stays safe, and so a run invoked directly
# from a shell (rather than by Steam) still honours real environment variables.
export JBVIP_RELAY_URL="${JBVIP_RELAY_URL:-}"
export JBVIP_RELAY_TOKEN="${JBVIP_RELAY_TOKEN:-}"
CGROUP=/sys/fs/cgroup/net_cls/jbvip

# The transparent redirect must be active, or the game's traffic never reaches us.
if [ ! -w "$CGROUP/cgroup.procs" ]; then
  echo "jbvip: redirect not set up. Run:  sudo $REPO/jbvip-redirect-up.sh $PORT" >&2
  exit 1
fi

# Private, per-run state dir (0700) so the trust bundle / log can't be pre-created
# as attacker symlinks.
umask 077
STATE="$(mktemp -d "${XDG_RUNTIME_DIR:-/tmp}/jbvip.XXXXXX")"
CA="$HOME/.mitmproxy/mitmproxy-ca-cert.pem"
BUNDLE="$STATE/cabundle.pem"
# $STATE is deleted on every exit path, which used to take the proxy log with
# it — and that log is the only place a relay misconfiguration is reported.
# Keep a copy when the run failed. It is the proxy's own output, so it can
# contain URLs from the session; hence 0600 (umask above) and failures only.
LAST_LOG="${XDG_STATE_HOME:-$HOME/.local/state}/jbvip/last-run.log"
MITM=""

cleanup() {
  status=$?
  if [ "$status" -ne 0 ] && [ -f "$STATE/mitm.log" ]; then
    if mkdir -p "$(dirname "$LAST_LOG")" 2>/dev/null &&
       cp -f "$STATE/mitm.log" "$LAST_LOG" 2>/dev/null; then
      echo "jbvip: proxy log kept at $LAST_LOG" >&2
    fi
  fi
  if [ -n "$MITM" ]; then
    kill "$MITM" 2>/dev/null || true
  fi
  rm -rf "$STATE"
}
trap cleanup EXIT INT TERM

# The CA file the game's statically-linked libcurl reads (it ignores SSL_CERT_FILE).
SYS_CA=""
for f in /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt /etc/ssl/cert.pem; do
  [ -f "$f" ] && SYS_CA="$f" && break
done
[ -n "$SYS_CA" ] || { echo "jbvip: no system CA bundle found" >&2; exit 1; }

port_up() { ss -ltn 2>/dev/null | grep -qE "[:.]$PORT[[:space:]]"; }
if port_up; then
  echo "jbvip: port $PORT already in use; refusing to start. Set JBVIP_PROXY_PORT to a free port (and re-run jbvip-redirect-up.sh with it)." >&2
  exit 1
fi

# Transparent proxy: the kernel redirect (jbvip-redirect-up.sh) sends the game's
# :443/:38203 here; mitmproxy reads SO_ORIGINAL_DST. The addon also opens the
# RoomCode:<CODE> server on :38469.
mitmdump --mode transparent --listen-port "$PORT" -s "$REPO/extractor/addon.py" \
  >"$STATE/mitm.log" 2>&1 &
MITM=$!

ready=0
for _ in $(seq 1 60); do
  if ! kill -0 "$MITM" 2>/dev/null; then
    echo "jbvip: mitmdump exited during startup; see $STATE/mitm.log" >&2
    tail -n 20 "$STATE/mitm.log" >&2
    exit 1
  fi
  if [ -f "$CA" ] && port_up; then ready=1; break; fi
  sleep 0.1
done
if [ "$ready" != 1 ]; then
  echo "jbvip: proxy did not come up on 127.0.0.1:$PORT within 6s; see $STATE/mitm.log" >&2
  tail -n 20 "$STATE/mitm.log" >&2
  exit 1
fi

# Per-game trust bundle: system CAs + mitmproxy CA.
tmpbundle="$(mktemp "$STATE/cabundle.XXXXXX")"
cat "$SYS_CA" "$CA" > "$tmpbundle"
mv -f "$tmpbundle" "$BUNDLE"

# Join the net_cls cgroup whose :443/:38203 the kernel redirects to mitmproxy.
# The game (this launcher's bwrap child) inherits the cgroup. NOTHING is injected
# into the game process, so the AIR runtime is untouched. mitmdump (started above)
# is NOT in this cgroup, so its own upstream connections aren't re-redirected.
echo $$ > "$CGROUP/cgroup.procs"

# Launch the game in a private mount namespace, binding our bundle over the CA
# path it reads (scoped to the game only). bwrap shares the host network ns.
bwrap --dev-bind / / \
  --bind "$BUNDLE" "$SYS_CA" \
  -- "$@"
