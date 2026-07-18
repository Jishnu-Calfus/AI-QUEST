# Guardian — Design Document

**One-liner (v0.2):** A control plane for the AI workforce. Register agent swarms once — know what they truly cost (LLM + cloud + DB + APIs), which agents waste money, why runs fail, and stay in control of everything they're allowed to do. One layer above every agent platform.

---

## v0.2 Platform Architecture (current)

The v0.1 watchdog (documented below, still accurate for the control layer) was extended into a full platform. One event stream now feeds **seven capabilities**:

```
agent step (SDK / REST / proxy / logs)
        │
        ▼
┌─ INGEST ──────────────────────────────────────────────────────┐
│ 1. PRICE IT      costs.py: exact (reported) or catalog-priced │
│                  (cost_catalog.yaml) → category: llm/api/db/  │
│                  compute/storage → per-run → per-swarm rollup │
│ 2. CHECK IT      detectors.py: loops, stalls, budgets, denied │
│                  tools, PII, runtime limits (deterministic)   │
│ 3. JUDGE IT      judge.py: goal drift, sampled, async —       │
│                  Claude / OpenAI-compatible (Ollama, vLLM) /  │
│                  offline heuristic mock                       │
│ 4. ACT ON IT     engine.py: warn→pause→kill→escalate ladder,  │
│                  human resume/kill, audit log                 │
└───────────────────────────────────────────────────────────────┘
        │
        ├── /v1/swarms          swarm economics + waste scores
        ├── /v1/agents          workforce registry (roster)
        ├── /v1/runs/{id}/diagnose   root-cause debugging
        ├── /v1/billing/*       cloud-bill import + true-up
        └── dashboard           live SSE: runs, costs, incidents
```

### New v0.2 components

| Component | File | What it does |
|---|---|---|
| Cost engine | `guardian/costs.py` + `cost_catalog.yaml` | Prices every event: exact where reported, unit-catalog estimates for infra (`db_query`, `compute_second`, `gpu_second`, `storage_gb`…). Categories: llm/api/db/compute/storage/other. |
| Swarm rollup + waste | `engine.swarm_summary()` | Aggregates cost per swarm and per agent; waste_score = cost_share − contribution_share (contribution = novel outputs, tracked by the stall detector). Names the top waster. |
| Registry | `store.py` registry table, `/v1/agents/register` | "Hire" agents: owner, purpose, swarm, budget. The workforce roster. |
| Billing true-up (lane 2) | `/v1/billing/import` + `/v1/billing/reconciliation` | Import actual cloud costs (AWS CUR format); per-category metered-vs-actual deltas. Live estimates + daily reconciliation = FinOps-grade honesty. |
| Root-cause diagnose | `judge.diagnose_run()`, `/v1/runs/{id}/diagnose` | Replays trace + incidents → which step broke the run and why, in plain English. |
| Open-model judge | `judge.py` `OPENAI_BASE_URL` | Any OpenAI-compatible endpoint: Ollama, vLLM, LM Studio. Three env vars, zero code. |
| Swarm demo | `demo/swarm.py`, `demo/import_billing.py`, `demo/sample_cur.csv` | 4-agent invoice swarm with full-stack costs and a deliberate waster; simulated AWS bill import. |

### v0.2 design decisions
- **Two-lane cost model:** live metered estimates (catalog) + delayed billing true-up. Never present estimates as measurements — allocation for shared infra is a labeled policy.
- **Waste needs behavior, not bills:** contribution is measured from output novelty in the trace — impossible for billing-side tools, trivial for us. This is the structural moat (traces × billing join).
- **Judge ignores infra events:** resource events carry no goal vocabulary; only semantic events (llm/tool/output) feed drift scoring — this was a real false-positive bug found in testing.

*(v0.1 watchdog design follows — still the authoritative reference for detectors, ladder, control channel, and NFRs.)*

---

## 1. Problem & positioning

Agents fail silently: high activity, zero progress (the **Looper**, the **Wanderer**, the **Repeater** — documented failure patterns), token burn without outcomes, and actions no human approved. Observability tools show traces *after* the incident; nothing intervenes *during* the run. Gartner projects "guardian agents" at 10–15% of the agentic AI market by 2030 — today it's near-greenfield.

**Positioning:** Not another agent framework. A sidecar/control-plane that works with LangGraph, CrewAI, raw loops, anything.

## 2. Architecture

