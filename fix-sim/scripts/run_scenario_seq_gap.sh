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

SCENARIO="seq_gap"
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

echo "==> Starting acceptor (VENUE)"
$DOCKER_BIN rm -f fix-acceptor >/dev/null 2>&1 || true
$DOCKER_BIN run -d --name fix-acceptor --network host \
  "${DOCKER_USER_ARGS[@]}" \
  -v "$ROOT_DIR":/work -w /work \
  -e MAVEN_CONFIG=/work/.m2 \
  maven:3-eclipse-temurin-21 \
  java -cp "/work/${JAR}" fixsim.VenueAcceptor /work/configs/venue_acceptor_seq_gap.cfg

# Give acceptor time to bind before initiator connects
sleep 1

echo "==> Running initiator once to establish baseline state"
$DOCKER_BIN rm -f fix-initiator >/dev/null 2>&1 || true
$DOCKER_BIN run --rm --name fix-initiator --network host \
  "${DOCKER_USER_ARGS[@]}" \
  -v "$ROOT_DIR":/work -w /work \
  -e MAVEN_CONFIG=/work/.m2 \
  maven:3-eclipse-temurin-21 \
  java -cp "/work/${JAR}" fixsim.HeadlessInitiator /work/configs/initiator_seq_gap.cfg

echo "==> Forcing outgoing seqnum jump in initiator filestore"
# QuickFIX/J 3.x FileStore uses Java DataOutput writeUTF per file:
#   <session>.senderseqnums   (next outgoing / sender seq)
#   <session>.targetseqnums   (next expected incoming)
# Older CachedFileStore used a single *.seqnums file — not used by default FileStoreFactory.
shopt -s nullglob
seq_files=( "$ROOT_DIR/state/${SCENARIO}/initiator/"*.senderseqnums )
shopt -u nullglob
SEQFILE="${seq_files[0]-}"
if [[ -z "${SEQFILE}" ]]; then
  echo "ERROR: could not find state/${SCENARIO}/initiator/*.senderseqnums"
  echo "Contents of state/${SCENARIO}/initiator (if any):"
  ls -la "$ROOT_DIR/state/${SCENARIO}/initiator" 2>/dev/null || true
  echo "Check that the initiator ran with FileStorePath=state/${SCENARIO}/initiator and cwd /work in Docker."
  $DOCKER_BIN rm -f fix-acceptor >/dev/null 2>&1 || true
  exit 1
fi

python3 - "$SEQFILE" <<'PY'
"""Bump next sender seq in QuickFIX/J FileStore *.senderseqnums (Java writeUTF / readUTF)."""
import struct
import sys

path = sys.argv[1]
bump = 5
with open(path, "rb") as f:
    raw = f.read()
if len(raw) < 2:
    raise SystemExit(f"{path}: file too short (empty store?)")
(nbytes,) = struct.unpack_from(">H", raw, 0)
if len(raw) < 2 + nbytes:
    raise SystemExit(f"{path}: truncated writeUTF payload")
s = raw[2 : 2 + nbytes].decode("ascii", errors="strict").strip()
cur = int(s)
new_val = cur + bump
out = str(new_val).encode("ascii")
with open(path, "wb") as f:
    f.write(struct.pack(">H", len(out)))
    f.write(out)
print(f"Updated {path}: sender seq {cur} -> {new_val} (+{bump})")
PY

echo "==> Re-running initiator; acceptor should emit ResendRequest and initiator should gap-fill"
$DOCKER_BIN run --rm --name fix-initiator --network host \
  "${DOCKER_USER_ARGS[@]}" \
  -v "$ROOT_DIR":/work -w /work \
  -e MAVEN_CONFIG=/work/.m2 \
  maven:3-eclipse-temurin-21 \
  java -cp "/work/${JAR}" fixsim.HeadlessInitiator /work/configs/initiator_seq_gap.cfg || true

echo "==> Stopping acceptor"
$DOCKER_BIN rm -f fix-acceptor >/dev/null

echo "==> Logs written to:"
echo "  logs/${SCENARIO}/acceptor/"
echo "  logs/${SCENARIO}/initiator/"

