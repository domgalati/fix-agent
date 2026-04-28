#!/usr/bin/env python3
"""
State reconstructor for parsed FIX logs.

Input: JSONL where each line is a parsed FIX message produced by parse_fix_logs.py
(one object per line with keys like ts, scenario, direction, session, tags, msg_type, seq_num).

Output: Session-level JSONL summaries (one object per session) with anomaly detection.
"""

from __future__ import annotations

import argparse
import json
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterator, Iterable, List, Optional, Tuple


TRADER_PERSPECTIVE_SESSION_TUPLE = "FIX.4.4|TRADER|VENUE"


def _parse_ts(ts: str) -> datetime:
    """Parse an ISO8601 timestamp into an aware datetime (UTC)."""
    # dataset uses trailing "Z"
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_sending_time(s52: str) -> Optional[datetime]:
    """Parse FIX tag 52 SendingTime into an aware datetime (UTC)."""
    try:
        dt = datetime.strptime(s52, "%Y%m%d-%H:%M:%S.%f")
    except (TypeError, ValueError):
        return None
    return dt.replace(tzinfo=timezone.utc)


def _safe_int(v: object) -> Optional[int]:
    """Best-effort int conversion for FIX tag values."""
    if v is None:
        return None
    try:
        return int(v)  # type: ignore[arg-type]
    except Exception:
        return None


