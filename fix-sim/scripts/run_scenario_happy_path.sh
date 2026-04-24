#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

JAR="tools/java-harness/target/fixsim-harness-0.1.0-all.jar"
if [[ ! -f "$JAR" ]]; then
  echo "Jar not found: $JAR"
  echo "Run: ./scripts/build.sh"
  exit 1
fi

SCENARIO="happy_path"
mkdir -p "logs/${SCENARIO}/"{acceptor,initiator} "state/${SCENARIO}/"{acceptor,initiator}

DOCKER_BIN="${DOCKER_BIN:-docker}"
if ! "$DOCKER_BIN" info >/dev/null 2>&1; then
  if command -v sudo >/dev/null 2>&1 && sudo -n true >/dev/null 2>&1; then
    DOCKER_BIN="sudo docker"
  fi
fi

# Bind-mounted logs/state must be owned by the host user (not root), or host-side tools fail.
_host_uid() { [[ -n "${SUDO_UID:-}" ]] && echo "$SUDO_UID" || id -u; }
_host_gid() { [[ -n "${SUDO_GID:-}" ]] && echo "$SUDO_GID" || id -g; }
DOCKER_USER_ARGS=(--user "$(_host_uid):$(_host_gid)")

echo "==> Starting acceptor (VENUE) in background"
$DOCKER_BIN rm -f fix-acceptor >/dev/null 2>&1 || true
$DOCKER_BIN run -d --name fix-acceptor --network host \
  "${DOCKER_USER_ARGS[@]}" \
  -v "$ROOT_DIR":/work -w /work \
  -e MAVEN_CONFIG=/work/.m2 \
  maven:3-eclipse-temurin-21 \
  java -cp "/work/${JAR}" fixsim.VenueAcceptor /work/configs/executor_happy_path.cfg

echo "==> Running initiator (TRADER) once (sends 1 order, waits for exec, exits)"
$DOCKER_BIN rm -f fix-initiator >/dev/null 2>&1 || true
$DOCKER_BIN run --rm --name fix-initiator --network host \
  "${DOCKER_USER_ARGS[@]}" \
  -v "$ROOT_DIR":/work -w /work \
  -e MAVEN_CONFIG=/work/.m2 \
  maven:3-eclipse-temurin-21 \
  java -cp "/work/${JAR}" fixsim.HeadlessInitiator /work/configs/initiator_happy_path.cfg

echo "==> Stopping acceptor"
$DOCKER_BIN rm -f fix-acceptor >/dev/null

echo "==> Logs written to:"
echo "  logs/${SCENARIO}/acceptor/"
echo "  logs/${SCENARIO}/initiator/"

