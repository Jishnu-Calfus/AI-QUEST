"""Drift demo — produces clear agent-drift and model-drift signals.

Each agent runs a BASELINE phase then a RECENT phase; the drift section splits
the history in half and measures the shift. Nothing here is special-cased — it
just emits normal events; drift is inferred from the distributions.

  summary-bot / gpt-4o-mini : output balloons 140 → 500 tokens, cost 5x
                              -> MODEL drift (gpt-4o-mini) + agent cost drift
  router-bot                : flips from llm+varied-output to tool-spam+repeat
                              -> AGENT drift (action mix, tool mix, novelty)
  steady-bot / claude-haiku : unchanged  -> control, stays 'stable'

Usage:  python drift_demo.py
"""
from __future__ import annotations

import os
import random
import sys
import threading
import time

sys.path.insert(0, "sdk")
sys.path.insert(0, "../sdk")
from guardian_sdk import Guardian, GuardianKilled  # noqa: E402

TICK = float(os.environ.get("DEMO_TICK", "0.05"))
SWARM = "drift-demo"
N = 10


def summary_bot():
    """Model drift: gpt-4o-mini's outputs balloon over time (cost + tokens)."""
    g = Guardian(agent_id="summary-bot", swarm_id=SWARM,
                 goal="Summarize incoming support tickets")
    try:
        for i in range(N):          # baseline: tight, cheap summaries
            g.llm("gpt-4o-mini", f"summarize ticket {i}", tokens_in=780,
                  tokens_out=140 + random.randint(-8, 8), cost_usd=0.0018)
            g.output(f"ticket {i}: refund request, 2 line items, resolved")
            time.sleep(TICK)
        for i in range(N):          # recent: same model, output balloons ~3.5x
            g.llm("gpt-4o-mini", f"summarize ticket {N+i}", tokens_in=815,
                  tokens_out=500 + random.randint(-20, 20), cost_usd=0.0092)
            g.output(f"ticket {N+i}: refund request with extended chain-of-thought "
                     f"reasoning, seven clarifying sub-points, and a verbose recap "
                     f"paragraph that keeps going well beyond what was asked here")
            time.sleep(TICK)
        g.end()
        print("[summary-bot] done (model output drifted)")
    except GuardianKilled as e:
        print(f"[summary-bot] {e}")


def router_bot():
    """Agent drift: flips from reasoning+varied output to tool-spam+repetition."""
    g = Guardian(agent_id="router-bot", swarm_id=SWARM,
                 goal="Route each request to the right handler")
    try:
        for i in range(N):          # baseline: llm reasoning, distinct outputs
            g.llm("gpt-4o", f"decide handler for request {i}", 300, 80, cost_usd=0.002)
            g.output(f"routed request {i} to handler {chr(65 + i % 8)} — matched intent")
            time.sleep(TICK)
        for i in range(N):          # recent: flips to web_search, repetitive dead-end output
            g.tool("web_search", f"route ambiguous request case {N+i}", cost_usd=0.005)
            g.output("no confident match found, escalating to fallback")
            time.sleep(TICK)
        g.end()
        print("[router-bot] done (behaviour drifted)")
    except GuardianKilled as e:
        print(f"[router-bot] {e}")


def steady_bot():
    """Control: consistent model, cost, and varied output — should stay stable."""
    g = Guardian(agent_id="steady-bot", swarm_id=SWARM,
                 goal="Classify documents by type")
    cats = ["invoice", "contract", "receipt", "statement"]
    try:
        for i in range(2 * N):
            g.llm("claude-haiku-4-5", f"classify document {i}", 500,
                  120 + random.randint(-6, 6), cost_usd=0.003)
            g.output(f"document {i}: {cats[i % 4]} (confidence {0.9 + random.random()*0.09:.2f})")
            time.sleep(TICK)
        g.end()
        print("[steady-bot] done (stable, as expected)")
    except GuardianKilled as e:
        print(f"[steady-bot] {e}")


import builtins as _b  # noqa: E402
_p = _b.print
_b.print = lambda *a, **k: _p(*a, **{**k, "flush": True})

if __name__ == "__main__":
    threads = [threading.Thread(target=f, daemon=True)
               for f in (summary_bot, router_bot, steady_bot)]
    for t in threads:
        t.start()
        time.sleep(0.4)
    for t in threads:
        t.join(timeout=60)
    print("drift demo complete — open /drift")
