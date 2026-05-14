# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

A local FIX-protocol incident-response agent. The pipeline converts raw QuickFIX/J logs into structured JSONL, runs deterministic anomaly detection, and then feeds an LLM to generate triage analyses—without sending logs to an external service.

Stack: Python + LangChain + Ollama (`qwen2.5:7b-instruct-q4_K_M`) for the agent layer; Docker + Maven + QuickFIX/J 3.0.0 for the log generator.

## Environment setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

The Ollama model must be available locally at `http://localhost:11434`. The investigator will fail to connect if it is not running.

## Pipeline commands

**1. Build the FIX simulation harness** (requires Docker):
```bash
cd fix-sim && ./scripts/build.sh
```

**2. Generate scenario logs** (requires Docker; some scenarios require `sudo` for iptables/tc):
```bash
cd fix-sim
./scripts/run_scenario_happy_path.sh
./scripts/run_scenario_seq_gap.sh
./scripts/run_scenario_heartbeat_miss.sh    # requires sudo
./scripts/run_scenario_network_loss.sh      # requires sudo
./scripts/run_scenario_flappy_session.sh
./scripts/run_scenario_latency_spike.sh
```
Logs land in `fix-sim/logs/<scenario>/{acceptor,initiator}/`.

**3. Parse logs into structured JSONL** (appends to `dataset.jsonl`):
```bash
python3 agent/tools/parse_fix_logs.py
python3 agent/tools/parse_fix_logs.py --scenario seq_gap   # single scenario only
```

**4. Reconstruct sessions and detect anomalies**:
```bash
python3 agent/tools/state_reconstructor.py --input dataset.jsonl --output sessions.jsonl
python3 agent/tools/state_reconstructor.py --input dataset.jsonl --output sessions.jsonl --verify
```
`--verify` prints a scenario-by-scenario anomaly table and exits non-zero on unexpected results.

**5. Run the LLM investigation layer** (requires Ollama):
```bash
python3 agent/tools/investigator.py --sessions sessions.jsonl --dataset dataset.jsonl
python3 agent/tools/investigator.py --session-id "FIX.4.4|TRADER|VENUE#3" --explain
python3 agent/tools/investigator.py --limit 5
```
Outputs: `investigations.jsonl` + `investigations.md`.

## Architecture

```
fix-sim/            ← Docker-based QuickFIX/J harness
  scripts/          ← per-scenario run scripts
  configs/          ← initiator/acceptor .cfg pairs per scenario
  logs/             ← generated log files (gitignored in fix-sim/state/ only)

agent/tools/
  parse_fix_logs.py         ← stage 1: raw logs → dataset.jsonl
  state_reconstructor.py    ← stage 2: dataset.jsonl → sessions.jsonl
  investigator.py           ← stage 3: sessions.jsonl → investigations.jsonl/md
```

### Data model

**`dataset.jsonl`** — one FIX message per line:
```json
{"ts": "2026-04-24T14:22:17.123Z", "scenario": "seq_gap", "direction": "in|out",
 "session": "FIX.4.4|TRADER|VENUE", "tags": {"35": "A", "34": "1", ...},
 "msg_type": "A", "seq_num": 1, "raw": "..."}
```

**`sessions.jsonl`** — one session summary per line:
```json
{"session_id": "FIX.4.4|TRADER|VENUE#12", "session_tuple": "...", "scenario": "...",
 "logon_ts": "...", "logoff_ts": "...", "duration_sec": 42.0,
 "messages_total": 120, "messages_by_type": {"A": 2, "0": 8, ...},
 "final_inbound_seq": 60, "final_outbound_seq": 60, "anomalies": [...]}
```

### Key design decisions

- **Trader-perspective only**: `state_reconstructor.py` filters to `FIX.4.4|TRADER|VENUE` and ignores the mirrored acceptor tuple. This matches the common production case where only one side's logs are available.
- **Session segmentation** is bidirectional: a session runs from a Logon pair to a Logout pair. An unsolicited repeat Logon in the same direction immediately flushes the open session as a dirty reconnect.
- **Anomaly families**: sequence (gap/duplicate/elevated-start), heartbeat (silence vs `HeartBtInt`+2s), TestRequest correlation (tag 112 matching), inbound latency (3-sigma vs session baseline from first 3 samples). Each family is an independent detector composed into `reconstruct_session`.
- **LLM output schema**: the agent must produce `SUMMARY: / EVIDENCE: / RECOMMENDATION:` sections. If the small model drifts, `_reformat_to_schema` retries with a strict reformat prompt before marking the result as an error.
- **parse_fix_logs.py appends** to `dataset.jsonl`; clear it manually before a full re-parse to avoid duplicates.
