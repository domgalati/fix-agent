#!/usr/bin/env bash
set -euo pipefail

# Drop traffic to a TCP port (both directions) using iptables.
# Requires: sudo, iptables
#
# Usage:
#   sudo ./scripts/faults/drop_port.sh 9876

PORT="${1:-9876}"

echo "==> Dropping TCP port ${PORT} on INPUT/OUTPUT"
sudo iptables -I INPUT 1 -p tcp --dport "${PORT}" -j DROP
sudo iptables -I OUTPUT 1 -p tcp --sport "${PORT}" -j DROP

echo "==> Rules (top):"
sudo iptables -S INPUT | head -n 5
sudo iptables -S OUTPUT | head -n 5
