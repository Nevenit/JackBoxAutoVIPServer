#!/usr/bin/env bash
# jbvip-redirect-up.sh — GAME-SCOPED transparent redirect via cgroup v1 net_cls (sudo).
#
# Tags a net_cls cgroup with a classid and redirects ONLY that classid's outbound
# :443 + :38203 to mitmproxy's transparent listener. Nothing else on the machine
# is touched. mitmproxy runs OUTSIDE the cgroup, so its own upstream connections
# are not re-redirected (no loop). Run once per session.
#
#   sudo ./jbvip-redirect-up.sh [PORT] [USER]  (PORT default 8081; USER default the sudo user)
set -euo pipefail

PORT="${1:-8081}"
# USER is for the boot-time service, which has no SUDO_USER (see the README).
USER_NAME="${2:-${SUDO_USER:-$USER}}"
CLASSID="0x00110011"
NC=/sys/fs/cgroup/net_cls/jbvip

# Mullvad's daemon mounts this hierarchy when it runs; without it, mount it ourselves.
if ! mountpoint -q /sys/fs/cgroup/net_cls; then
  mkdir -p /sys/fs/cgroup/net_cls
  mount -t cgroup -o net_cls net_cls /sys/fs/cgroup/net_cls
fi

mkdir -p "$NC"
echo "$CLASSID" > "$NC/net_cls.classid"
# Let the (non-root) launcher add the game to the cgroup without sudo.
chown "$USER_NAME" "$NC/cgroup.procs" "$NC/tasks" 2>/dev/null || true

for p in 443 38203; do
  iptables -t nat -C OUTPUT -p tcp --dport "$p" -m cgroup --cgroup "$CLASSID" -j REDIRECT --to-ports "$PORT" 2>/dev/null \
    || iptables -t nat -A OUTPUT -p tcp --dport "$p" -m cgroup --cgroup "$CLASSID" -j REDIRECT --to-ports "$PORT"
done

echo "jbvip redirect ACTIVE: net_cls classid $CLASSID, :443 + :38203 -> 127.0.0.1:$PORT (membership delegated to $USER_NAME)."
echo "Add the game with:  echo <pid> > $NC/cgroup.procs   (the launcher does this)"
echo "Tear down with:     sudo ./jbvip-redirect-down.sh $PORT"
