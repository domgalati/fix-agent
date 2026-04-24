#!/usr/bin/env bash
set -euo pipefail

# Fault injection: apply 50% packet loss on loopback using tc netem while the session is live.
# This works because the containers run with --network host and talk over 127.0.0.1.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

JAR="tools/java-harness/target/fixsim-harness-0.1.0-all.jar"
if [[ ! -f "$JAR" ]]; then
  echo "Jar not found: $JAR"
  echo "Run: ./scripts/build.sh"
  exit 1
fi

SCENARIO="network_loss"

mkdir -p "logs/${SCENARIO}/"{acceptor,initiator} "state/${SCENARIO}/"{acceptor,initiator}

DOCKER_BIN="${DOCKER_BIN:-docker}"
if ! "$DOCKER_BIN" info >/dev/null 2>&1; then
  if command -v sudo >/dev/null 2>&1 && sudo -n true >/dev/null 2>&1; then
    DOCKER_BIN="sudo docker"
  fi
fi

_host_uid() { [[ -n "${SUDO_UID:-}" ]] && echo "$SUDO_UID" || id -u; }
_host_gid() { [[ -n "${SUDO_GID:-}" ]] && echo "$SUDO_GID" || id -g; }
DOCKER_USER_ARGS=(--user "$(_host_uid):$(_host_gid)")

_netem_apply() {
  local loss="${1:?loss}"
  echo "==> Adding netem loss=${loss} on lo"
  sudo tc qdisc replace dev lo root netem loss "${loss}"
}

_netem_clear() {
  echo "==> Removing netem on lo (if present)"
  sudo tc qdisc del dev lo root 2>/dev/null || true
}

cleanup() {
  _netem_clear || true
  $DOCKER_BIN rm -f fix-initiator >/dev/null 2>&1 || true
  $DOCKER_BIN rm -f fix-acceptor >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "==> Starting acceptor (VENUE) in background"
$DOCKER_BIN rm -f fix-acceptor >/dev/null 2>&1 || true
$DOCKER_BIN run -d --name fix-acceptor --network host \
  "${DOCKER_USER_ARGS[@]}" \
  -v "$ROOT_DIR":/work -w /work \
  -e MAVEN_CONFIG=/work/.m2 \
  maven:3-eclipse-temurin-21 \
  java -cp "/work/${JAR}" fixsim.VenueAcceptor /work/configs/venue_acceptor_network_loss.cfg

sleep 1

echo "==> Running initiator once (order->exec), then holding session open"
$DOCKER_BIN rm -f fix-initiator >/dev/null 2>&1 || true
$DOCKER_BIN run -d --name fix-initiator --network host \
  "${DOCKER_USER_ARGS[@]}" \
  -v "$ROOT_DIR":/work -w /work \
  -e MAVEN_CONFIG=/work/.m2 \
  -e FIXSIM_HOLD_SECONDS="${FIXSIM_NETWORK_DEGRADE_TOTAL_SECONDS:-20}" \
  maven:3-eclipse-temurin-21 \
  java -cp "/work/${JAR}" fixsim.HeadlessInitiator /work/configs/initiator_network_loss.cfg

# Let logon/order happen before degrading.
sleep 2

echo "==> Degrading network with 50% packet loss for ~10 seconds"
_netem_apply "${FIXSIM_NETWORK_LOSS_PCT:-50%}"
sleep "${FIXSIM_NETWORK_DEGRADE_SECONDS:-10}"

echo "==> Restoring network"
_netem_clear

# Allow recovery / resends / heartbeats to settle.
sleep "${FIXSIM_NETWORK_RECOVERY_SECONDS:-8}"

echo "==> Waiting for initiator to exit cleanly"
$DOCKER_BIN wait fix-initiator >/dev/null 2>&1 || true
$DOCKER_BIN rm -f fix-initiator >/dev/null 2>&1 || true

echo "==> Stopping acceptor"
$DOCKER_BIN rm -f fix-acceptor >/dev/null 2>&1 || true

echo "==> Logs written to:"
echo "  logs/${SCENARIO}/acceptor/"
echo "  logs/${SCENARIO}/initiator/"

