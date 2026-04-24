#!/usr/bin/env bash
set -euo pipefail

# Inject latency/jitter on loopback. Best with --network host.
# Requires: sudo, iproute2 (tc)
#
# Usage:
#   sudo ./scripts/faults/latency.sh 200ms 50ms

DELAY="${1:-200ms}"
JITTER="${2:-50ms}"

echo "==> Adding netem delay=${DELAY} ${JITTER} on lo"
sudo tc qdisc replace dev lo root netem delay "${DELAY}" "${JITTER}"

echo "==> Current qdisc:"
tc qdisc show dev lo
