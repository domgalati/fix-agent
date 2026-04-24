#!/usr/bin/env bash
set -euo pipefail

# Remove netem from loopback.
# Usage:
#   sudo ./scripts/faults/clear_netem.sh

echo "==> Removing netem on lo (if present)"
sudo tc qdisc del dev lo root 2>/dev/null || true

echo "==> Current qdisc:"
tc qdisc show dev lo
