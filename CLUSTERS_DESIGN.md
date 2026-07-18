# Guardian — Dynamic Sub-Cluster Governance (Design)

**One-liner:** Group agents *within* a swarm into policy-bearing **clusters**
(ingestion, extraction, payments…), then govern not just what each agent does
but the **shape of execution across clusters** — enforced in real time against
the topology that actually emerges at runtime, not the one drawn on a whiteboard.

This document is concept-level. It reuses Guardian's existing machinery
(one-event-per-step ingest, deterministic detectors, the warn→pause→kill→escalate
ladder, per-agent/per-swarm rollup, YAML policy resolution, cooperative control,
SQLite audit) and adds exactly two primitives — **task** and **cluster** — plus
the join between what was *declared* and what was *realized*.

---

## 1. Mental model & vocabulary

The whole design rests on separating two layers that the industry already
separates elsewhere. Keep the analogy in mind throughout:

| Guardian term | IAM analogy | Distributed-tracing analogy | Nature |
|---|---|---|---|
| **Cluster (declared)** | Role / group | Service in the catalog | Static, human-authored |
| **Task (realized)** | Session | Trace | Dynamic, observed |

**Definitions (crisp):**

- **Agent** — an LLM-in-a-loop identity (`agent_id`). Already exists. Has an
  owner and a purpose in the registry.
- **Run (a.k.a. span)** — one execution of one agent pursuing one goal
  (`run_id`). Already exists as `RunStatus`. **The run is the unit of
  enforcement**: pause/kill/escalate act on a run. In a task graph a run *is* a
  span.
- **Swarm** — a group of agents cooperating on a workflow (`swarm_id`). Exists.
- **Cluster** — a named sub-group of agents **within one swarm**, grouped by use
  case. **Declared** by humans in the registry; **carries policy**. This is the
  org chart / IAM role. An agent's cluster membership is *intent*, not a promise
  about any particular run.
- **Task** — one end-to-end end-user request / workflow instance (`task_id`).
  One task = "this one invoice being processed." **A task spans many runs across
  many agents and clusters.** This is the new top-level identity — the trace /
  the session. It is the missing unit that makes cost-per-outcome and
  cross-agent governance possible.
- **Edge** — a parent→child invocation: run A invoked run B
  (`parent_run_id`). A **cluster edge** is `cluster(A) → cluster(B)`.
- **Realized cluster set** — the clusters a task actually touched, *derived* from
  its runs. Never declared.
- **Topology** — the shape (a DAG of runs/clusters) of a task's realized call
  graph.
- **Governance event** — any divergence between realized behavior and declared
  structure: a denied edge traversed, a required predecessor skipped, a fan-out
  breach, a task-budget breach, or **shadow utilization** (an agent acting on
  behalf of a cluster it was never assigned to).

**The product lives in the JOIN.** Declared alone is a static diagram nobody
enforces. Realized alone is an observability trace nobody governs. Overlaying
one on the other — *"reality just did something the org chart forbids"* — is the
governable moment, and Guardian already owns the enforcement primitive to act on
it.

```
DECLARED (registry, YAML)             REALIZED (event stream, per task)
  swarm: invoice                        task 8f21  (one invoice)
   ├ cluster ingestion  {policy}          run r1 fetcher  (ingestion)
   ├ cluster extraction {policy}          ├─▶ run r2 ocr       (extraction)
   ├ cluster validation {policy}          │    └─▶ run r3 llm  (extraction)
   └ cluster payments   {policy}          └─▶ run r4 validate  (validation)
         ▲                                       └─▶ run r5 ledger (payments)
         └──────────────── JOIN ─────────────────────┘
              governance = does r-graph obey cluster policy?
```

---

## 2. Context propagation — capturing the realized graph with ~zero author effort

The realized graph is only valuable if it materializes automatically. The design
target: **an agent author does exactly one new thing — pass a small context
token when they hand work to another agent.** Everything else (which clusters,
cost per cluster, topology, violations) is derived at ingest.

### Two new IDs on every event

