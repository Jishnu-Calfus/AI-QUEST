"""Runtime-subagent tracking demo — the EMERGENT-CLUSTER story.

Declared org chart for 'invoice-swarm' (from policies.yaml):
    ingestion → extraction → validation → payments

But at runtime the extraction agent — an LLM orchestrator — spins up helpers
that are on NO declared cluster. Nobody drew them on the org chart; they appeared
when the model decided it needed them:

  * pdf-splitter-1 / pdf-splitter-2  — same tools (ocr_page + gpt-4o-mini):
        Guardian discovers them as ONE emergent cluster (runtime:ocr_page).
  * web-enricher                     — a different tool (web_search): its own
        emergent cluster (runtime:web_search).
  * auto-pay-helper                  — an undeclared agent that reaches into the
        PAYMENTS cluster: flagged HIGH RISK (off-chart code touching money).

The only integration effort is context propagation: each agent hands the next
its g.context() token. Guardian derives the emergent clusters, their cost, their
reach, and the risk flags from that alone. See them on the dashboard's
"Runtime subagents" tab (http://localhost:8090/runtime).

Usage:  python runtime.py
"""
from __future__ import annotations

import os
import sys
import time
import uuid

import httpx

sys.path.insert(0, "sdk")
sys.path.insert(0, "../sdk")
from guardian_sdk import Guardian, GuardianKilled  # noqa: E402

TICK = float(os.environ.get("DEMO_TICK", "0.25"))
BASE = os.environ.get("GUARDIAN_URL", "http://localhost:8090")
KEY = os.environ.get("GUARDIAN_API_KEY", "guardian-dev-key")
SWARM = "invoice-swarm"

# declared org chart — the four legitimate clusters. The runtime helpers below
# are deliberately NOT registered: that is the whole point.
ROSTER = [
    dict(agent_id="ingest-agent",   cluster="ingestion",  owner="maya",
         purpose="Pull invoices from ERP + object storage"),
    dict(agent_id="extract-agent",  cluster="extraction", owner="maya",
         purpose="OCR + LLM line-item extraction"),
    dict(agent_id="validate-agent", cluster="validation", owner="raj",
         purpose="Rules + LLM verification before money moves"),
    dict(agent_id="pay-agent",      cluster="payments",   owner="raj",
         purpose="Write to the ledger / initiate transfers"),
]


def register_roster() -> None:
    with httpx.Client(timeout=5, trust_env=False) as c:
        for a in ROSTER:
            c.post(f"{BASE}/v1/agents/register",
                   json={**a, "swarm_id": SWARM, "budget_usd": 1.0},
                   headers={"X-Guardian-Key": KEY})
    print(f"registered {len(ROSTER)} DECLARED agents into '{SWARM}' clusters")


def declared_step(agent_id: str, goal: str, ctx: dict, work) -> dict:
    """Run one declared-cluster agent, return its context token for the next hop."""
    g = Guardian(agent_id=agent_id, swarm_id=SWARM, goal=goal, **ctx)
    work(g)
    g.end()
    return g.context()


def runtime_subagent(agent_id: str, goal: str, parent_ctx: dict, work) -> dict:
    """An OFF-CHART agent the orchestrator spun up mid-run. Same SDK, but this
    agent_id was never registered to a cluster — Guardian will track it on the
    Runtime subagents tab and group it with its behavioural peers."""
    g = Guardian(agent_id=agent_id, swarm_id=SWARM, goal=goal, **parent_ctx)
    try:
        work(g)
        g.end()
    except GuardianKilled as e:
        print(f"  [{agent_id}] frozen by Guardian: {e}")
    return g.context()


def main() -> None:
    register_roster()
    tid = "RT-" + uuid.uuid4().hex[:6]
    print(f"[task {tid}] invoice with runtime-spawned helpers")

    # 1) declared happy path start: ingestion → extraction
    ctx = {"task_id": tid}
    ctx = declared_step("ingest-agent", "Ingest INV-7781", ctx, lambda g: (
        g.resource("db_query", qty=4, note="ERP lookup"),
        g.resource("storage_gb", qty=0.4, note="S3 fetch PDF"),
        g.output("pulled invoice INV-7781, vendor Initech, $8,600")))
    time.sleep(TICK)

    extract_ctx = declared_step("extract-agent", "Extract line items for INV-7781", ctx,
        lambda g: (
            g.tool("ocr_page", "page 1-3"),
            g.llm("gpt-4o-mini", "extract line items from INV-7781", 1600, 300, 0.006),
            g.output("18 line items extracted, total $8,600")))
    time.sleep(TICK)

    # 2) extraction spins up OFF-CHART helpers (emergent cluster 1: same tools)
    print("  extraction spun up 2 undeclared pdf-splitter helpers (emergent cluster)")
    for n in (1, 2):
        runtime_subagent(f"pdf-splitter-{n}", "Split multi-invoice PDF into pages",
            extract_ctx, lambda g: (
                g.tool("ocr_page", "detect page boundaries"),
                g.llm("gpt-4o-mini", "classify each page as invoice/attachment", 900, 120, 0.004),
                g.output("split into 6 single-invoice PDFs")))
        time.sleep(TICK)

    # 3) a different off-chart helper (emergent cluster 2: different tool)
    print("  extraction spun up an undeclared web-enricher (2nd emergent cluster)")
    runtime_subagent("web-enricher", "Enrich vendor Initech with public data",
        extract_ctx, lambda g: (
            g.tool("web_search", "Initech Inc tax id headquarters"),
            g.tool("web_search", "Initech Inc D-U-N-S number"),
            g.output("matched vendor to DUNS 08-146-2199")))
    time.sleep(TICK)

    # 4) declared validation runs (so payments has its required predecessor)
    val_ctx = declared_step("validate-agent", "Validate INV-7781 before payment",
        extract_ctx, lambda g: (
            g.tool("rules_check", "3-way match PO/GRN/invoice"),
            g.llm("gpt-4o-mini", "verify totals and approval chain", 700, 120, 0.003),
            g.output("validation PASSED: match ok, within approval limit")))
    time.sleep(TICK)

    # 5) HIGH RISK: an undeclared helper orchestrates the payment itself, reaching
    #    straight into the payments cluster. Validation ran, so it is not frozen —
    #    but off-chart code touching money is exactly what /runtime must surface.
    print("  ⚠ an undeclared auto-pay-helper is orchestrating the payment (HIGH RISK)")
    pay_ctx = runtime_subagent("auto-pay-helper", "Auto-approve and trigger payment",
        val_ctx, lambda g: (
            g.tool("db_query", "load payee bank account for Initech"),
            g.output("payee resolved, ready to disburse $8,600")))
    time.sleep(TICK)

    # the declared payments agent, but invoked BY the off-chart helper (emergent → payments)
    declared_step("pay-agent", "Pay INV-7781", pay_ctx, lambda g: (
        g.tool("ledger_write", "transfer $8,600 to Initech"),
        g.output("payment of $8,600 initiated")))

    print(f"[task {tid}] done — open http://localhost:8090/runtime")


import builtins as _b  # noqa: E402
_p = _b.print
_b.print = lambda *a, **k: _p(*a, **{**k, "flush": True})

if __name__ == "__main__":
    main()
