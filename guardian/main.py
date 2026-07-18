"""Guardian API server — control plane for the AI workforce.

Endpoints:
  POST /v1/events                    ingest one agent step   (auth)
  GET  /v1/runs/{run_id}/control     cooperative control check
  GET  /v1/runs                      run statuses
  POST /v1/runs/{run_id}/action      human pause/resume/kill (dashboard)
  GET  /v1/runs/{run_id}/diagnose    root-cause analysis (debugging)
  GET  /v1/swarms                    swarm cost rollup + waste analytics
  POST /v1/agents/register           register agent (workforce roster)
  GET  /v1/agents                    registry listing
  POST /v1/billing/import            import actual cloud costs (CUR lane 2)
  GET  /v1/billing/reconciliation    metered estimate vs actual, per swarm
  GET  /v1/incidents                 incident feed
  GET  /v1/audit                     audit log
  GET  /v1/stream                    SSE live feed (dashboard)
  GET  /                             dashboard UI
  GET  /healthz                      liveness
"""
from __future__ import annotations

import asyncio
import json
import os

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, StreamingResponse

from .config import settings
from .costs import CATEGORIES
from .engine import Engine
from .judge import diagnose_run
from .models import AgentEvent, AgentProfile, BillingRow, ControlResponse
from .store import Store

app = FastAPI(title="Guardian", version="0.2.0",
              description="Control plane for the AI workforce")

store = Store(settings.db_path)
_subscribers: list[asyncio.Queue] = []


def _notify(kind: str, payload: dict) -> None:
    dead = []
    for q in _subscribers:
        try:
            q.put_nowait({"kind": kind, "data": payload})
        except asyncio.QueueFull:
            dead.append(q)
    for q in dead:
        _subscribers.remove(q)


engine = Engine(store, notify=_notify)


def auth(x_guardian_key: str = Header(default="")) -> None:
    if x_guardian_key != settings.api_key:
        raise HTTPException(401, "invalid or missing X-Guardian-Key")


# ---------------- ingest & control ----------------

@app.post("/v1/events", response_model=ControlResponse, dependencies=[Depends(auth)])
async def ingest(ev: AgentEvent):
    state = await engine.handle_event(ev)
    run = engine.runs.get(ev.run_id)
    return ControlResponse(run_id=ev.run_id, state=state,
                           reason=(run.last_reason if run else ""))


@app.get("/v1/runs/{run_id}/control", response_model=ControlResponse)
async def control(run_id: str):
    run = engine.runs.get(run_id)
    return ControlResponse(run_id=run_id, state=engine.control(run_id),
                           reason=(run.last_reason if run else ""))


@app.get("/v1/runs")
async def runs():
    return [r.model_dump() for r in engine.runs.values()]


@app.post("/v1/runs/{run_id}/action")
async def human_action(run_id: str, body: dict):
    action = body.get("action", "")
    if action not in ("pause", "resume", "kill"):
        raise HTTPException(400, "action must be pause|resume|kill")
    if run_id not in engine.runs:
        raise HTTPException(404, "unknown run")
    run = await engine.human_action(run_id, action, body.get("who", "operator"))
    return run.model_dump()


# ---------------- debugging: root cause ----------------

@app.get("/v1/runs/{run_id}/diagnose")
async def diagnose(run_id: str):
    if run_id not in engine.runs:
        raise HTTPException(404, "unknown run")
    run = engine.runs[run_id]
    events = store.recent_events(run_id, 25)
    incs = [i for i in store.incidents(100) if i["run_id"] == run_id]
    result = await diagnose_run(run.goal or "", events, incs)
    return {"run_id": run_id, "agent_id": run.agent_id, "state": run.state.value,
            **result}


# ---------------- swarm economics ----------------

@app.get("/v1/swarms")
async def swarms():
    return engine.swarm_summary()


@app.post("/v1/billing/import", dependencies=[Depends(auth)])
async def billing_import(rows: list[BillingRow]):
    n = store.import_billing(rows)
    store.audit("-", "billing", "billing_import", f"{n} rows")
    _notify("billing", {"imported": n})
    return {"imported": n}


@app.get("/v1/billing/reconciliation")
async def reconciliation():
    """Lane-1 metered estimates vs lane-2 actual billing, per swarm/category."""
    actual = store.billing_by_swarm()
    out = []
    for s in engine.swarm_summary():
        act = actual.get(s["swarm_id"], {})
        cats = []
        for c in CATEGORIES:
            m, a = s["by_category"].get(c, 0.0), act.get(c, 0.0)
            if m == 0 and a == 0:
                continue
            cats.append({"category": c, "metered_usd": round(m, 4),
                         "actual_usd": round(a, 4),
                         "delta_pct": (round((a - m) / m * 100, 1) if m else None)})
        out.append({"swarm_id": s["swarm_id"],
                    "metered_total": round(s["total_usd"], 4),
                    "actual_total": round(sum(act.values()), 4),
                    "categories": cats})
    return out


# ---------------- registry (workforce roster) ----------------

@app.post("/v1/agents/register", dependencies=[Depends(auth)])
async def register(profile: AgentProfile):
    store.register_agent(profile)
    store.audit("-", "registry", "agent_registered",
                f"{profile.agent_id} owner={profile.owner}")
    _notify("registry", profile.model_dump())
    return profile.model_dump()


@app.get("/v1/agents")
async def agents():
    return store.list_agents()


@app.get("/v1/agents/{agent_id}/analysis")
async def agent_analysis(agent_id: str):
    """Agent-level tracing: full trace, tool rollup, trace graph, governing
    policy, incidents, audit trail and loop-aware root-cause diagnosis."""
    return await engine.agent_analysis(agent_id)


# ---------------- feeds ----------------

@app.get("/v1/incidents")
async def incidents():
    return store.incidents()


@app.get("/v1/audit")
async def audit_log():
    return store.audit_log()


@app.get("/v1/stream")
async def stream():
    q: asyncio.Queue = asyncio.Queue(maxsize=500)
    _subscribers.append(q)

    async def gen():
        for r in engine.runs.values():
            yield f"data: {json.dumps({'kind': 'run', 'data': r.model_dump()})}\n\n"
        try:
            while True:
                try:
                    item = await asyncio.wait_for(q.get(), timeout=15)
                    yield f"data: {json.dumps(item)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            if q in _subscribers:
                _subscribers.remove(q)

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/healthz")
async def healthz():
    if os.environ.get("ANTHROPIC_API_KEY"):
        judge = "anthropic"
    elif os.environ.get("OPENAI_API_KEY"):
        judge = f"openai-compatible ({os.environ.get('OPENAI_BASE_URL', 'api.openai.com')})"
    else:
        judge = "mock"
    return {"ok": True, "runs": len(engine.runs), "judge_provider": judge}


@app.get("/")
async def dashboard():
    path = os.path.join(os.path.dirname(__file__), "dashboard.html")
    return FileResponse(path)


@app.get("/agent")
async def agent_page():
    """Per-agent analysis landing page (reads ?agent_id= client-side)."""
    path = os.path.join(os.path.dirname(__file__), "agent.html")
    return FileResponse(path)
