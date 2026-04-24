# fix-agent

**Goal:** Encode how production support actually triages **FIX** sessions—sequence gaps, flappy clients, one-way network blocks, degraded paths—so a **local, sovereign** stack can do the high-friction log correlation instead of living in `grep` across two counterparties’ logs forever. The LLM is meant to sit on **structured facts** (parsed tags, scenario labels, session lifecycle), not raw SOH strings, so reasoning stays grounded instead of hallucinated.

This repo is the **data and plumbing layer** for that idea: QuickFIX/J generates realistic **FIX 4.4** traffic in Docker (two parties: venue acceptor ↔ client initiator), shell scripts reproduce distinct **failure signatures**, and Python turns QuickFIX/J file logs into **JSONL** the agent can learn from. Most of the work here is ordinary engineering—Docker, `tc`/`iptables` fault injection, parsers—not the model itself.

**Rough roadmap (from the build journal):**

1. **Session state reconstructor** — deterministic Python over parsed logs (per-side seq progression, heartbeats, recovery events). No LLM.
2. **Anomaly detector** — rule-based tailing / triggers (“gap with no ResendRequest within N seconds”, “missed heartbeats”, …).
3. **Investigation agent** — LangChain-style tool loop (`get_session_state`, `get_messages_in_window`, …) with Ollama (or similar) orchestrating.
4. Later: **retrieval** over runbooks, prior incidents, counterparty quirks so reasoning isn’t only generic.

### Repo layout

- **`fix-sim/`** — Harness, scenarios, fault scripts, log → JSONL tooling. Details: [fix-sim/README.md](fix-sim/README.md).
- **`agent/`** — Python venv–friendly scripts (e.g. LangChain + Ollama hello path, shared parsing helpers). Dependencies: [requirements.txt](requirements.txt).

### Links

- **Code:** `https://github.com/PLACEHOLDER/PLACEHOLDER` *(replace with your public repo URL when published)*  
- **Build journal / essays:** [andihow on Substack](https://substack.com/@andihow)
