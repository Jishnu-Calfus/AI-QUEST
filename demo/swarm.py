"""Invoice-processing swarm demo — the ECONOMICS story.

Four agents, one swarm ('invoice-swarm'). Three do real work with full-stack
costs (LLM + db + compute + api). One — 'reviewer-agent' — burns 60% of spend
producing nothing novel: the top waster the dashboard will name.

Also registers every agent in the workforce roster first (the 'hiring' step).
"""
from __future__ import annotations

import os
import random
import sys
import threading
import time

import httpx

sys.path.insert(0, "sdk")
sys.path.insert(0, "../sdk")
from guardian_sdk import Guardian, GuardianKilled  # noqa: E402

TICK = float(os.environ.get("DEMO_TICK", "0.8"))
BASE = os.environ.get("GUARDIAN_URL", "http://localhost:8090")
KEY = os.environ.get("GUARDIAN_API_KEY", "guardian-dev-key")
SWARM = "invoice-swarm"

ROSTER = [
    dict(agent_id="fetcher-agent", swarm_id=SWARM, owner="jishnu",
         purpose="Pull invoices from ERP + object storage", budget_usd=0.50),
    dict(agent_id="extractor-agent", swarm_id=SWARM, owner="jishnu",
         purpose="LLM extraction of line items", budget_usd=1.00),
    dict(agent_id="reporter-agent", swarm_id=SWARM, owner="jishnu",
         purpose="Aggregate totals, write report", budget_usd=0.50),
    dict(agent_id="reviewer-agent", swarm_id=SWARM, owner="jishnu",
         purpose="Double-check extractions (suspected redundant)", budget_usd=1.00),
]


def register_roster() -> None:
    with httpx.Client(timeout=5, trust_env=False) as c:
        for a in ROSTER:
            c.post(f"{BASE}/v1/agents/register", json=a,
                   headers={"X-Guardian-Key": KEY})
    print(f"registered {len(ROSTER)} agents in swarm '{SWARM}'")


def fetcher():
    g = Guardian(agent_id="fetcher-agent", swarm_id=SWARM,
                 goal="Pull all Q2 invoices from ERP and S3")
    try:
        for i in range(6):
            g.resource("db_query", qty=random.randint(3, 8), note="ERP invoice lookup")
            g.resource("storage_gb", qty=0.4, note="S3 fetch invoice PDFs")
            g.resource("compute_second", qty=random.randint(8, 15))
            g.output(f"batch {i+1}: fetched {random.randint(4, 9)} invoices from vendor set {i+1}")
            time.sleep(TICK)
        g.end()
        print("[fetcher] done")
    except GuardianKilled as e:
        print(f"[fetcher] stopped: {e}")


def extractor():
    g = Guardian(agent_id="extractor-agent", swarm_id=SWARM,
                 goal="Extract line items and totals from fetched invoices")
    try:
        for i in range(8):
            g.llm("gpt-4o-mini", f"extract line items, invoice batch {i+1}",
                  tokens_in=1800, tokens_out=350, cost_usd=0.006)
            g.resource("compute_second", qty=random.randint(4, 9))
            g.resource("ocr_page", qty=random.randint(2, 5))
            g.output(f"batch {i+1}: {random.randint(9, 18)} line items, "
                     f"total ${random.randint(700, 4300)}")
            time.sleep(TICK)
        g.end()
        print("[extractor] done")
    except GuardianKilled as e:
        print(f"[extractor] stopped: {e}")


def reporter():
    g = Guardian(agent_id="reporter-agent", swarm_id=SWARM,
                 goal="Aggregate extracted invoice data into the Q2 spend report")
    try:
        time.sleep(TICK * 4)  # waits for upstream agents
        for i in range(4):
            g.resource("db_query", qty=random.randint(2, 5), note="write aggregates")
            g.llm("gpt-4o-mini", f"aggregate invoice data into Q2 spend report section {i+1}",
                  tokens_in=900, tokens_out=280, cost_usd=0.004)
            g.output(f"Q2 spend report section {i+1}: aggregated invoice totals for "
                     f"vendor group {i+1}, anomalies noted")
            time.sleep(TICK)
        g.output("Q2 spend report finalized: aggregated invoice data, 34 vendors, "
                 "$61,204 total, 3 anomalies")
        g.end()
        print("[reporter] done")
    except GuardianKilled as e:
        print(f"[reporter] stopped: {e}")


def reviewer():
    """The waster: re-runs expensive LLM checks that add nothing new.
    High cost share, near-zero novel-output share -> top waste_score."""
    g = Guardian(agent_id="reviewer-agent", swarm_id=SWARM,
                 goal="Verify extracted invoice line items for accuracy")
    try:
        for i in range(10):
            g.llm("gpt-4o", "re-verify all line items with full context window",
                  tokens_in=6000, tokens_out=400, cost_usd=0.022)
            g.resource("gpu_second", qty=random.randint(6, 12))
            g.output("Verification pass complete. All line items match. No changes.")
            time.sleep(TICK * 0.8)
        g.end()
        print("[reviewer] done (and wasted a lot doing it)")
    except GuardianKilled as e:
        print(f"[reviewer] stopped: {e}")


def main() -> None:
    register_roster()
    threads = [threading.Thread(target=f, name=f.__name__, daemon=True)
               for f in (fetcher, extractor, reporter, reviewer)]
    for t in threads:
        t.start()
        time.sleep(0.7)
    for t in threads:
        t.join(timeout=180)
    print("swarm demo complete — check /v1/swarms for the economics")


if __name__ == "__main__":
    main()
