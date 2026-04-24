#!/usr/bin/env bash
set -euo pipefail

# Restore traffic to a TCP port by removing DROP rules inserted by drop_port.sh.
# Requires: sudo, iptables
#
# Usage:
#   sudo ./scripts/faults/restore_port.sh 9876

PORT="${1:-9876}"

echo "==> Removing DROP rules for TCP port ${PORT}"

# Remove all matching rules; loop until none remain.
while sudo iptables -C INPUT -p tcp --dport "${PORT}" -j DROP 2>/dev/null; do
  sudo iptables -D INPUT -p tcp --dport "${PORT}" -j DROP
done

while sudo iptables -C OUTPUT -p tcp --sport "${PORT}" -j DROP 2>/dev/null; do
  sudo iptables -D OUTPUT -p tcp --sport "${PORT}" -j DROP
done

echo "==> Done"