`AgentEvent` gains two fields (promoted to top-level, not buried in `meta`, so
the store can index them):

| Field | Meaning | Who sets it |
|---|---|---|
| `task_id` | The end-to-end workflow instance | Minted once at the entry point; propagated unchanged to every descendant run |
| `parent_run_id` | The run that invoked this run | Set by the caller when it hands off; empty for the root run |

From these two, Guardian reconstructs the entire tree: `task_id` groups all runs
of a workflow; `parent_run_id` supplies the edges; `cluster(run)` comes from the
declared registry. No author ever states "this task uses these clusters" — it is
*computed*.

### Propagation mechanism (framework-agnostic, industry-proven)

This is deliberately **W3C `traceparent` / OpenTelemetry context propagation**,
narrowed to two IDs. The SDK holds a context `(task_id, my run_id)`. When agent A
invokes agent B, A asks the SDK for a **handoff token** and passes it however A
already talks to B:

- **HTTP** — headers `X-Guardian-Task`, `X-Guardian-Parent`.
- **In-process / function call** — kwargs or a context object.
- **Queue / message bus** — message metadata.

B constructs its Guardian client from that token; B's first event therefore
arrives carrying `task_id` and `parent_run_id = A's run_id`. The edge is recorded
at B's `run_start`. **The author's only responsibility is "pass the token on
handoff."** Root entry points (the orchestrator receiving the user request) mint
a fresh `task_id`; if a root run arrives with none, Guardian mints one so a task
always exists.

### Zero-code and degraded modes (be honest about confidence)

- **Chokepoint modes** (LLM-gateway proxy / tool-proxy) inject the same two IDs
  into forwarded headers — so agents you cannot modify still get stitched.
- **No propagation at all** → Guardian falls back to *heuristic stitching*
  (same swarm + temporal adjacency + goal-vocabulary correlation) and labels the
  resulting trace **`inferred`**. Inference is best-effort and **must not be
  treated as ground truth for hard enforcement** (see §7 open questions).
  Explicit propagation is the reliable path; inference keeps the picture from
  going blank when a team hasn't wired it yet.

### Why this survives real agentic workloads

The declared layer (manual cluster assignment) is a trivial authoring surface —
good enough for v1. The dynamic layer is what keeps it honest when an LLM
orchestrator invents a path at runtime: the trace records what *happened*, not
what someone hoped would happen. Manual assignment + traced reality is the whole
point of the two-layer split.

---

## 3. Policy model

### Resolution: four levels, most-specific-wins — with one deliberate asymmetry

Today `config.py` field-merges `default → agent`. We extend to:

```
default  →  swarm  →  cluster  →  agent
(least specific)                  (most specific)
```

merged **field by field** (keeping the current `{**base, **override}` semantics —
a more specific level overrides only the fields it names, inheriting the rest).

**The asymmetry — and it is a governance decision, not an accident:**

- **Scalar limits** (`max_cost_usd`, `max_tokens`, thresholds, `judge_every_n`):
  *most-specific-wins*. An agent may raise or lower its own budget within what
  its cluster/swarm allow.
- **Restrictive / security fields** (`denied_tools`, `denied_patterns`,
  `pii_block`, and the new `denied_edges`): **monotonic — can only get
  stricter going down the levels (union, never subtraction).** A cluster cannot
  *unblock* a tool the swarm denied; an agent cannot turn off `pii_block` its
  cluster requires. Separation-of-duties boundaries must be **floors, not
  defaults.** *(Flagged as a choice in §7 — the alternative, pure
  most-specific-wins, is simpler but lets a leaf loosen a boundary, which
  defeats the feature.)*

### Two classes of policy

**(a) Per-run limit policies** — budgets, allowed/denied tools, thresholds.
These attach at any of the four levels and are evaluated by the *existing*
detectors against individual runs/events, just with richer resolution.

**(b) Topology policies** — govern the *shape* of a task, not a single event.
They cannot resolve per-agent (no single agent owns a shape); they attach at the
**swarm** level (optionally cluster-qualified) and are evaluated over
**task-accumulated state**.

