#!/usr/bin/env bash
# jbvip-redirect-down.sh — remove the game-scoped transparent redirect (sudo).
# Removes both the net_cls (v1) classid rules and any leftover v2 --path rules.
#   sudo ./jbvip-redirect-down.sh [PORT]      (PORT default 8081)
set -uo pipefail

PORT="${1:-8081}"
CLASSID="0x00110011"
for p in 443 38203; do
  iptables -t nat -D OUTPUT -p tcp --dport "$p" -m cgroup --cgroup "$CLASSID" -j REDIRECT --to-ports "$PORT" 2>/dev/null || true
  iptables -t nat -D OUTPUT -p tcp --dport "$p" -m cgroup --path jbvip -j REDIRECT --to-ports "$PORT" 2>/dev/null || true
done
rmdir /sys/fs/cgroup/net_cls/jbvip 2>/dev/null || true
rmdir /sys/fs/cgroup/jbvip 2>/dev/null || true
echo "jbvip redirect REMOVED."
