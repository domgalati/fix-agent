#!/usr/bin/env bash
set -euo pipefail

# Fault injection: block acceptor->initiator traffic only (one-way) using iptables OUTPUT --sport 9876.
# This simulates a silent/unidirectional network failure (initiator stops receiving heartbeats).

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

JAR="tools/java-harness/target/fixsim-harness-0.1.0-all.jar"
if [[ ! -f "$JAR" ]]; then
  echo "Jar not found: $JAR"
  echo "Run: ./scripts/build.sh"
  exit 1
fi

SCENARIO="heartbeat_miss"
PORT="${FIXSIM_PORT:-9876}"

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

_rule_exists() {
  sudo iptables -C OUTPUT -p tcp --sport "${PORT}" -j DROP >/dev/null 2>&1
}

_add_rule_if_missing() {
  if _rule_exists; then
    return 0
  fi
  sudo iptables -I OUTPUT 1 -p tcp --sport "${PORT}" -j DROP
}

_del_rule_if_present() {
  if _rule_exists; then
    sudo iptables -D OUTPUT -p tcp --sport "${PORT}" -j DROP
  fi
}

block_one_way() {
  # Block acceptor -> initiator by dropping packets *leaving* the acceptor port.
  # Acceptor sends from source port 9876 to initiator's ephemeral port.
  echo "==> Blocking acceptor->initiator (OUTPUT --sport ${PORT})"
  _add_rule_if_missing
}

unblock_one_way() {
  echo "==> Removing acceptor->initiator block (if present)"
  _del_rule_if_present
}

cleanup() {
  unblock_one_way || true
  $DOCKER_BIN rm -f fix-initiator >/dev/null 2>&1 || true
  $DOCKER_BIN rm -f fix-acceptor >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "==> Starting acceptor (VENUE) in background (HeartBtInt=5s)"
$DOCKER_BIN rm -f fix-acceptor >/dev/null 2>&1 || true
$DOCKER_BIN run -d --name fix-acceptor --network host \
  "${DOCKER_USER_ARGS[@]}" \
  -v "$ROOT_DIR":/work -w /work \
  -e MAVEN_CONFIG=/work/.m2 \
  maven:3-eclipse-temurin-21 \
  java -cp "/work/${JAR}" fixsim.VenueAcceptor /work/configs/venue_acceptor_heartbeat_miss.cfg

sleep 1

echo "==> Starting initiator (TRADER) in background; hold open so we can inject fault"
$DOCKER_BIN rm -f fix-initiator >/dev/null 2>&1 || true
$DOCKER_BIN run -d --name fix-initiator --network host \
  "${DOCKER_USER_ARGS[@]}" \
  -v "$ROOT_DIR":/work -w /work \
  -e MAVEN_CONFIG=/work/.m2 \
  -e FIXSIM_HOLD_SECONDS="${FIXSIM_HEARTBEAT_TOTAL_SECONDS:-30}" \
  maven:3-eclipse-temurin-21 \
  java -cp "/work/${JAR}" fixsim.HeadlessInitiator /work/configs/initiator_heartbeat_miss.cfg

# Let logon/order/exec happen before blocking.
sleep 2

block_one_way
sleep "${FIXSIM_HEARTBEAT_BLOCK_SECONDS:-15}"
unblock_one_way

# Allow time for TestRequests / recovery / potential timeout handling in logs.
sleep "${FIXSIM_HEARTBEAT_RECOVERY_SECONDS:-8}"

echo "==> Waiting for initiator to exit cleanly"
$DOCKER_BIN wait fix-initiator >/dev/null 2>&1 || true
$DOCKER_BIN rm -f fix-initiator >/dev/null 2>&1 || true

echo "==> Stopping acceptor"
$DOCKER_BIN rm -f fix-acceptor >/dev/null 2>&1 || true

echo "==> Logs written to:"
echo "  logs/${SCENARIO}/acceptor/"
echo "  logs/${SCENARIO}/initiator/"

