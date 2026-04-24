#!/usr/bin/env bash
set -euo pipefail

# Inject packet loss on loopback. This works best when your FIX containers run with --network host.
# Requires: sudo, iproute2 (tc)
#
# Usage:
#   sudo ./scripts/faults/packet_loss.sh 10%

LOSS="${1:-10%}"

echo "==> Adding netem loss=${LOSS} on lo"
sudo tc qdisc replace dev lo root netem loss "${LOSS}"

echo "==> Current qdisc:"
tc qdisc show dev lo
