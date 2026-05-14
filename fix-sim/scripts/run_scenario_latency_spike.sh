#!/usr/bin/env bash
set -euo pipefail

# Fault injection: degrade latency on loopback using tc netem *mid-session*.
# Runs long enough to produce >60s continuous order flow and >20 messages for 3-sigma stats.
# Requires: sudo, iproute2 (tc)

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

JAR="tools/java-harness/target/fixsim-harness-0.1.0-all.jar"
if [[ ! -f "$JAR" ]]; then
  echo "Jar not found: $JAR"
  echo "Run: ./scripts/build.sh"
  exit 1
fi

SCENARIO="latency_spike"

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

_netem_apply_delay() {
  local delay="${1:?delay}"
  local jitter="${2:?jitter}"
  local dist="${3:?distribution}"
  echo "==> Adding netem delay=${delay} ${jitter} distribution ${dist} on lo"
  sudo tc qdisc replace dev lo root netem delay "${delay}" "${jitter}" distribution "${dist}"
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

baseline="${FIXSIM_LAT_SPIKE_BASELINE_SECONDS:-10}"
degrade="${FIXSIM_LAT_SPIKE_DEGRADE_SECONDS:-30}"
recovery="${FIXSIM_LAT_SPIKE_RECOVERY_SECONDS:-20}"

delay="${FIXSIM_LAT_SPIKE_DELAY:-500ms}"
jitter="${FIXSIM_LAT_SPIKE_JITTER:-200ms}"
dist="${FIXSIM_LAT_SPIKE_DISTRIBUTION:-normal}"

order_interval_ms="${FIXSIM_ORDER_INTERVAL_MS:-1000}"
order_duration_seconds="${FIXSIM_ORDER_DURATION_SECONDS:-75}"

hold="${FIXSIM_LAT_SPIKE_HOLD_SECONDS:-110}"

echo "==> Starting acceptor (VENUE) in background"
$DOCKER_BIN rm -f fix-acceptor >/dev/null 2>&1 || true
$DOCKER_BIN run -d --name fix-acceptor --network host \
  "${DOCKER_USER_ARGS[@]}" \
  -v "$ROOT_DIR":/work -w /work \
  -e MAVEN_CONFIG=/work/.m2 \
  maven:3-eclipse-temurin-21 \
  java -cp "/work/${JAR}" fixsim.VenueAcceptor /work/configs/venue_acceptor_latency_spike.cfg

sleep 1

echo "==> Starting initiator (TRADER) in background with continuous order flow"
$DOCKER_BIN rm -f fix-initiator >/dev/null 2>&1 || true
$DOCKER_BIN run -d --name fix-initiator --network host \
  "${DOCKER_USER_ARGS[@]}" \
  -v "$ROOT_DIR":/work -w /work \
  -e MAVEN_CONFIG=/work/.m2 \
  -e FIXSIM_HOLD_SECONDS="${hold}" \
  -e FIXSIM_ORDER_FLOW=1 \
  -e FIXSIM_ORDER_INTERVAL_MS="${order_interval_ms}" \
  -e FIXSIM_ORDER_DURATION_SECONDS="${order_duration_seconds}" \
  maven:3-eclipse-temurin-21 \
  java -cp "/work/${JAR}" fixsim.HeadlessInitiator /work/configs/initiator_latency_spike.cfg

echo "==> Baseline window (~${baseline}s) before latency injection"
sleep "${baseline}"

echo "==> Injecting latency spike for ~${degrade}s"
_netem_apply_delay "${delay}" "${jitter}" "${dist}"
sleep "${degrade}"

echo "==> Restoring network"
_netem_clear

echo "==> Recovery window (~${recovery}s) before logoff"
sleep "${recovery}"

echo "==> Waiting for initiator to exit cleanly"
$DOCKER_BIN wait fix-initiator >/dev/null 2>&1 || true
$DOCKER_BIN rm -f fix-initiator >/dev/null 2>&1 || true

echo "==> Stopping acceptor"
$DOCKER_BIN rm -f fix-acceptor >/dev/null 2>&1 || true

echo "==> Logs written to:"
echo "  logs/${SCENARIO}/acceptor/"
echo "  logs/${SCENARIO}/initiator/"