### YAML

```yaml
default:                        # global fallback (exists today)
  max_cost_usd: 1.00
  pii_block: true

swarms:
  invoice-swarm:
    max_cost_usd: 5.00          # swarm-wide default for its agents

    clusters:
      ingestion:
        allowed_tools: [fetch_invoice, s3_get, db_query]
        max_cost_usd: 0.50
      extraction:
        allowed_tools: [ocr_page, llm_extract]
        max_cost_usd: 1.50
      validation:
        allowed_tools: [rules_check, llm_verify]
      payments:
        allowed_tools: [ledger_write, send_wire_transfer]
        pii_block: true         # monotonic: cannot be turned off below

    topology:                   # NEW — governs the task graph shape
      denied_edges:
        - { from: extraction, to: payments }   # never invoke payments directly
      required_predecessors:
        - { cluster: payments, requires: validation }  # must pass validation first
      max_clusters_per_task: 4
      max_runs_per_task: 12                     # runaway-orchestrator fan-out cap
      task_budget_usd: 2.00                     # spans ALL runs in the task
      shared_clusters: [utility]                # exempt from shadow-utilization rule
      # per-violation enforcement actions (reuse existing ladder vocabulary)
      on_denied_edge:           escalate        # freeze child + human decision
      on_missing_predecessor:   escalate
      on_fanout_breach:         pause
      on_task_budget:           pause
      on_shadow_utilization:    escalate

agents:                         # exists today; still most specific
  reviewer-agent:
    cluster: extraction         # DECLARED assignment
    max_cost_usd: 1.00
```

Cluster **assignment** (`agent → cluster`) may live either in `agents:` (as
above) or in the registry roster (`/v1/agents/register` gains a `cluster` field).
Registry is the recommended authoring surface — it's where owner/purpose already
live, and it keeps assignment editable without a YAML redeploy.

---

## 4. Detection model

Guardian's detectors run **inline, per event, deterministically, <50 ms**. The
new topology detector needs one new piece of state: a **per-task accumulator**
(`TaskState`, keyed by `task_id`, sitting beside `engine.runs` in memory and
mirrored to the store) holding: the set of runs, the edge list, the ordered log
of *cluster entries* (first time each cluster is touched, with the run and the
predecessor cluster), and running task cost.

### Deterministically checkable at ingest

| Violation | When it's decidable | Signal / evidence | Default action |
|---|---|---|---|
| **Denied cluster edge** | On the child run's **first event** (`run_start`), compute `cluster(parent)→cluster(child)`; membership test against `denied_edges` | `from`, `to`, `parent_run_id` | **escalate** (freeze child *before* it acts) → configurable `kill` for high-risk targets |
| **Missing required predecessor** | On first event of a run entering the guarded cluster (e.g. payments): scan task's cluster-entry log for `requires` | required cluster, task's realized cluster order | **escalate / pause** (freeze before the sensitive action) |
| **Fan-out breach** | On each new run / new cluster in the task: increment counters vs `max_runs_per_task` / `max_clusters_per_task` | current counts, caps | **pause** the offending new run (warn first via the suspicion ladder) |
| **Task budget breach** | On every priced event: add to task cost; compare to `task_budget_usd` | task cost, cap, contributing clusters | **pause** all active runs in the task (or `kill`) |
| **Shadow utilization** | On the child run's first event: the child's declared cluster ∉ any cluster (unassigned) **or** it was invoked *on behalf of* a cluster it isn't in, and its cluster ∉ `shared_clusters` | agent, expected cluster, actual cluster | **escalate** (human — this is the "undeclared pathway" signal) |

All of these reduce to set-membership / counter / sum comparisons over
`TaskState` — cheap, explainable, no LLM. They emit `Signal(detector="topology",
severity=…)` and flow through the **existing** `_open_incident` → ladder →
Slack/audit path unchanged. Critical topology signals (denied edge into payments,
missing validation, shadow into a sensitive cluster) map to `critical` severity
and act immediately, exactly like a denied-tool violation does today.

