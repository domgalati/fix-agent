#!/usr/bin/env python3
"""
parse_fix_logs.py

Convert QuickFIX/J FileLog output into JSONL (one FIX message per line).

Usage:
  python tools/parse_fix_logs.py
  python tools/parse_fix_logs.py --scenario seq_gap

Input:
  A logs directory tree containing QuickFIX/J FileLog files.
  This repo commonly stores them under `fix-sim/logs/<scenario>/.../*.messages.log`.

Output:
  `dataset.jsonl` in the repo root (append mode).

JSONL schema (one dict per line):
{
  "ts": "2026-04-24T14:22:17.123Z",
  "scenario": "happy_path",
  "direction": "in" | "out",
  "session": "FIX.4.4|TRADER|VENUE",
  "raw": "8=FIX.4.4\\x0135=A\\x0134=1\\x0149=TRADER\\x0156=VENUE\\x01...",
  "tags": {"8": "FIX.4.4", "35": "A", "34": "1", "49": "TRADER", ...},
  "msg_type": "A",
  "seq_num": 1
}
"""

from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional


SOH = "\x01"

# Examples seen in this repo:
# - "8=FIX.4.4<SOH>9=...<SOH>..."
# - "20260424-06:33:27.654: 8=FIX.4.4<SOH>..."
# Spec mentions: "[timestamp] direction message"
_BRACKET_TS_RE = re.compile(r"^\[(?P<ts>[^\]]+)\]\s+(?P<rest>.*)$")
_PLAIN_TS_RE = re.compile(
    r"^(?P<ts>\d{8}-\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?)\s*:\s*(?P<rest>.*)$"
)
_FIX_START_RE = re.compile(r"8=FIX\.[0-9]\.[0-9]")


@dataclass(frozen=True)
class FileContext:
    scenario: str
    # "Local" session as encoded by the log filename, if present.
    local_begin: Optional[str]
    local_sender: Optional[str]
    local_target: Optional[str]


def _parse_fix_datetime_to_iso_z(s: str) -> Optional[str]:
    """
    Parse FIX-style timestamps like:
      - YYYYMMDD-HH:MM:SS
      - YYYYMMDD-HH:MM:SS.sss (or more fractional digits)
    and return ISO8601 with Z (UTC).
    """
    s = s.strip()
    for fmt in ("%Y%m%d-%H:%M:%S.%f", "%Y%m%d-%H:%M:%S"):
        try:
            dt = datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
            # Keep milliseconds when present, otherwise seconds.
            if dt.microsecond:
                return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")
            return dt.isoformat(timespec="seconds").replace("+00:00", "Z")
        except ValueError:
            pass
    return None


def _parse_any_timestamp_to_iso_z(ts: str) -> Optional[str]:
    """
    Accept a few common timestamp formats found in QuickFIX/J logs and normalize to ISO8601 Z.
    """
    ts = ts.strip()
    # First: FIX timestamp format commonly used in these logs.
    iso = _parse_fix_datetime_to_iso_z(ts)
    if iso:
        return iso

    # Next: ISO-ish inputs that datetime.fromisoformat can handle (without Z).
    try:
        candidate = ts.replace("Z", "+00:00")
        dt = datetime.fromisoformat(candidate)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        if dt.microsecond:
            return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")
        return dt.isoformat(timespec="seconds").replace("+00:00", "Z")
    except ValueError:
        return None


def _normalize_delimiters(raw: str) -> str:
    """
    Normalize common SOH placeholders to literal SOH (\x01).

    We do NOT add or remove trailing delimiters; we only rewrite placeholder encodings.
    """
    # Literal escape sequences that might appear if logs were pre-escaped.
    raw = raw.replace("\\x01", SOH)
    raw = raw.replace("\\001", SOH)
    raw = raw.replace("\\u0001", SOH)
    # Human-friendly placeholder sometimes used in FIX dumps.
    if SOH not in raw and "|" in raw:
        raw = raw.replace("|", SOH)
    return raw


def _extract_fix_payload(line: str) -> Optional[str]:
    """
    Extract the FIX message substring starting from '8=FIX.x.y'.
    Returns None if no FIX message is present on the line.
    """
    m = _FIX_START_RE.search(line)
    if not m:
        return None
    return line[m.start() :].rstrip("\r\n")


def _parse_direction_token(token: str) -> Optional[str]:
    t = token.strip().lower()
    if t in {"in", "incoming", "recv", "receive", "received"}:
        return "in"
    if t in {"out", "outgoing", "send", "sent", "sending"}:
        return "out"
    return None


def _parse_line_prefix(line: str) -> tuple[Optional[str], Optional[str], str]:
    """
    Parse and remove QuickFIX/J-ish prefixes.

    Returns:
      (ts_iso_z, direction, remainder_line)
    Where remainder_line still contains the FIX payload (somewhere).
    """
    s = line.strip("\r\n")
    if not s.strip():
        return None, None, ""

    # [timestamp] direction message
    m = _BRACKET_TS_RE.match(s)
    if m:
        ts_iso = _parse_any_timestamp_to_iso_z(m.group("ts"))
        rest = m.group("rest").lstrip()
        parts = rest.split(None, 1)
        if len(parts) == 2:
            dir_ = _parse_direction_token(parts[0])
            if dir_:
                return ts_iso, dir_, parts[1]
        return ts_iso, None, rest

    # timestamp: message
    m = _PLAIN_TS_RE.match(s)
    if m:
        ts_iso = _parse_any_timestamp_to_iso_z(m.group("ts"))
        rest = m.group("rest").lstrip()
        # Some formats include a direction token right after timestamp.
        parts = rest.split(None, 1)
        if len(parts) == 2:
            dir_ = _parse_direction_token(parts[0])
            if dir_:
                return ts_iso, dir_, parts[1]
        return ts_iso, None, rest

    # No recognizable prefix.
    return None, None, s


