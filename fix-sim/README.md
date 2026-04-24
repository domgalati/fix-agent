## FIX log harness (QuickFIX/J, Docker)

This module generates SOH-delimited FIX **4.4** logs for training an incident agent.

Defaults:
- **BeginString**: `FIX.4.4`
- **SenderCompID/TargetCompID**: `TRADER` ↔ `VENUE`
- **HeartBtInt**: 30s
- **Port**: 9876

### Follow the build log

Development notes and longer write-ups live on Substack: [subscribe / read the series](https://YOUR_SUBSTACK_HERE.substack.com) (replace with your publication URL when you publish).

### Quick start

Build the harness jar *inside Docker*. QuickFIX/J **3.0.0** is resolved from **Maven Central** (no local QuickFIX/J `./mvnw install`, no Javadoc failures from the upstream reactor).

```bash
./scripts/build.sh
```

The optional `quickfixj/` directory (if present from an earlier clone) is **not** required for builds or for `DataDictionary`—configs use `FIX44.xml` from the shaded classpath.

Generate a clean happy-path run (logon → 1 order → exec report → logout):

```bash
./scripts/run_scenario_happy_path.sh
```

Parse logs into normalized JSONL for your agent:

```bash
./tools/parse_fix_logs.py --logs-dir logs --out dataset.jsonl
```

### Failure scenarios

#### Seq gap + recovery
Creates an initiator outgoing sequence-number jump by editing QuickFIX/J **FileStore** `*.senderseqnums` (Java `writeUTF` format; QFJ 3.x splits sender/target into separate files), then reconnects so the acceptor can trigger resend / gap-fill behavior.

```bash
./scripts/run_scenario_seq_gap.sh
```

Containers run as your host UID/GID (`docker run --user …`) so files under `logs/` and `state/` stay editable on the host. If you ran older scripts as root, fix ownership once: `sudo chown -R "$(id -un):$(id -gn)" logs state`.

#### Heartbeat miss (one-way traffic block)
Simulates a “silent” failure where the initiator stops receiving heartbeats by dropping packets *leaving* the acceptor’s FIX port (iptables `OUTPUT --sport 9876`).

```bash
./scripts/run_scenario_heartbeat_miss.sh
```

This scenario requires `sudo` (iptables). You can override the port via `FIXSIM_PORT` (default: `9876`).

#### Network loss (packet loss on loopback)
Applies packet loss on `lo` during an active session using `tc netem` (works because the run scripts use `--network host` and communicate over `127.0.0.1`).

```bash
./scripts/run_scenario_network_loss.sh
```

This scenario requires `sudo` (tc). You can override the loss percent via `FIXSIM_NETWORK_LOSS_PCT` (default: `50%`).

#### Flappy session (restart initiator mid-session)
Repeatedly kills/restarts the initiator container mid-session to produce reconnect churn, then performs a final clean run.

```bash
./scripts/run_scenario_flappy_session.sh
```

You can tune the behavior via `FIXSIM_FLAPPY_CYCLES` (default: `4`) and `FIXSIM_FLAPPY_HOLD_SECONDS` (default: `60`).

#### Network faults (host networking)
These scripts assume the containers run with `--network host` (the included run scripts do).

- Packet loss on loopback:

```bash
sudo ./scripts/faults/packet_loss.sh 10%
sudo ./scripts/faults/clear_netem.sh
```

- Latency/jitter on loopback:

```bash
sudo ./scripts/faults/latency.sh 200ms 50ms
sudo ./scripts/faults/clear_netem.sh
```

- Drop the FIX port with iptables:

```bash
sudo ./scripts/faults/drop_port.sh 9876
sudo ./scripts/faults/restore_port.sh 9876
```

### Where logs/state go
- Happy path:
  - `logs/happy_path/{acceptor,initiator}/`
  - `state/happy_path/{acceptor,initiator}/`
- Seq gap:
  - `logs/seq_gap/{acceptor,initiator}/`
  - `state/seq_gap/{acceptor,initiator}/`
- Scenarios:
  - `logs/{heartbeat_miss,network_loss,flappy_session}/{acceptor,initiator}/`
  - `state/{heartbeat_miss,network_loss,flappy_session}/{acceptor,initiator}/`

