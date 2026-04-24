#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

mkdir -p .m2

DOCKER_BIN="${DOCKER_BIN:-docker}"
if ! "$DOCKER_BIN" info >/dev/null 2>&1; then
  if command -v sudo >/dev/null 2>&1 && sudo -n true >/dev/null 2>&1; then
    DOCKER_BIN="sudo docker"
  fi
fi

_host_uid() { [[ -n "${SUDO_UID:-}" ]] && echo "$SUDO_UID" || id -u; }
_host_gid() { [[ -n "${SUDO_GID:-}" ]] && echo "$SUDO_GID" || id -g; }
DOCKER_USER_ARGS=(--user "$(_host_uid):$(_host_gid)")

echo "==> Building harness jar (QuickFIX/J 3.0.0 from Maven Central)"
$DOCKER_BIN run --rm \
  "${DOCKER_USER_ARGS[@]}" \
  -v "$ROOT_DIR":/work \
  -w /work/tools/java-harness \
  -e MAVEN_CONFIG=/work/.m2 \
  maven:3-eclipse-temurin-21 \
  mvn -q -DskipTests package

echo "==> OK"
