"""Sub-cluster governance demo — the DYNAMIC-TOPOLOGY story.

One swarm ('invoice-swarm'), agents assigned to declared clusters
(ingestion -> extraction -> validation -> payments). We then run THREE
end-to-end tasks whose realized shapes differ — because the path is emergent,
not declared:

  Task A  ingestion -> extraction -> validation -> payments   (happy path)
  Task B  ingestion -> extraction                             (shorter shape)
  Task C  ingestion -> extraction -> payments                 (VIOLATION:
          crosses the denied extraction->payments edge AND skips validation;
          the payments run is frozen mid-task before any ledger_write.)

Context propagation is the whole integration: each agent hands the next its
`g.context()` token, and Guardian derives the cross-agent graph from that.

Usage:  python clusters.py            # all three tasks
        python clusters.py C          # just the violation task
"""
from __future__ import annotations

import os
import sys
import threading
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

# declared org chart: agent -> cluster (the human-authored intent)
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
    print(f"registered {len(ROSTER)} agents into '{SWARM}' clusters")


# ---- per-cluster work (each starts with a cheap checkpoint, THEN the real act) ----

def w_ingestion(g: Guardian) -> None:
    g.resource("db_query", qty=4, note="ERP invoice lookup")
    g.resource("storage_gb", qty=0.4, note="fetch invoice PDF from S3")
    g.output("pulled invoice INV-4821, vendor Globex, $4,200")


def w_extraction(g: Guardian) -> None:
    g.resource("ocr_page", qty=3)
    g.llm("gpt-4o-mini", "extract line items from INV-4821", 1600, 300, 0.006)
    g.output("12 line items extracted, total $4,200")


def w_validation(g: Guardian) -> None:
    g.tool("rules_check", "3-way match PO/GRN/invoice")
    g.llm("gpt-4o-mini", "verify totals and approval chain", 700, 120, 0.003)
    g.output("validation PASSED: match ok, within approval limit")


def w_payments(g: Guardian) -> None:
    # first event is a benign checkpoint — topology enforcement freezes the run
    # HERE (before the ledger write) if this hop is illegal.
    g.tool("db_query", "load payee bank account for Globex")
    g.tool("ledger_write", "transfer $4,200 to Globex")     # must not run if frozen
    g.output("payment of $4,200 initiated")


def run_chain(task_id: str, steps: list) -> None:
    """steps: list of (agent_id, goal, work_fn). Each agent invokes the next by
    passing its context token down — that hop is the only integration effort."""
    ctx = {"task_id": task_id}          # root: task_id set, no parent
    for agent_id, goal, work in steps:
        g = Guardian(agent_id=agent_id, swarm_id=SWARM, goal=goal, **ctx)
        try:
            work(g)
            g.end()
        except GuardianKilled as e:
            print(f"  [{agent_id}] killed by Guardian: {e}")
            return
        # if the run was frozen (escalate/pause), the SDK is still blocking inside
        # work() above and we never reach here — exactly the "frozen mid-run" state
        ctx = g.context()               # next agent's parent = this run


def task_a() -> None:
    tid = "A-" + uuid.uuid4().hex[:6]
    print(f"[task A {tid}] happy path: ingestion -> extraction -> validation -> payments")
    run_chain(tid, [
        ("ingest-agent",   "Ingest invoice INV-4821",              w_ingestion),
        ("extract-agent",  "Extract line items for INV-4821",      w_extraction),
        ("validate-agent", "Validate INV-4821 before payment",     w_validation),
        ("pay-agent",      "Pay INV-4821",                         w_payments),
    ])
    print(f"[task A {tid}] done")


def task_b() -> None:
    tid = "B-" + uuid.uuid4().hex[:6]
    print(f"[task B {tid}] shorter shape: ingestion -> extraction (pre-validated)")
    run_chain(tid, [
        ("ingest-agent",  "Ingest invoice INV-4822",         w_ingestion),
        ("extract-agent", "Extract line items for INV-4822", w_extraction),
    ])
    print(f"[task B {tid}] done")


def task_c() -> None:
    tid = "C-" + uuid.uuid4().hex[:6]
    print(f"[task C {tid}] VIOLATION: extraction invokes payments directly (skips validation)")
    run_chain(tid, [
        ("ingest-agent",  "Ingest refund INV-4823",           w_ingestion),
        ("extract-agent", "Extract refund INV-4823",          w_extraction),
        ("pay-agent",     "Refund INV-4823 immediately",      w_payments),  # frozen here
    ])
    print(f"[task C {tid}] chain returned (payments should be frozen, not paid)")


TASKS = {"A": task_a, "B": task_b, "C": task_c}

import builtins as _b  # noqa: E402
_p = _b.print
_b.print = lambda *a, **k: _p(*a, **{**k, "flush": True})


def main() -> None:
    register_roster()
    which = [x.upper() for x in sys.argv[1:]] or ["A", "B", "C"]
    threads = []
    for name in which:
        t = threading.Thread(target=TASKS[name], name=f"task-{name}", daemon=True)
        t.start()
        threads.append(t)
        time.sleep(3)          # stagger so the dashboard tells a story
    for t in threads:
        t.join(timeout=30)     # task C stays frozen (blocked) — expected
    print("cluster demo dispatched — see /v1/tasks and the dashboard")


if __name__ == "__main__":
    main()