def load_messages(jsonl_path: str, perspective_filter: str) -> Iterator[dict]:
    """
    Load parsed FIX messages from a JSONL file.

    Only yields messages matching `perspective_filter` in the `session` field.

    We intentionally keep a single tuple perspective only and ignore mirrored
    counterparty tuples entirely. This matches production reality where only one
    side's logs are typically available.

    Note: This matches production reality where only one side's logs are typically available.
    """
    with open(jsonl_path, "r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError as e:
                raise ValueError(f"Invalid JSON at line {line_no}: {e}") from e

            if msg.get("session") != perspective_filter:
                continue

            yield msg


def segment_into_sessions(messages: Iterable[dict]) -> Iterator[List[dict]]:
    """
    Segment messages into logical sessions.

    Design decision (Fork A): A "session" is one logon-to-logoff cycle.
    - Start: 35=A (Logon)
    - End: 35=5 (Logout) OR when a new 35=A starts a new cycle on the same session tuple.

    Sessions are assigned synthetic IDs like "FIX.4.4|TRADER|VENUE#1", "#2", ...

    IMPORTANT: Future work will need to support incidents spanning multiple sessions
    (e.g., a bad counterparty release affecting every reconnection). We keep
    `session_tuple` + `session_id` in outputs so cross-session queries are possible later,
    but we do not implement cross-session correlation in this first pass.
    """
    # Support multiple session tuples, even though we filter to one tuple today.
    counters: Dict[str, int] = defaultdict(int)
    open_sessions: Dict[str, List[dict]] = {}

    def _new_session_id(session_tuple: str) -> str:
        counters[session_tuple] += 1
        return f"{session_tuple}#{counters[session_tuple]}"

    for msg in messages:
        session_tuple = msg.get("session")
        msg_type = msg.get("msg_type")

        if not session_tuple:
            continue

        direction = msg.get("direction")

        if msg_type == "A":
            # Close any existing session on this tuple.
            if session_tuple in open_sessions and open_sessions[session_tuple]:
                yield open_sessions.pop(session_tuple)

            sid = _new_session_id(session_tuple)
            msg = dict(msg)
            msg["__session_id"] = sid
            open_sessions[session_tuple] = [msg]
            continue

        # Ignore pre-logon noise for this first pass.
        if session_tuple not in open_sessions:
            continue

        msg = dict(msg)
        msg["__session_id"] = open_sessions[session_tuple][0].get("__session_id")
        open_sessions[session_tuple].append(msg)

        if msg_type == "5":
            yield open_sessions.pop(session_tuple)

    # Close any remaining open sessions at EOF.
    for session_tuple, sess_msgs in list(open_sessions.items()):
        if sess_msgs:
            yield sess_msgs
        open_sessions.pop(session_tuple, None)


@dataclass
class _PendingGap:
    expected_seq: int
    actual_seq: int
    detected_ts: str


def _detect_seq_gaps_and_finals(messages: List[dict]) -> Tuple[List[dict], int, int]:
    """
    Detect sequence anomalies (gaps/duplicates/out-of-order) and return final seq state.

    Implements the "Sequence Gap Detection Logic" for inbound/outbound streams.
    """
    expected_in = 1
    expected_out = 1

    anomalies: List[dict] = []

    pending_in_gap: Optional[_PendingGap] = None
    pending_out_gap: Optional[_PendingGap] = None
    saw_first_inbound = False
    saw_first_outbound = False
    last_inbound_seq: Optional[int] = None
    last_outbound_seq: Optional[int] = None

    for msg in messages:
        direction = msg.get("direction")
        msg_type = msg.get("msg_type")
        seq = msg.get("seq_num")
        tags = msg.get("tags") or {}

        if not isinstance(seq, int):
            continue

        # Inbound = messages coming into TRADER (VENUE -> TRADER)
        if direction == "in":
            if not saw_first_inbound:
                saw_first_inbound = True
                last_inbound_seq = seq
                if seq > 1:
                    anomalies.append(
                        {
                            "type": "session_started_at_elevated_seq",
                            "detected_ts": msg["ts"],
                            "actual_seq": seq,
                            "note": "Counterparty logged on with seq > 1; possible stale state on their side",
                        }
                    )
                expected_in = seq + 1
                continue

            if msg_type == "4":
                new_seq_no = _safe_int(tags.get("36"))
                if pending_in_gap and new_seq_no is not None and new_seq_no >= pending_in_gap.actual_seq:
                    gapfill_flag = str(tags.get("123", "")).upper()
                    recovery_method = "sequence_reset_gapfill" if gapfill_flag == "Y" else "sequence_reset"
                    for a in reversed(anomalies):
                        if (
                            a.get("type") == "seq_gap_inbound"
                            and a.get("detected_ts") == pending_in_gap.detected_ts
                            and a.get("expected_seq") == pending_in_gap.expected_seq
                            and a.get("actual_seq") == pending_in_gap.actual_seq
                        ):
                            a["recovered"] = True
                            a["recovery_ts"] = msg["ts"]
                            ttr = _parse_ts(msg["ts"]) - _parse_ts(pending_in_gap.detected_ts)
                            a["time_to_recovery_sec"] = round(ttr.total_seconds(), 6)
                            a["recovery_method"] = recovery_method
                            break
                    pending_in_gap = None

                # SequenceReset/GapFill does not participate in normal seq comparison.
                if new_seq_no is not None:
                    expected_in = new_seq_no
                last_inbound_seq = seq
                continue

            if seq == expected_in:
                expected_in += 1
            elif seq > expected_in:
                # GAP DETECTED (inbound)
                pending_in_gap = _PendingGap(expected_seq=expected_in, actual_seq=seq, detected_ts=msg["ts"])
                anomalies.append(
                    {
                        "type": "seq_gap_inbound",
                        "detected_ts": msg["ts"],
                        "expected_seq": expected_in,
                        "actual_seq": seq,
                        "missing_count": seq - expected_in,
                        "recovered": False,
                        "recovery_ts": None,
                        "time_to_recovery_sec": None,
                        "recovery_method": None,
                    }
                )
                expected_in = seq + 1
            else:
                anomalies.append(
                    {
                        "type": "seq_num_lt_expected_inbound",
                        "detected_ts": msg["ts"],
                        "expected_seq": expected_in,
                        "actual_seq": seq,
                    }
                )

            last_inbound_seq = seq

        # Outbound = messages sent by TRADER (TRADER -> VENUE)
        elif direction == "out":
            if not saw_first_outbound:
                saw_first_outbound = True
                expected_out = seq + 1
                last_outbound_seq = seq
                continue

            if msg_type == "4":
                new_seq_no = _safe_int(tags.get("36"))
                if new_seq_no is not None:
                    expected_out = new_seq_no
                last_outbound_seq = seq
                continue

            if seq == expected_out:
                expected_out += 1
            elif seq > expected_out:
                pending_out_gap = _PendingGap(expected_seq=expected_out, actual_seq=seq, detected_ts=msg["ts"])
                anomalies.append(
                    {
                        "type": "seq_gap_outbound",
                        "detected_ts": msg["ts"],
                        "expected_seq": expected_out,
                        "actual_seq": seq,
                        "missing_count": seq - expected_out,
                        "recovered": False,
                        "recovery_ts": None,
                        "time_to_recovery_sec": None,
                        "recovery_method": None,
                    }
                )
                expected_out = seq + 1
            else:
                anomalies.append(
                    {
                        "type": "seq_num_lt_expected_outbound",
                        "detected_ts": msg["ts"],
                        "expected_seq": expected_out,
                        "actual_seq": seq,
                    }
                )

            last_outbound_seq = seq

        # Same logic for outbound recovery, but we watch for a ResendRequest (35=2)
        # FROM the other side as the trigger (this side detected a gap from us).
        if pending_out_gap and msg.get("direction") == "in" and msg_type == "2":
            begin_seq_no = _safe_int((msg.get("tags") or {}).get("7"))
            end_seq_no = _safe_int((msg.get("tags") or {}).get("16"))
            # If the ResendRequest range overlaps the gap's missing region, treat it as "recovered"/noticed.
            missing_start = pending_out_gap.expected_seq
            missing_end = pending_out_gap.actual_seq - 1
            if begin_seq_no is None:
                begin_seq_no = missing_start
            if end_seq_no is None or end_seq_no == 0:
                end_seq_no = missing_end
            if begin_seq_no <= missing_end and end_seq_no >= missing_start:
                for a in reversed(anomalies):
                    if (
                        a.get("type") == "seq_gap_outbound"
                        and a.get("detected_ts") == pending_out_gap.detected_ts
                        and a.get("expected_seq") == pending_out_gap.expected_seq
                        and a.get("actual_seq") == pending_out_gap.actual_seq
                    ):
                        a["recovered"] = True
                        a["recovery_ts"] = msg["ts"]
                        ttr = _parse_ts(msg["ts"]) - _parse_ts(pending_out_gap.detected_ts)
                        a["time_to_recovery_sec"] = round(ttr.total_seconds(), 6)
                        a["recovery_method"] = "resend_request"
                        a["resend_begin_seq_no"] = begin_seq_no
                        a["resend_end_seq_no"] = end_seq_no
                        break
                pending_out_gap = None

    final_in = last_inbound_seq or 0
    final_out = last_outbound_seq or 0
    return anomalies, final_in, final_out


def detect_seq_gaps(messages: List[dict]) -> List[dict]:
    """
    Detect and return the list of anomalies for a session.

    (This wrapper exists to match the required function signature; the session reconstructor
    also uses the final sequence values from the shared implementation.)
    """
    anomalies, _, _ = _detect_seq_gaps_and_finals(messages)
    return anomalies


def detect_heartbeat_anomalies(messages: List[dict]) -> List[dict]:
    """
    Detect heartbeat timing anomalies (intervals exceeding HeartBtInt + tolerance).
    Tracks inbound (counterparty -> us) and outbound (us -> counterparty)
    heartbeat streams independently.
    """
    if not messages:
        return []

    # 1) Read HeartBtInt from the first Logon in the session.
    first_logon = next((m for m in messages if m.get("msg_type") == "A"), None)
    if first_logon is None:
        # No Logon means we cannot anchor; treat as no heartbeat analysis for now.
        return []

    hb_int = _safe_int((first_logon.get("tags") or {}).get("108"))
    anomalies: List[dict] = []
    if hb_int is None:
        hb_int = 30
        anomalies.append(
            {
                "type": "heartbeat_interval_unspecified",
                "detected_ts": first_logon.get("ts"),
                "note": "HeartBtInt (tag 108) not found in Logon; defaulting to 30s",
            }
        )

    # 2) HeartBtInt + 2.0 seconds tolerance.
    expected_within = float(hb_int) + 2.0

    # 3) Anchor starts at Logon timestamp in that direction, not first heartbeat.
    first_in_logon = next((m for m in messages if m.get("msg_type") == "A" and m.get("direction") == "in"), None)
    first_out_logon = next((m for m in messages if m.get("msg_type") == "A" and m.get("direction") == "out"), None)

    anchor_in_ts = (first_in_logon or first_logon).get("ts")
    anchor_out_ts = (first_out_logon or first_logon).get("ts")
    if not anchor_in_ts or not anchor_out_ts:
        return anomalies

    last_in_hb_ts: str = anchor_in_ts
    last_out_hb_ts: str = anchor_out_ts
    in_silence_emitted = False
    out_silence_emitted = False

    for msg in messages:
        ts = msg.get("ts")
        if not ts:
            continue
        now = _parse_ts(ts)

        # 4) On every message, check time since last heartbeat (or anchor) per direction.
        if not in_silence_emitted:
            gap = (now - _parse_ts(last_in_hb_ts)).total_seconds()
            if gap > expected_within:
                anomalies.append(
                    {
                        "type": "heartbeat_gap_inbound",
                        "detected_ts": ts,
                        "expected_within_sec": expected_within,
                        "actual_gap_sec": round(gap, 3),
                        "last_heartbeat_ts": last_in_hb_ts,
                        "note": "Heartbeat interval exceeded threshold; counterparty may be silent",
                    }
                )
                in_silence_emitted = True

        if not out_silence_emitted:
            gap = (now - _parse_ts(last_out_hb_ts)).total_seconds()
            if gap > expected_within:
                anomalies.append(
                    {
                        "type": "heartbeat_gap_outbound",
                        "detected_ts": ts,
                        "expected_within_sec": expected_within,
                        "actual_gap_sec": round(gap, 3),
                        "last_heartbeat_ts": last_out_hb_ts,
                        "note": "Heartbeat interval exceeded threshold; counterparty may be silent",
                    }
                )
                out_silence_emitted = True

        # 5) Reset anchors only when a new heartbeat (35=0) or logon (35=A) appears in that direction.
        msg_type = msg.get("msg_type")
        direction = msg.get("direction")
        if msg_type in {"0", "A"}:
            if direction == "in":
                last_in_hb_ts = ts
                in_silence_emitted = False
            elif direction == "out":
                last_out_hb_ts = ts
                out_silence_emitted = False

        # 6) TestRequest (35=1) does not count as a heartbeat (handled by the reset condition above).

    return anomalies


def detect_test_request_anomalies(messages: List[dict]) -> List[dict]:
    """
    Correlate TestRequest (35=1) with Heartbeat (35=0) responses by TestReqID (tag 112).
    Tracks both directions independently:
      - outbound TestRequest (TRADER -> VENUE): expects inbound Heartbeat reply
      - inbound TestRequest (VENUE -> TRADER): expects outbound Heartbeat reply
    """
    if not messages:
        return []

    first_logon = next((m for m in messages if m.get("msg_type") == "A"), None)
    if first_logon is None:
        return []

    hb_int = _safe_int((first_logon.get("tags") or {}).get("108"))
    if hb_int is None:
        hb_int = 30
    expected_within = float(hb_int) + 2.0

    pending_outbound_testreq: Dict[str, Tuple[str, bool]] = {}
    pending_inbound_testreq: Dict[str, Tuple[str, bool]] = {}
    anomalies: List[dict] = []

    for msg in messages:
        msg_type = msg.get("msg_type")
        direction = msg.get("direction")
        ts = msg.get("ts")
        if not ts:
            continue

        tags = msg.get("tags") or {}

        if msg_type == "1":
            raw_test_req_id = tags.get("112")
            fallback_used = raw_test_req_id in (None, "")
            test_req_id = str(raw_test_req_id if raw_test_req_id not in (None, "") else msg.get("seq_num"))
            if direction == "out":
                pending_outbound_testreq[test_req_id] = (ts, fallback_used)
            elif direction == "in":
                pending_inbound_testreq[test_req_id] = (ts, fallback_used)
            continue

        if msg_type != "0":
            continue

        hb_test_req_id = tags.get("112")
        if hb_test_req_id in (None, ""):
            # Normal heartbeat, not a TestRequest response.
            continue

        test_req_id = str(hb_test_req_id)
        if direction == "in" and test_req_id in pending_outbound_testreq:
            detected_ts, fallback_used = pending_outbound_testreq.pop(test_req_id)
            response_time = (_parse_ts(ts) - _parse_ts(detected_ts)).total_seconds()
            if response_time > expected_within:
                note = "TestRequest answered, but response time exceeded threshold"
                if fallback_used:
                    note += " TestReqID missing; using seq_num as fallback."
                anomalies.append(
                    {
                        "type": "slow_test_request_response_outbound",
                        "detected_ts": detected_ts,
                        "test_req_id": test_req_id,
                        "response_ts": ts,
                        "response_time_sec": round(response_time, 3),
                        "expected_within_sec": expected_within,
                        "note": note,
                    }
                )
        elif direction == "out" and test_req_id in pending_inbound_testreq:
            detected_ts, fallback_used = pending_inbound_testreq.pop(test_req_id)
            response_time = (_parse_ts(ts) - _parse_ts(detected_ts)).total_seconds()
            if response_time > expected_within:
                note = "TestRequest answered, but response time exceeded threshold"
                if fallback_used:
                    note += " TestReqID missing; using seq_num as fallback."
                anomalies.append(
                    {
                        "type": "slow_test_request_response_inbound",
                        "detected_ts": detected_ts,
                        "test_req_id": test_req_id,
                        "response_ts": ts,
                        "response_time_sec": round(response_time, 3),
                        "expected_within_sec": expected_within,
                        "note": note,
                    }
                )

    for test_req_id, (detected_ts, fallback_used) in pending_outbound_testreq.items():
        note = (
            "TestRequest sent but no matching Heartbeat received before session end; "
            "strong signal of one-way network failure or counterparty unresponsive"
        )
        if fallback_used:
            note += " TestReqID missing; using seq_num as fallback."
        anomalies.append(
            {
                "type": "unanswered_test_request_outbound",
                "detected_ts": detected_ts,
                "test_req_id": test_req_id,
                "expected_within_sec": expected_within,
                "note": note,
            }
        )

    for test_req_id, (detected_ts, fallback_used) in pending_inbound_testreq.items():
        note = (
            "TestRequest sent but no matching Heartbeat received before session end; "
            "strong signal of one-way network failure or counterparty unresponsive"
        )
        if fallback_used:
            note += " TestReqID missing; using seq_num as fallback."
        anomalies.append(
            {
                "type": "unanswered_test_request_inbound",
                "detected_ts": detected_ts,
                "test_req_id": test_req_id,
                "expected_within_sec": expected_within,
                "note": note,
            }
        )

    return anomalies


def detect_timestamp_latency_anomalies(messages: List[dict]) -> List[dict]:
    """
    Detect inbound messages with elevated transmission latency vs session baseline.
    Latency = ts (when we logged) - tag 52 SendingTime (when sender created).
    """
    inbound_latencies: List[Tuple[dict, float]] = []

    for msg in messages:
        if msg.get("direction") != "in":
            continue
        tags = msg.get("tags") or {}
        sending_time_raw = tags.get("52")
        ts = msg.get("ts")
        if not sending_time_raw or not ts:
            continue
        sending_time = _parse_sending_time(str(sending_time_raw))
        if sending_time is None:
            continue
        latency_sec = (_parse_ts(ts) - sending_time).total_seconds()
        inbound_latencies.append((msg, latency_sec))

    if len(inbound_latencies) < 3:
        return []

    baseline_samples = [lat for _, lat in inbound_latencies[:3]]
    baseline_mean = statistics.mean(baseline_samples)
    baseline_stdev = statistics.stdev(baseline_samples)
    baseline_stdev = max(baseline_stdev, 0.001)

    anomalies: List[dict] = []
    for msg, latency_sec in inbound_latencies[3:]:
        z_score = (latency_sec - baseline_mean) / baseline_stdev
        if abs(z_score) <= 3.0:
            continue

        note = (
            "Inbound message latency exceeds 3-sigma vs session baseline; "
            "possible network degradation or clock drift"
        )
        if len(anomalies) == 4:
            note += " (further outliers in session not reported)"

        anomalies.append(
            {
                "type": "elevated_inbound_latency",
                "detected_ts": msg.get("ts"),
                "latency_sec": round(latency_sec, 6),
                "baseline_mean_sec": round(baseline_mean, 6),
                "baseline_stdev_sec": round(baseline_stdev, 6),
                "z_score": round(z_score, 2),
                "msg_type": msg.get("msg_type"),
                "seq_num": msg.get("seq_num"),
                "note": note,
            }
        )

        if len(anomalies) >= 5:
            break

    return anomalies


def reconstruct_session(messages: List[dict]) -> dict:
    """
    Reconstruct session state from its messages and emit a session-level summary dict.
    """
    if not messages:
        raise ValueError("Cannot reconstruct empty session")

    session_id = messages[0].get("__session_id")
    session_tuple = messages[0].get("session")

    scenario = None
    for m in messages:
        if m.get("scenario"):
            scenario = m["scenario"]
            break

    logon_ts = None
    logoff_ts = None
    for m in messages:
        if m.get("msg_type") == "A" and logon_ts is None:
            logon_ts = m.get("ts")
        if m.get("msg_type") == "5":
            logoff_ts = m.get("ts")
    if logon_ts is None:
        logon_ts = messages[0].get("ts")
    if logoff_ts is None:
        logoff_ts = messages[-1].get("ts")

    duration_sec = None
    try:
        duration_sec = round((_parse_ts(logoff_ts) - _parse_ts(logon_ts)).total_seconds(), 6)  # type: ignore[arg-type]
    except Exception:
        duration_sec = None

    by_type = Counter(m.get("msg_type") for m in messages if m.get("msg_type"))

    seq_anoms, final_in, final_out = _detect_seq_gaps_and_finals(messages)
    hb_anoms = detect_heartbeat_anomalies(messages)
    tr_anoms = detect_test_request_anomalies(messages)
    latency_anoms = detect_timestamp_latency_anomalies(messages)
    anomalies = list(seq_anoms) + list(hb_anoms) + list(tr_anoms) + list(latency_anoms)
    anomalies.sort(key=lambda a: _parse_ts(a.get("detected_ts") or "1970-01-01T00:00:00Z"))

    return {
        "session_id": session_id,
        "session_tuple": session_tuple,
        "scenario": scenario,
        "logon_ts": logon_ts,
        "logoff_ts": logoff_ts,
        "duration_sec": duration_sec,
        "messages_total": len(messages),
        "messages_by_type": dict(by_type),
        "final_inbound_seq": final_in,
        "final_outbound_seq": final_out,
        "anomalies": anomalies,
    }


def _has_outbound_messages(messages: List[dict]) -> bool:
    """Return True if the session includes at least one outbound message."""
    return any(m.get("direction") == "out" and isinstance(m.get("seq_num"), int) for m in messages)


# --- TODOs (future work; structure only, not implemented in this pass) ---
#
# def reject_ratios(messages: List[dict]) -> dict:
#     """TODO: Track Reject/BusinessReject ratios (35=3/35=j) per session."""
#     raise NotImplementedError
#
# def zombie_session_detection(messages: List[dict]) -> dict:
#     """TODO: Detect sessions that remain "logged on" but are non-functional."""
#     raise NotImplementedError


def _format_table(rows: List[Tuple[str, int, int, int, int, int, int, int, int]]) -> str:
    """Format a simple fixed-width table for verify output."""
    headers = (
        "scenario",
        "sessions",
        "clean",
        "with_anomalies",
        "seq_anomalies",
        "heartbeat_anomalies",
        "test_request_anomalies",
        "latency_anomalies",
        "total_anomalies",
    )
    col_widths = [len(h) for h in headers]
    for r in rows:
        for i, cell in enumerate(r):
            col_widths[i] = max(col_widths[i], len(str(cell)))

    def fmt_row(values: Tuple[object, ...]) -> str:
        parts = []
        for i, v in enumerate(values):
            s = str(v)
            if i == 0:
                parts.append(s.ljust(col_widths[i]))
            else:
                parts.append(s.rjust(col_widths[i]))
        return "  ".join(parts)

    out = [fmt_row(headers)]
    out.append(fmt_row(tuple("-" * w for w in col_widths)))
    for r in rows:
        out.append(fmt_row(r))
    return "\n".join(out)


def main() -> int:
    """CLI entrypoint."""
    parser = argparse.ArgumentParser(description="Reconstruct session-level FIX state from dataset.jsonl")
    parser.add_argument("--input", default="dataset.jsonl", help="Input JSONL path (default: dataset.jsonl)")
    parser.add_argument("--output", default="sessions.jsonl", help="Output JSONL path (default: sessions.jsonl)")
    parser.add_argument(
        "--perspective-session",
        default=TRADER_PERSPECTIVE_SESSION_TUPLE,
        help=f"Session tuple to keep (default: {TRADER_PERSPECTIVE_SESSION_TUPLE})",
    )
    parser.add_argument("--verify", action="store_true", help="Run reconstructor and print scenario summary table")
    args = parser.parse_args()

    input_path = str(args.input)
    output_path = str(args.output)

    msgs = load_messages(input_path, perspective_filter=str(args.perspective_session))
    sessions_iter = segment_into_sessions(msgs)

    summaries: List[dict] = []
    out_file = Path(output_path)
    with open(out_file, "w", encoding="utf-8") as out:
        for sess_msgs in sessions_iter:
            summary = reconstruct_session(sess_msgs)
            out.write(json.dumps(summary, sort_keys=True) + "\n")
            summaries.append(summary)

    if args.verify:
        by_scenario: Dict[str, List[dict]] = defaultdict(list)
        for s in summaries:
            by_scenario[str(s.get("scenario"))].append(s)

        rows: List[Tuple[str, int, int, int, int, int, int, int, int]] = []
        total_failures: List[str] = []

        seq_types = {
            "seq_gap_inbound",
            "seq_gap_outbound",
            "seq_num_lt_expected_inbound",
            "seq_num_lt_expected_outbound",
            "session_started_at_elevated_seq",
        }
        heartbeat_types = {
            "heartbeat_gap_inbound",
            "heartbeat_gap_outbound",
            "heartbeat_interval_unspecified",
        }
        test_request_types = {
            "unanswered_test_request_inbound",
            "unanswered_test_request_outbound",
            "slow_test_request_response_inbound",
            "slow_test_request_response_outbound",
        }
        latency_types = {"elevated_inbound_latency"}

        for scenario in sorted(by_scenario.keys()):
            sess = by_scenario[scenario]
            sessions_n = len(sess)
            clean_n = sum(1 for s in sess if not s.get("anomalies"))
            with_anom_n = sessions_n - clean_n
            seq_anoms = sum(
                1
                for s in sess
                for a in (s.get("anomalies") or [])
                if a.get("type") in seq_types
            )
            heartbeat_anoms = sum(
                1
                for s in sess
                for a in (s.get("anomalies") or [])
                if a.get("type") in heartbeat_types
            )
            test_request_anoms = sum(
                1
                for s in sess
                for a in (s.get("anomalies") or [])
                if a.get("type") in test_request_types
            )
            latency_anoms = sum(
                1
                for s in sess
                for a in (s.get("anomalies") or [])
                if a.get("type") in latency_types
            )
            total_anoms = seq_anoms + heartbeat_anoms + test_request_anoms + latency_anoms
            rows.append(
                (
                    scenario,
                    sessions_n,
                    clean_n,
                    with_anom_n,
                    seq_anoms,
                    heartbeat_anoms,
                    test_request_anoms,
                    latency_anoms,
                    total_anoms,
                )
            )

            # VERIFY OK requirements.
            if scenario in {"happy_path", "flappy_session", "network_loss"} and total_anoms != 0:
                total_failures.append(f"{scenario} expected 0 anomalies, saw {total_anoms}")

            if scenario == "seq_gap":
                elevated = sum(
                    1
                    for s in sess
                    for a in (s.get("anomalies") or [])
                    if a.get("type") == "session_started_at_elevated_seq"
                )
                if elevated != 1:
                    total_failures.append(f"seq_gap expected 1 session_started_at_elevated_seq anomaly, saw {elevated}")
                if heartbeat_anoms != 0:
                    total_failures.append(f"seq_gap expected 0 heartbeat anomalies, saw {heartbeat_anoms}")
                if test_request_anoms != 0:
                    total_failures.append(f"seq_gap expected 0 test_request anomalies, saw {test_request_anoms}")
                if latency_anoms != 0:
                    total_failures.append(f"seq_gap expected 0 latency anomalies, saw {latency_anoms}")

            if scenario == "heartbeat_miss":
                elevated = sum(
                    1
                    for s in sess
                    for a in (s.get("anomalies") or [])
                    if a.get("type") == "session_started_at_elevated_seq"
                )
                inbound_gaps = sum(
                    1
                    for s in sess
                    for a in (s.get("anomalies") or [])
                    if a.get("type") == "heartbeat_gap_inbound"
                )
                if elevated < 1:
                    total_failures.append("heartbeat_miss expected >=1 session_started_at_elevated_seq anomaly")
                if inbound_gaps < 1:
                    total_failures.append("heartbeat_miss expected >=1 heartbeat_gap_inbound anomaly")
                unanswered = sum(
                    1
                    for s in sess
                    for a in (s.get("anomalies") or [])
                    if a.get("type") in {"unanswered_test_request_outbound", "unanswered_test_request_inbound"}
                )
                if unanswered < 1:
                    total_failures.append("heartbeat_miss expected >=1 unanswered_test_request anomaly")
            if scenario == "happy_path" and latency_anoms != 0:
                total_failures.append(f"happy_path expected 0 latency anomalies, saw {latency_anoms}")

        # Verify outbound tracking produces a non-zero final sequence whenever
        # the session included outbound messages.
        msgs = load_messages(input_path, perspective_filter=str(args.perspective_session))
        for sess_msgs in segment_into_sessions(msgs):
            summary = reconstruct_session(sess_msgs)
            if _has_outbound_messages(sess_msgs) and summary["final_outbound_seq"] == 0:
                total_failures.append(
                    f"{summary.get('session_id')} has outbound messages but final_outbound_seq is 0"
                )

        print(_format_table(rows))
        if total_failures:
            print("\nVERIFY FAILED:")
            for f in total_failures:
                print(f"- {f}")
            return 1
        print("\nVERIFY OK")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

