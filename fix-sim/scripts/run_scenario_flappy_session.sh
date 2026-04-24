#!/usr/bin/env bash
set -euo pipefail

# Fault injection: repeatedly kill/restart the initiator container mid-session ("flappy" client).

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

JAR="tools/java-harness/target/fixsim-harness-0.1.0-all.jar"
if [[ ! -f "$JAR" ]]; then
  echo "Jar not found: $JAR"
  echo "Run: ./scripts/build.sh"
  exit 1
fi

SCENARIO="flappy_session"

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

cleanup() {
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
  java -cp "/work/${JAR}" fixsim.VenueAcceptor /work/configs/venue_acceptor_flappy_session.cfg

sleep 1

cycles="${FIXSIM_FLAPPY_CYCLES:-4}"
hold="${FIXSIM_FLAPPY_HOLD_SECONDS:-60}"

echo "==> Running ${cycles} flappy cycles (kill/restart initiator)"
for i in $(seq 1 "$cycles"); do
  echo "==> Cycle ${i}/${cycles}: start initiator (stay alive), then kill it"
  $DOCKER_BIN rm -f fix-initiator >/dev/null 2>&1 || true
  $DOCKER_BIN run -d --name fix-initiator --network host \
    "${DOCKER_USER_ARGS[@]}" \
    -v "$ROOT_DIR":/work -w /work \
    -e MAVEN_CONFIG=/work/.m2 \
    -e FIXSIM_NO_ORDER=1 \
    -e FIXSIM_HOLD_SECONDS="${hold}" \
    maven:3-eclipse-temurin-21 \
    java -cp "/work/${JAR}" fixsim.HeadlessInitiator /work/configs/initiator_flappy_session.cfg

  # Let it logon and exchange a few heartbeats (then kill mid-session).
  sleep 3
  $DOCKER_BIN rm -f fix-initiator >/dev/null 2>&1 || true
  sleep 2
done

echo "==> Final run: initiator sends one order and exits cleanly (logoff)"
$DOCKER_BIN rm -f fix-initiator >/dev/null 2>&1 || true
$DOCKER_BIN run --rm --name fix-initiator --network host \
  "${DOCKER_USER_ARGS[@]}" \
  -v "$ROOT_DIR":/work -w /work \
  -e MAVEN_CONFIG=/work/.m2 \
  -e FIXSIM_HOLD_SECONDS=2 \
  maven:3-eclipse-temurin-21 \
  java -cp "/work/${JAR}" fixsim.HeadlessInitiator /work/configs/initiator_flappy_session.cfg

echo "==> Stopping acceptor"
$DOCKER_BIN rm -f fix-acceptor >/dev/null 2>&1 || true

echo "==> Logs written to:"
echo "  logs/${SCENARIO}/acceptor/"
echo "  logs/${SCENARIO}/initiator/"