def _parse_tags(raw_fix: str) -> dict[str, str]:
    tags: dict[str, str] = {}
    for field in raw_fix.split(SOH):
        if not field:
            continue
        if "=" not in field:
            continue
        k, v = field.split("=", 1)
        if k:
            tags[k] = v
    return tags


def _scenario_from_path(logs_root: Path, path: Path) -> str:
    """
    Identify scenario name from directory structure:
      <logs_root>/<scenario>/...
    """
    try:
        rel = path.relative_to(logs_root)
        parts = rel.parts
        if parts:
            return parts[0]
    except ValueError:
        pass
    return "unknown"


def _file_local_session_from_name(path: Path) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Parse the local session from FileLog filenames like:
      FIX.4.4-TRADER-VENUE.messages.log
    """
    name = path.name
    m = re.match(r"^(FIX\.\d\.\d)-([^-]+)-([^.]+)\.", name)
    if not m:
        return None, None, None
    return m.group(1), m.group(2), m.group(3)


def _infer_direction_from_file_and_tags(ctx: FileContext, tags: dict[str, str]) -> Optional[str]:
    """
    Infer direction by comparing tag 49/56 to the log file's local session naming.

    If message appears to be sent by the local sender->target pair: out
    If reversed: in
    """
    if not (ctx.local_sender and ctx.local_target):
        return None
    sender = tags.get("49")
    target = tags.get("56")
    if not sender or not target:
        return None
    if sender == ctx.local_sender and target == ctx.local_target:
        return "out"
    if sender == ctx.local_target and target == ctx.local_sender:
        return "in"
    return None


def _iter_message_log_files(logs_root: Path) -> Iterable[Path]:
    """
    Prefer QuickFIX/J message logs, but fall back to any *.log if needed.
    """
    msg_logs = sorted(logs_root.rglob("*.messages.log"))
    if msg_logs:
        yield from msg_logs
        return
    yield from sorted(logs_root.rglob("*.log"))


def _select_logs_root(repo_root: Path) -> Path:
    """
    Choose logs root.
    - Prefer ./logs if present (matches requested interface)
    - Otherwise use ./fix-sim/logs (matches this repo's fixture layout)
    """
    preferred = repo_root / "logs"
    if preferred.exists() and preferred.is_dir():
        return preferred
    fallback = repo_root / "fix-sim" / "logs"
    if fallback.exists() and fallback.is_dir():
        return fallback
    raise SystemExit(f"Could not find logs root at {preferred} or {fallback}")


def _repo_root_from_this_file() -> Path:
    """
    Find the repository root regardless of where this script lives.

    We detect the root by walking upwards until we find a directory that contains
    the `fix-sim/` folder (the canonical location of logs in this repo).
    """
    start = Path(__file__).resolve()
    for p in [start.parent, *start.parents]:
        if (p / "fix-sim").is_dir():
            return p
    # Fallback to a reasonable default (old behavior).
    return start.parents[2] if len(start.parents) > 2 else start.parent


def main() -> int:
    ap = argparse.ArgumentParser(description="Convert QuickFIX/J FileLog output into JSONL.")
    ap.add_argument(
        "--scenario",
        help="Only include this scenario name (e.g. seq_gap). Default: include all scenarios.",
    )
    args = ap.parse_args()

    repo_root = _repo_root_from_this_file()
    logs_root = _select_logs_root(repo_root)
    out_path = repo_root / "dataset.jsonl"

    scenario_filter = args.scenario
    wrote = 0

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("a", encoding="utf-8") as out:
        for log_file in _iter_message_log_files(logs_root):
            scenario = _scenario_from_path(logs_root, log_file)
            if scenario_filter and scenario != scenario_filter:
                continue

            local_begin, local_sender, local_target = _file_local_session_from_name(log_file)
            ctx = FileContext(
                scenario=scenario,
                local_begin=local_begin,
                local_sender=local_sender,
                local_target=local_target,
            )

            # Read as text but keep odd bytes stable.
            with log_file.open("r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    ts_iso, direction, remainder = _parse_line_prefix(line)
                    if not remainder:
                        continue

                    payload = _extract_fix_payload(remainder)
                    if not payload:
                        # Skip headers / event lines / non-message logs.
                        continue

                    raw = _normalize_delimiters(payload)
                    tags = _parse_tags(raw)
                    if not tags:
                        continue

                    # Fill missing ts from tag 52 (SendingTime) when possible.
                    if ts_iso is None:
                        ts_iso = _parse_fix_datetime_to_iso_z(tags.get("52", "")) if tags.get("52") else None
                    if ts_iso is None:
                        ts_iso = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")

                    if direction is None:
                        direction = _infer_direction_from_file_and_tags(ctx, tags)

                    msg_type = tags.get("35")
                    seq_raw = tags.get("34")
                    try:
                        seq_num = int(seq_raw) if seq_raw is not None else None
                    except ValueError:
                        seq_num = None

                    session = "|".join([tags.get("8", ""), tags.get("49", ""), tags.get("56", "")])

                    obj = {
                        "ts": ts_iso,
                        "scenario": ctx.scenario,
                        "direction": direction,
                        "session": session,
                        "raw": raw,
                        "tags": tags,
                        "msg_type": msg_type,
                        "seq_num": seq_num,
                    }

                    out.write(json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n")
                    wrote += 1

    print(f"Appended {wrote} messages to {out_path} (logs root: {logs_root})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

