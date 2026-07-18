# ⬡ Guardian — Control Plane for the AI Workforce

Register your agent swarms once — know **what they truly cost** (LLM + cloud + DB + APIs), **which agents waste money**, **why runs fail**, and **stay in control** of everything they're allowed to do. One layer above every agent platform. Nothing slips.

## Setup from scratch (2 minutes)

**Option A — Docker (recommended for demo day):**
```bash
docker compose up --build
```

**Option B — plain Python (3.10+):**
```bash
pip install -r requirements.txt
uvicorn guardian.main:app --port 8090
```

Open **http://localhost:8090** → the dashboard.

## Run the demos

```bash
cd demo
python swarm.py            # ECONOMICS: 4-agent invoice swarm, full-stack costs,
                           #   waster agent gets named + paused
python run_demo.py         # CONTROL: rogue agents get warned/paused/killed/escalated
python import_billing.py   # TRUE-UP: import simulated AWS bill, see metered-vs-actual
python clusters.py         # TOPOLOGY: same swarm, differently-shaped tasks; one task
                           #   frozen mid-run for crossing a denied cluster boundary
```

Demo order for the pitch: `swarm.py` first (cost story) → `import_billing.py` (reconciliation) → `run_demo.py` (enforcement theater) → click resume/kill on the dashboard (human-in-the-loop).

## Optional environment (all have working defaults)

| var | effect |
|---|---|
| *(nothing)* | fully offline: deterministic heuristic judge — demo cannot break |
| `ANTHROPIC_API_KEY` | real LLM judge via Claude |
| `OPENAI_API_KEY` + `OPENAI_BASE_URL` + `JUDGE_MODEL` | any OpenAI-compatible endpoint — **open-source models**: `OPENAI_BASE_URL=http://localhost:11434/v1 OPENAI_API_KEY=ollama JUDGE_MODEL=llama3.1:8b` (Ollama), or vLLM/LM Studio the same way |
| `SLACK_WEBHOOK_URL` | incident alerts to Slack |
| `GUARDIAN_API_KEY` | ingest auth (default `guardian-dev-key`) |

## Integrate an agent (≤5 lines, any framework)

```python
from guardian_sdk import Guardian
g = Guardian(agent_id="invoice-bot", swarm_id="finance-swarm",
             goal="Summarize Q2 invoices")
g.llm("gpt-4o", prompt, tokens_in=1200, tokens_out=300, cost_usd=0.01)
g.tool("web_search", "acme invoices")        # priced from cost_catalog.yaml
g.resource("db_query", qty=4)                # infra cost: db/compute/storage/gpu
g.output("Found 12 invoices totalling $9,412")
g.end()

# multi-agent workflows: pass the context token on hand-off — that single hop is
# the ENTIRE integration; Guardian derives the cross-agent task graph, per-task
# cost, and topology governance from it.
child = Guardian(agent_id="extractor", swarm_id="finance-swarm", **g.context())
```
Fail-open (Guardian down ⇒ agent unaffected). On pause the call blocks until a human resumes; on kill it raises `GuardianKilled`. Any language works via raw REST: `POST /v1/events`.

## Configure

- `policies.yaml` — guardrails resolved across **four levels** (`default → swarm → cluster → agent`, most specific wins; restrictive fields tighten-only): budgets, denied tools, PII blocking, thresholds, action ladder (warn→pause→kill→escalate), plus swarm-level **topology** policy (denied cluster edges, required predecessors, fan-out caps, task budgets)
- `cost_catalog.yaml` — unit prices for infra events (derive from your cloud bills)

## Sub-cluster governance (declared vs realized)

Group agents within a swarm into policy-bearing **clusters** (ingestion, extraction, payments…), then govern the **shape** of execution — not just individual steps — against the topology that actually emerges at runtime:

- **Declared** (static): agents assigned to clusters in the registry — the org chart.
- **Realized** (dynamic): every end-to-end **task** (`task_id`) is stitched from the event stream via context propagation — which runs/clusters participated, in what order, at what cost.
- **Governance = the join**: denied cluster edges, missing required predecessors, fan-out caps, task budgets and shadow utilization are enforced deterministically at ingest, freezing the offending run *before* its action runs. See `CLUSTERS_DESIGN.md`.

## API map

| endpoint | what |
|---|---|
| `POST /v1/events` | ingest one agent step (carries `task_id` + `parent_run_id`) |
| `GET /v1/swarms` | swarm cost rollup + waste analytics |
| `POST /v1/agents/register` · `GET /v1/agents` | workforce roster (with cluster) |
| `GET /v1/agents/{id}/analysis` | per-agent trace, tools, graph, root cause |
| `GET /v1/tasks` · `GET /v1/tasks/{id}` | realized task list + per-task graph, cost-by-cluster, violations |
| `POST /v1/tasks/{id}/action` | human decision on a whole task (resume/kill the frozen run) |
| `GET /v1/clusters` | per-cluster cost + waste rollup |
| `GET /v1/runs/{id}/diagnose` | root-cause a failed run |
| `POST /v1/billing/import` · `GET /v1/billing/reconciliation` | actual-bill true-up |
| `POST /v1/runs/{id}/action` | human pause/resume/kill |
| `GET /v1/runs` · `/v1/incidents` · `/v1/audit` · `/v1/stream` | state, incidents, audit, live SSE |
| `GET /` · `/agent?agent_id=` · `/task?task_id=` | dashboard · per-agent page · per-task page |

## Repo map

```
guardian/        core: ingest → pricing → detectors → judge → actions → audit
  costs.py       full-stack cost engine (catalog pricing + categories)
  detectors.py   deterministic rules: loops, stalls, budgets, policy, runtime
  judge.py       pluggable LLM judge (Claude/OpenAI-compatible/offline mock)
  engine.py      suspicion ladder + swarm rollup + waste + TASK topology governance
  config.py      4-level policy resolution (default→swarm→cluster→agent) + topology
  dashboard.html · agent.html · task.html   fleet · per-agent · per-task pages
sdk/             guardian_sdk.py — the ≤5-line client (+ context propagation)
demo/            swarm.py (economics), run_demo.py (control), import_billing.py,
                 clusters.py (sub-cluster topology governance)
PITCH_PREP.md    everything the presenting team must know
DESIGN.md        architecture, FR/NFR, learnings
CLUSTERS_DESIGN.md  sub-cluster governance: declared vs realized, topology policy
```
