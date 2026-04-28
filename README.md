# fix-agent

**Goal:** Encode how production support actually triages **FIX** sessions—sequence gaps, flappy clients, one-way network blocks, degraded paths—so a **local, sovereign** stack can do the high-friction log correlation instead of living in `grep` across two counterparties’ logs forever. The LLM is meant to sit on **structured facts** (parsed tags, scenario labels, session lifecycle), not raw SOH strings, so reasoning stays grounded instead of hallucinated.

### Log generator

`fix-sim/` is the synthetic data source for the project. It uses
QuickFIX/J plus scenario scripts to generate realistic FIX 4.4 sessions and
then injects operational failure modes like sequence gaps, flappy reconnects,
one-way heartbeat loss, and degraded network paths. The point is not just to
make "some FIX traffic", but to produce recognizable support-style signatures
that can be reconstructed and reasoned about later.

### Log parser

The parser stage converts raw QuickFIX/J file logs into structured JSONL
(`dataset.jsonl`). Each line is one FIX message with normalized fields like
timestamp, scenario label, direction, session tuple, parsed tags, message type,
and sequence number. That gives the reconstructor deterministic inputs instead
of forcing downstream logic to reason over raw SOH-delimited strings.

### State reconstructor

The first milestone is now built in `agent/tools/state_reconstructor.py`.
It takes parsed FIX message JSONL (`dataset.jsonl`) and reconstructs
**session-level state summaries** into `sessions.jsonl`, one JSON object per
logical session.

Current behavior:

- Segments logs into synthetic sessions like `FIX.4.4|TRADER|VENUE#12`, where a
  session is one Logon (`35=A`) to Logout (`35=5`) cycle.
- Tracks inbound and outbound sequence progression separately.
- Emits session summaries with:
  - `logon_ts`, `logoff_ts`, `duration_sec`
  - `messages_total`
  - `messages_by_type`
  - `final_inbound_seq`, `final_outbound_seq`
  - `anomalies` (ordered by detection time)

Current anomaly families:

- **Sequence state**
  - `seq_gap_inbound`, `seq_gap_outbound`
  - `seq_num_lt_expected_inbound`, `seq_num_lt_expected_outbound`
  - `session_started_at_elevated_seq`
- **Heartbeat timing**
  - `heartbeat_gap_inbound`, `heartbeat_gap_outbound`
  - `heartbeat_interval_unspecified`
- **TestRequest correlation**
  - `unanswered_test_request_inbound`, `unanswered_test_request_outbound`
  - `slow_test_request_response_inbound`, `slow_test_request_response_outbound`
- **Timestamp latency**
  - `elevated_inbound_latency`

What each detector is doing today:

- **Sequence reconstruction** catches true seq gaps, out-of-order / duplicate
  numbers, and the important reconnect case where a session begins with an
  already-elevated inbound sequence number.
- **Heartbeat timing** measures silence relative to `HeartBtInt` (tag `108`)
  with a `+2s` tolerance and emits one anomaly per continuous silence window.
- **TestRequest correlation** matches `35=1` against `35=0` replies via
  `TestReqID` (`112`) and flags unanswered or slow responses.
- **Timestamp latency** compares parser time (`ts`) with FIX `SendingTime`
  (`52`) for inbound messages and flags three-sigma outliers relative to the
  first three inbound samples in a session.

Run it against the generated data in `sessions.jsonl`:

```bash
python3 agent/tools/state_reconstructor.py --input dataset.jsonl --output sessions.jsonl
python3 agent/tools/state_reconstructor.py --verify
```

`--verify` prints a scenario summary table with anomaly-family counts:

- `seq_anomalies`
- `heartbeat_anomalies`
- `test_request_anomalies`
- `latency_anomalies`
- `total_anomalies`

On the current sample dataset, the reconstructor is correctly identifying:

- `seq_gap` via elevated inbound sequence on reconnect
- `heartbeat_miss` via heartbeat silence plus unanswered TestRequest
- `network_loss` as clean at the current detector threshold, because those
  sample sessions are too short to produce post-baseline latency outliers

That means the repo now has a deterministic, non-LLM state layer for the main
operational FIX failure signatures we wanted to model first.

Example --verify output:
```bash
$ python state_reconstructor.py --input ../../dataset.jsonl --output ../../sessions.jsonl --verify
scenario        sessions  clean  with_anomalies  seq_anomalies  heartbeat_anomalies  test_request_anomalies  latency_anomalies  total_anomalies
--------------  --------  -----  --------------  -------------  -------------------  ----------------------  -----------------  ---------------
flappy_session        10     10               0              0                    0                       0                  0                0
happy_path             2      2               0              0                    0                       0                  0                0
heartbeat_miss         4      1               3              1                    3                       1                  0                5
network_loss           2      2               0              0                    0                       0                  0                0
seq_gap                4      3               1              1                    0                       0                  0                1

VERIFY OK
```

### Current pipeline

1. `fix-sim/` generates scenario-driven FIX logs.
2. The parser converts raw FIX logs into structured message JSONL.
3. `agent/tools/state_reconstructor.py` converts message JSONL into
   session-level state summaries plus anomaly facts.

### TODO

- [x] Session state reconstructor
- [ ] Write a `latency_spike` scenario in `fix-sim` using a clean session start
  and mid-session injected delay, then re-parse and re-run the reconstructor to
  verify that `latency_anomalies` actually fire on longer sessions.
- [ ] Start the LLM agent layer with a LangChain tool-calling loop using
  `qwen2.5:7b`, with tools like `get_anomalous_sessions()`,
  `get_session_details(session_id)`, and `get_messages_in_session(session_id)`.
  The runner should read `sessions.jsonl`, investigate anomalous sessions, and
  write the analyses to a file.

### Repo layout

- **`fix-sim/`** — Harness, scenarios, fault scripts, log → JSONL tooling. Details: [fix-sim/README.md](fix-sim/README.md).
- **`agent/`** — Python venv–friendly scripts (e.g. LangChain + Ollama hello path, shared parsing helpers). Dependencies: [requirements.txt](requirements.txt).

### Links

- **Build journal / essays:** [andihow on Substack](https://substack.com/@andihow)