### Why "freeze mid-run" works for free

Detection fires on the **child's first event** — *before* the child performs its
action. Guardian's control is cooperative: the child's Guardian client reads the
control state on that same call, sees `escalated`/`paused`, and **blocks at the
checkpoint**. So a denied `extraction→payments` hop freezes the payments run
*before any `ledger_write` executes*. This is the existing
"enforcement-needs-a-chokepoint" story applied at task scope — the child's first
event is the chokepoint.

### Not deterministic → deferred, not faked

- *"Was this pathway appropriate?"* — semantic, not topological. Leave to the
  existing L2 judge / drift scoring.
- **Drift analytics** — declared clusters never realized (dead roles),
  undeclared pathways that keep recurring (formalization candidates),
  shadow-utilization frequency — computed **offline/batch** over accumulated task
  traces and surfaced as *recommendations*, never as real-time blocks.

---

## 5. Dashboard

Four surfaces, all built on the SSE feed + the per-task/per-cluster rollups. The
task-graph renderer generalizes the agent-page SVG graph (already shipped in
`agent.html`) from run-scope to task-scope.

1. **Per-task realized graph.** A task picker (recent `task_id`s with status,
   cluster count, cost). Selecting one renders the DAG: **nodes = runs** (labeled
   by agent, **colored by cluster**), **edges = parent→child invocations**,
   grouped into **cluster swimlanes / colored hulls**. Denied-edge traversals are
   drawn **red**; a frozen node pulses. Declared-but-untouched clusters render
   greyed at the margin — *reality vs org chart on one canvas.*

2. **Cluster cost breakdown.** For the selected task: stacked cost bar **per
   cluster** — the literal CFO answer, *"processing this invoice cost $0.34:
   ingestion 4¢, extraction 22¢, validation 3¢, payments 5¢."* Aggregate views:
   per-cluster cost across all tasks, per-cluster **waste** (the existing
   cost-share − contribution-share score, now at cluster granularity), and the
   **cost-per-task distribution** (unit economics with outliers).

3. **Topology violation card.** When a task carries a governance event: a
   prominent task-scope incident — *what boundary was crossed*
   (`extraction → payments`), *which run is frozen*, *the required-but-missing
   predecessor*, *the policy that fired*, and **resume / kill buttons that act on
   the frozen child run**. The graph highlights the offending edge in red. This
   is the governance moment made human-actionable, reusing the existing
   human-action + audit flow (the click lands in the audit log with a name).

4. **Drift panel (org chart vs reality, over time).** Dead declared clusters,
   recurring undeclared pathways (promote-to-policy candidates), shadow-
   utilization leaderboard. This is the batch-analytics output from §4.

---

## 6. Demo narrative

Same `invoice-swarm`. Declared clusters: **ingestion → extraction → validation →
payments**, with `denied_edges: extraction→payments` and
`required_predecessors: payments requires validation`.

- **Task A — happy path.** Orchestrator fans out: ingestion → extraction (2 runs)
  → validation → payments. **4 clusters, 5 runs, $0.34.** The realized graph is a
  clean chain; cluster cost bar shows extraction dominating. *"That's what one
  invoice cost, by stage — a number no per-agent or per-swarm view can give
  you."*

- **Task B — different shape, same swarm.** A simpler invoice already validated
  upstream: the LLM orchestrator only touches ingestion → extraction. **2
  clusters, 2 runs, $0.08.** Show A and B graphs side by side. *"Same swarm, same
  agents, two different topologies — nobody declared either shape; they emerged,
  and Guardian traced both."* This is the dynamism proof.

- **Task C — frozen at a boundary.** A refund path where extraction tries to
  invoke payments **directly** (skipping validation). At payments' `run_start`
  Guardian fires **two** deterministic topology violations at once — **denied
  edge** `extraction→payments` *and* **missing predecessor** `validation` — and
  **freezes the payments run before any `ledger_write` runs.** The dashboard
  shows the task graph with the red `extraction→payments` edge and the pulsing
  frozen payments node; the violation card names both breaches. A human clicks
  **resume-with-override** (audited) or **kill**. *"The money never moved. A human
  decided. It's in the audit log with their name — and nobody had ever declared
  that extraction→payments path; it appeared at runtime, and Guardian named it,
  priced it, and blocked it."*

