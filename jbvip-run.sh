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
STATE="/tmp/jbvip"
CA="$HOME/.mitmproxy/mitmproxy-ca-cert.pem"
BUNDLE="$STATE/cabundle.pem"
mkdir -p "$STATE"

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

mitmdump --listen-port "$PORT" -s "$(dirname "$(readlink -f "$0")")/extractor/addon.py" \
  >"$STATE/mitm.log" 2>&1 &
MITM=$!
STARTED=1
for _ in $(seq 1 60); do { [ -f "$CA" ] && port_up; } && break; sleep 0.1; done
trap '[ "$STARTED" = 1 ] && kill "$MITM" 2>/dev/null || true' EXIT

export PYTHONPATH="$(dirname "$(readlink -f "$0")")${PYTHONPATH:+:$PYTHONPATH}"

[ -f "$CA" ] || { echo "jbvip: mitmproxy CA not found at $CA (run mitmdump once)" >&2; exit 1; }

# 2. Build the per-game trust bundle: system CAs + mitmproxy CA.
cat "$SYS_CA" "$CA" > "$BUNDLE"

# 3. Launch the game in a private mount namespace:
#    - bind our bundle over the CA path the game reads (scoped to the game only)
#    - route the game's HTTPS through mitmproxy
#    bwrap shares the host network namespace, so 127.0.0.1:PORT is reachable.
bwrap --dev-bind / / \
  --bind "$BUNDLE" "$SYS_CA" \
  --setenv HTTP_PROXY  "http://127.0.0.1:$PORT" \
  --setenv HTTPS_PROXY "http://127.0.0.1:$PORT" \
  -- "$@"
