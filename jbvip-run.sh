#!/usr/bin/env bash
# jbvip-run.sh — run a Jackbox game with room-code interception.
#
# NO system-wide trust change, NO root. The mitmproxy CA is injected ONLY into
# the game's own mount namespace (via bubblewrap), so the rest of the system
# never trusts it and nothing persists after the game exits. mitmproxy runs
# only while the game is running.
#
# Use as a Steam launch option (one line):
#     /ABSOLUTE/PATH/TO/jbvip-run.sh %command%
#
# Requirements: bubblewrap (bwrap), unprivileged user namespaces, mitmproxy.
set -euo pipefail

PORT="${JBVIP_PROXY_PORT:-8080}"
REPO="$(dirname "$(readlink -f "$0")")"
export PYTHONPATH="$REPO${PYTHONPATH:+:$PYTHONPATH}"

# Private, per-run state dir: mktemp -d => 0700, owned by us, unpredictable path,
# so the trust bundle and mitm.log cannot be pre-created as attacker symlinks.
umask 077
STATE="$(mktemp -d "${XDG_RUNTIME_DIR:-/tmp}/jbvip.XXXXXX")"
CA="$HOME/.mitmproxy/mitmproxy-ca-cert.pem"
BUNDLE="$STATE/cabundle.pem"

# Clean up the private state dir on any exit, including the early port-in-use
# exit below (before mitmdump is forked).
trap 'rm -rf "$STATE"' EXIT INT TERM

# The CA file the game's statically-linked libcurl reads (it ignores SSL_CERT_FILE).
SYS_CA=""
for f in /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt /etc/ssl/cert.pem; do
  [ -f "$f" ] && SYS_CA="$f" && break
done
[ -n "$SYS_CA" ] || { echo "jbvip: no system CA bundle found" >&2; exit 1; }

port_up() { ss -ltn 2>/dev/null | grep -qE "[:.]$PORT[[:space:]]"; }

# Refuse to silently reuse a stray proxy that has no addon loaded.
if port_up; then
  echo "jbvip: port $PORT is already in use; refusing to start. Set JBVIP_PROXY_PORT to a free port." >&2
  exit 1
fi

mitmdump --listen-port "$PORT" -s "$REPO/extractor/addon.py" \
  >"$STATE/mitm.log" 2>&1 &
MITM=$!
# Reap mitmdump and clean the private state dir on any exit/signal during or
# after the wait window, so the proxy is never orphaned holding the port.
trap 'kill "$MITM" 2>/dev/null; rm -rf "$STATE"' EXIT INT TERM

ready=0
for _ in $(seq 1 60); do
  # mitmdump died during startup -> show the traceback and stop.
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

# 2. Build the per-game trust bundle: system CAs + mitmproxy CA.
#    Write to a fresh temp file in the private STATE dir and rename into place.
tmpbundle="$(mktemp "$STATE/cabundle.XXXXXX")"
cat "$SYS_CA" "$CA" > "$tmpbundle"
mv -f "$tmpbundle" "$BUNDLE"

# 3. Launch the game in a private mount namespace:
#    - bind our bundle over the CA path the game reads (scoped to the game only)
#    - route the game's HTTPS through mitmproxy
#    bwrap shares the host network namespace, so 127.0.0.1:PORT is reachable.
bwrap --dev-bind / / \
  --bind "$BUNDLE" "$SYS_CA" \
  --setenv HTTP_PROXY  "http://127.0.0.1:$PORT" \
  --setenv HTTPS_PROXY "http://127.0.0.1:$PORT" \
  -- "$@"