- **Kicker.** *"Cost-per-outcome, per stage. Boundaries the org chart draws but
  never enforced — now enforced against the path an LLM actually took. Same
  control plane, one new idea: govern the shape, not just the step."*

---

## 7. Open questions & edge cases (flagged, not silently decided)

1. **Agent in two clusters.** Which cluster governs a given run — and which
   `cluster(run)` do edge/shadow checks use? *Proposal:* carry a
   *cluster-on-behalf-of* hint in the invocation context (the caller states which
   cluster it's engaging); absent that, fall back to the **most-restrictive** of
   the agent's clusters, or require a declared `primary_cluster`. **Undecided —
   affects every edge/shadow evaluation.**

2. **Task spanning two swarms.** If an invoice-swarm task calls a
   notifications-swarm agent, does `task_id` cross the swarm boundary?
   *Proposal:* `task_id` is **global** (above swarm); a cross-swarm edge is its
   own governance class (default **escalate**); topology policy resolves against
   the swarm that owns the edge's **target**. **Open:** who owns the *task
   budget* when a task straddles two swarms' P&L?

3. **Retroactive cluster reassignment.** If an agent moves cluster A→B, do past
   task traces re-evaluate? *Proposal:* **snapshot `cluster(run)` onto each event
   at ingest** — the realized layer is immutable history; declared changes affect
   **future** tasks only; drift analytics compare snapshots against current
   declared. **Do NOT re-run enforcement retroactively** (audit integrity).
   Confirm this is acceptable.

4. **Task-boundary definition.** What delimits one task? Explicit `task_id` from
   the entry point is reliable; without it, where does a task begin/end (root
   run? idle timeout?). A long-lived orchestrator process that serves many
   end-user requests must mint a **per-request** `task_id`, not one per process —
   otherwise every task budget and fan-out cap is meaningless. **Needs an
   authoring convention.**

5. **Enforcing on inferred edges.** Heuristically-stitched traces may be wrong.
   Do we enforce topology on `inferred` edges (risking false freezes) or only
   observe them? *Proposal:* **observe + warn** on inferred; **hard-enforce only
   on explicitly-propagated** edges. **Open.**

6. **Cycles / retries in the graph.** A→B→A (retry, or genuine mutual
   invocation) breaks the DAG assumption behind predecessor/edge checks, and must
   be distinguished from the existing per-run *loop* detector (which is
   intra-run, not inter-run). **Needs an explicit cycle policy.**

7. **Shadow utilization vs legitimate shared services.** A `db_query` utility
   agent used by every cluster is *shared*, not *shadow*. The `shared_clusters`
   exemption in the YAML is a first cut, but the boundary ("when is broad use
   legitimate?") needs a real rule. **Open.**

8. **Fan-out limits vs legitimate parallelism.** A map-reduce over 50 line items
   is 50 legitimate child runs. `max_runs_per_task` as a raw count punishes
   healthy breadth. *Proposal:* cap **distinct agents/clusters** or **invocation
   depth**, not raw run count; or per-agent instance caps. **Undecided.**

9. **Predecessor semantics across async/parallel branches.** Does "passed
   validation" mean validation **started** or **succeeded** (emitted a success
   output)? If validation runs in a still-incomplete parallel branch when
   payments starts, ordering ≠ completion. "Succeeded" is safer but needs
   output-semantics that edge toward the non-deterministic layer. **Must be
   pinned down before payments-style gates are trustworthy.**

10. **Policy-conflict direction (§3 asymmetry).** Confirm the intended model:
    restrictive fields **monotonic/union** (tighten-only going down), scalar
    fields **most-specific-wins**. Pure most-specific-wins is simpler but lets a
    leaf loosen a separation-of-duties boundary — which would defeat the feature.
    **Explicit sign-off wanted.**
```