```
┌────────────┐  events (HTTP/SDK)   ┌─────────────────────────────────────┐
│ Your Agent │ ───────────────────► │            GUARDIAN CORE            │
│  (any fw)  │ ◄─────────────────── │                                     │
└────────────┘  control (continue/  │ Ingest API ─► L1 Detectors (inline, │
      ▲          pause/kill)        │   <50ms, deterministic)             │
      │                             │     • Loop / repetition             │
   SDK wrapper                      │     • Stall (no progress)           │
   or raw REST                      │     • Budget (tokens/$/rate)        │
   or log tailer                    │     • Policy (tools/PII/domains)    │
                                    │     • Runtime (steps/duration)      │
                                    │          │ suspicion                │
                                    │          ▼                          │
                                    │ L2 LLM Judge (async, sampled,       │
                                    │   pluggable: Claude/OpenAI/mock)    │
                                    │     • goal drift scoring            │
                                    │     • incident explanation          │
                                    │          │                          │
                                    │          ▼                          │
                                    │ Action Engine (graduated ladder)    │
                                    │   observe→warn→pause→kill→escalate  │
                                    │          │                          │
                                    │   SQLite audit log                  │
                                    └──────┬──────────────┬───────────────┘
                                           │ SSE          │ webhook
                                           ▼              ▼
                                    Live Dashboard   Slack / human
                                    (pause/resume/kill buttons)
```

### Components
- **Ingest API** — `POST /v1/events`: one JSON event per agent step (llm_call, tool_call, output, error). Auth via `X-Guardian-Key`.
- **L1 Detectors (deterministic, inline)** — run on every event in-process, no LLM, <50ms. Cheap and explainable. Each returns `(severity, signal, evidence)`.
- **L2 LLM Judge (async, sampled)** — only invoked on L1 suspicion or every N steps. Compacts recent trace → asks judge model for `{drift_score, verdict, reasoning, recommended_action}`. Pluggable providers: Anthropic, OpenAI, or heuristic mock (no key needed). Never blocks the agent.
- **Action Engine** — maps signals to a graduated ladder with hysteresis (no flapping): `observe → warn → pause → kill → escalate`. Pause requires human resume from dashboard. Every verdict/action is audit-logged.
- **Control channel** — the SDK checks `GET /v1/runs/{id}/control` before each step (and event POSTs return the current control state). Paused ⇒ SDK blocks at the next checkpoint; killed ⇒ SDK raises `GuardianKilled`.
- **Dashboard** — single-page, SSE live feed: active runs, health, cost, incident timeline with LLM explanations, pause/resume/kill buttons.
- **Storage** — SQLite (zero config; swap to Postgres via one env var pattern later).

### Key design decision: cooperative control
You cannot force-kill a process you don't own. Guardian therefore uses **cooperative interruption**: the watched agent checks in at natural checkpoints (each step). This is honest, framework-agnostic, and identical to how HITL gates work in production systems. For hard-kill, run the agent under `guardian run -- <cmd>` (stretch goal: process supervisor mode).

## 3. Functional requirements

- **FR1 Ingest**: accept trace events via REST + Python SDK + JSONL log tailer (zero-code mode).
- **FR2 Detect**: loops/repetition, stalls (no new information), budget breach (tokens, $, call rate), policy violations (denied tools, PII regex, denied domains), runtime overrun (max steps/wall time), goal drift (LLM-judged).
- **FR3 Intervene**: graduated actions — warn, pause (human resume), kill, escalate (Slack webhook + dashboard). Per-agent policy decides the ladder.
- **FR4 Explain**: every incident gets a human-readable explanation (LLM-written when key present; template otherwise).
- **FR5 Observe**: live dashboard of runs, events, costs, incidents; audit log of every verdict and action.
- **FR6 Policy-as-config**: `policies.yaml` — per-agent budgets, allowed/denied tools, thresholds, action ladder.
- **FR7 Multi-agent**: watch N concurrent runs independently.

## 4. Non-functional requirements

- **Easy integration (the #1 NFR)**: ≤5 lines of code with SDK; raw REST for any language; log-tailer for zero code changes.
- **Non-blocking**: L1 inline <50ms; L2 async — the watchdog never adds latency to the agent's LLM calls.
- **Fail-open by default**: if Guardian is down, agents keep working (SDK timeouts + local no-op). `fail_closed: true` per policy for regulated flows.
- **Watchdog cost ≪ agent cost**: L2 sampled/triggered, compact prompts, cheap model class; target <2% of watched-agent spend.
- **Reliability**: idempotent event ingest (event_id dedupe); guardian restart-safe (state in SQLite).
- **Security**: API-key auth; optional payload redaction (store hashes only) for PII-sensitive deployments.
- **Deployability**: single Docker container; `docker compose up` runs server + demo fleet.
- **Scale path (documented, not built)**: stateless API → put events on a queue (Redis/SQS), detectors as consumers, Postgres. Nothing in the design blocks this.

## 5. What works vs. what doesn't (research-backed learnings)

- **Hybrid beats LLM-only.** LLM-judging every step is too slow/expensive and non-deterministic for guarantees. Deterministic L1 rules catch ~80% of documented failure modes (loops, budget, policy) instantly; the LLM is reserved for the fuzzy 20% (drift, intent). This mirrors how Aetherion's "Hybrid Decisioning" markets governance — rules fused with reasoning.
- **Similarity loops beat exact-match loops.** Real loopers rephrase ("search X", "look up X again") — exact-hash detection misses them. Use normalized-text Jaccard/overlap similarity over a sliding window.
- **Pause > kill.** Killing loses work and context; pausing at a checkpoint with human resume converts a runaway into a HITL moment. Kill is reserved for policy violations and hard budget breach.
- **Hysteresis matters.** One suspicious step ≠ incident. Escalate on consecutive/accumulated signals; decay scores as healthy steps pass. Otherwise the dashboard cries wolf and humans ignore it (alert fatigue is why current observability tools fail).
- **Fail-open is the only viable default** for adoption — nobody adds a dependency that can take their agent down. Offer fail-closed as opt-in for regulated workflows.
- **You can't hard-stop what you don't own.** Cooperative checkpoints are the honest baseline; a process-supervisor mode (`guardian run --`) is the upgrade path.
- **Judge on deltas, not full traces.** Sending the whole trace to the judge explodes tokens; send the goal + last K compacted steps + running stats.

## 6. Tech stack

| Layer | Choice | Why |
|---|---|---|
| Core service | Python 3.11 + FastAPI + Uvicorn | Fast to extend, async-native, squad-readable |
| Storage | SQLite (stdlib) | Zero config; audit-grade enough for demo; Postgres later |
| Live updates | SSE | Simpler than WebSockets, one-way is all we need |
| LLM judge | Pluggable: Anthropic / OpenAI / mock | Keys unknown on demo day; mock = demo never blocked |
| SDK | Pure-python `guardian_sdk.py` (httpx) | Copy-paste into any project |
| Dashboard | Single HTML file, vanilla JS + SSE | No build step, no node_modules on stage |
| Packaging | Docker + docker-compose | One command, portable across squad laptops |

## 7. Demo script (5 minutes)

1. `docker compose up` → dashboard on `localhost:8090`. (30s)
2. Launch the fleet: 1 healthy agent + 4 rogues (Looper, Wanderer, Spender, Violator). All green initially. (30s)
3. **Looper** starts repeating a tool call → L1 flags repetition → warn → still looping → **auto-pause**. Dashboard shows evidence ("same search 4×, 0 new info"). (60s)
4. **Spender** burns tokens → budget breach at $0.50 → **kill** with cost graph spiking. (45s)
5. **Violator** calls a denied tool (`delete_database`) / emits PII → instant **kill**, policy evidence shown. (45s)
6. **Wanderer** drifts off-goal → L2 judge verdict with reasoning ("asked to summarize invoices; is now researching crypto") → **escalate**: Slack-style approval on dashboard → human clicks Resume-with-warning or Kill. (60s)
7. Healthy agent finishes untouched. Close on the audit log: every decision, timestamped, explainable. (30s)

**Kicker line:** "Every demo you saw today ran under a $2 budget. Guardian spent 3 cents supervising it."

## 8. Hackathon-day task split (squad of 4–5)

- **Dev A**: harden detectors (tune thresholds, add regex packs), unit tests.
- **Dev B**: dashboard polish (charts, incident drill-down), Slack webhook → real channel.
- **Dev C**: real-LLM demo agent (wire an actual LangGraph/CrewAI agent through the SDK) — proves "any framework" claim live.
- **Dev D**: judge prompts + eval (golden traces → expected verdicts), README/pitch deck.
- **Stretch**: `guardian run -- python my_agent.py` supervisor mode; OTel ingest adapter; cost-per-outcome report.

## 9. Repo layout

```
ai-guardian/
├── DESIGN.md            ← this file
├── README.md            ← quickstart
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── policies.yaml        ← per-agent budgets/rules/ladders
├── guardian/
│   ├── main.py          ← FastAPI app, routes, SSE
│   ├── models.py        ← pydantic schemas
│   ├── store.py         ← SQLite audit store
│   ├── detectors.py     ← L1 deterministic detectors
│   ├── judge.py         ← L2 pluggable LLM judge (anthropic/openai/mock)
│   ├── engine.py        ← verdict → action ladder, hysteresis, control state
│   ├── config.py        ← policy loading
│   └── dashboard.html   ← live UI
├── sdk/
│   └── guardian_sdk.py  ← ≤5-line integration client
└── demo/
    ├── agents.py        ← Healthy, Looper, Wanderer, Spender, Violator
    └── run_demo.py      ← fleet scenario runner
```
