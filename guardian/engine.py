"""Action engine: signals + verdicts -> graduated intervention ladder with hysteresis.
Also the swarm-level rollup: full-stack cost aggregation + waste analytics.

Suspicion scoring (decays as healthy steps pass, so one-off blips don't flap):
  warn signal  +1.5   high signal +3.0   critical -> immediate policy action
Ladder: observe -> warn -> pause -> kill; escalate = pause + human decision required.
"""
from __future__ import annotations

import asyncio
import time
from typing import Callable, Optional

import httpx

from .config import Policy, settings
from .costs import CATEGORIES, catalog
from .detectors import RunState, run_detectors
from .judge import explain_incident, judge_run
from .models import (AgentEvent, ControlState, Incident, RunStatus, Severity,
                     Signal, Verdict)
from .store import Store

DECAY = 0.85          # suspicion multiplier per healthy event
WEIGHTS = {Severity.info: 0.0, Severity.warn: 1.5, Severity.high: 3.0}


class Engine:
    def __init__(self, store: Store, notify: Optional[Callable] = None):
        self.store = store
        self.runs: dict[str, RunStatus] = {}
        self.states: dict[str, RunState] = {}
        self.notify = notify or (lambda kind, payload: None)  # SSE broadcaster

    # ---------- public API ----------

    async def handle_event(self, ev: AgentEvent) -> ControlState:
        run = self.runs.get(ev.run_id)
        if run is None:
            run = RunStatus(run_id=ev.run_id, agent_id=ev.agent_id,
                            swarm_id=ev.swarm_id, goal=ev.goal)
            self.runs[ev.run_id] = run
            self.states[ev.run_id] = RunState(goal=ev.goal or "")
            self.store.audit(ev.run_id, "guardian", "run_registered",
                             f"agent={ev.agent_id} swarm={ev.swarm_id}")
        st = self.states[ev.run_id]
        pol = settings.policy_for(ev.agent_id)

        # terminal states short-circuit
        if run.state in (ControlState.killed, ControlState.finished):
            return run.state

        # --- price the event (lane 1: live metering) ---
        ev.cost_usd = catalog.price(ev)
        cat = catalog.categorize(ev)

        if not self.store.save_event(ev):          # duplicate event_id
            return run.state

        if ev.type.value == "run_end":
            run.state = ControlState.finished
            self._sync(run, st)
            self.notify("run", run.model_dump())
            return run.state

        signals = run_detectors(ev, st, pol)
        run.cost_by_cat[cat] = round(run.cost_by_cat.get(cat, 0.0) + ev.cost_usd, 6)
        self._sync(run, st)
        run.last_seen = time.time()

        # --- criticals act immediately ---
        crits = [s for s in signals if s.severity == Severity.critical]
        if crits and run.state not in (ControlState.killed,):
            action = self._critical_action(crits, pol)
            await self._open_incident(run, crits, action,
                                      title=crits[0].reason.split(":")[0])
        else:
            # --- suspicion ladder with decay ---
            inc = sum(WEIGHTS.get(s.severity, 0.0) for s in signals)
            run.suspicion = run.suspicion * DECAY + inc
            if signals:
                run.last_reason = signals[-1].reason
            active = run.state in (ControlState.running, ControlState.warned)
            if active and run.suspicion >= pol.kill_threshold:
                await self._open_incident(run, signals or self._synth(run), "kill",
                                          title="Runaway behaviour — kill threshold crossed")
            elif active and run.suspicion >= pol.pause_threshold:
                await self._open_incident(run, signals or self._synth(run), "pause",
                                          title="Sustained anomalous behaviour")
            elif run.state == ControlState.running and run.suspicion >= pol.warn_threshold:
                await self._open_incident(run, signals or self._synth(run), "warn",
                                          title="Suspicious behaviour forming")

        # --- periodic / triggered L2 judge (async, non-blocking) ---
        judge_due = (st.steps % max(1, pol.judge_every_n_steps) == 0
                     or run.suspicion >= pol.warn_threshold)
        if judge_due and run.state in (ControlState.running, ControlState.warned):
            asyncio.create_task(self._judge(run, st, pol))

        self.notify("event", {"run_id": ev.run_id, "agent_id": ev.agent_id,
                              "swarm_id": ev.swarm_id, "type": ev.type.value,
                              "name": ev.name, "content": (ev.content or "")[:160],
                              "cost": round(st.cost_usd, 4), "ts": ev.ts})
        self.notify("run", run.model_dump())
        return run.state

    def control(self, run_id: str) -> ControlState:
        run = self.runs.get(run_id)
        return run.state if run else ControlState.running

    async def human_action(self, run_id: str, action: str, who: str = "operator") -> RunStatus:
        """Dashboard buttons: pause / resume / kill."""
        run = self.runs[run_id]
        if action == "resume":
            run.state = ControlState.running
            run.suspicion = 0.0
        elif action == "pause":
            run.state = ControlState.paused
        elif action == "kill":
            run.state = ControlState.killed
        self.store.audit(run_id, who, f"human_{action}")
        self.notify("run", run.model_dump())
        return run

    # ---------- swarm rollup: cost + waste analytics ----------

    def swarm_summary(self) -> list[dict]:
        """Per-swarm: total fully-loaded cost, category breakdown, per-agent share,
        and waste score = cost share minus contribution share (novel outputs)."""
        swarms: dict[str, dict] = {}
        for run in self.runs.values():
            s = swarms.setdefault(run.swarm_id, {
                "swarm_id": run.swarm_id, "agents": {}, "total_usd": 0.0,
                "by_category": {c: 0.0 for c in CATEGORIES}, "runs": 0,
            })
            s["runs"] += 1
            s["total_usd"] = round(s["total_usd"] + run.cost_usd, 6)
            for c, v in run.cost_by_cat.items():
                s["by_category"][c] = round(s["by_category"].get(c, 0.0) + v, 6)
            a = s["agents"].setdefault(run.agent_id, {
                "agent_id": run.agent_id, "cost_usd": 0.0,
                "outputs_novel": 0, "outputs_total": 0, "state": run.state.value})
            a["cost_usd"] = round(a["cost_usd"] + run.cost_usd, 6)
            a["outputs_novel"] += run.outputs_novel
            a["outputs_total"] += run.outputs_total
            a["state"] = run.state.value

        out = []
        for s in swarms.values():
            agents = list(s["agents"].values())
            total_cost = s["total_usd"] or 1e-9
            total_novel = sum(a["outputs_novel"] for a in agents) or 1e-9
            for a in agents:
                cost_share = a["cost_usd"] / total_cost
                contrib_share = a["outputs_novel"] / total_novel
                a["cost_share"] = round(cost_share, 3)
                a["contribution_share"] = round(contrib_share, 3)
                a["waste_score"] = round(max(0.0, cost_share - contrib_share), 3)
            agents.sort(key=lambda a: -a["waste_score"])
            s["agents"] = agents
            s["top_waster"] = (agents[0]["agent_id"]
                               if agents and agents[0]["waste_score"] > 0.15 else None)
            out.append(s)
        return sorted(out, key=lambda s: -s["total_usd"])

    # ---------- internals ----------

    def _sync(self, run: RunStatus, st: RunState) -> None:
        run.steps, run.tokens, run.cost_usd = st.steps, st.tokens, round(st.cost_usd, 6)
        run.outputs_total, run.outputs_novel = st.outputs_total, st.outputs_novel
        if st.goal:
            run.goal = st.goal

    def _synth(self, run: RunStatus) -> list[Signal]:
        return [Signal(detector="ladder", severity=Severity.warn,
                       reason=run.last_reason or "accumulated suspicion")]

    def _critical_action(self, crits: list[Signal], pol: Policy) -> str:
        detectors = {c.detector for c in crits}
        if "policy" in detectors:
            return pol.policy_action
        if "budget" in detectors:
            return pol.budget_action
        return "pause"  # runtime overruns default to pause

    async def _judge(self, run: RunStatus, st: RunState, pol: Policy) -> None:
        try:
            events = self.store.recent_events(run.run_id, 15)
            stats = {"steps": st.steps, "tokens": st.tokens,
                     "cost_usd": round(st.cost_usd, 4), "suspicion": round(run.suspicion, 1)}
            v: Verdict = await judge_run(st.goal, events, stats)
            self.store.audit(run.run_id, f"judge:{v.provider}", f"verdict_{v.verdict}",
                             f"drift={v.drift_score} {v.reasoning[:180]}")
            self.notify("verdict", {"run_id": run.run_id, **v.model_dump()})
            if v.verdict in ("off_goal", "unsafe") and run.state in (
                    ControlState.running, ControlState.warned):
                sig = Signal(detector="judge", severity=Severity.high,
                             reason=f"Judge ({v.provider}): {v.verdict} "
                                    f"drift={v.drift_score:.2f} — {v.reasoning}",
                             evidence={"drift_score": v.drift_score})
                await self._open_incident(run, [sig], pol.drift_action,
                                          title=f"Goal drift detected ({v.verdict})")
        except Exception as exc:      # judge must never crash the engine
            self.store.audit(run.run_id, "judge", "judge_error", repr(exc)[:200])

    async def _open_incident(self, run: RunStatus, signals: list[Signal],
                             action: str, title: str) -> None:
        transition = {
            "warn": ControlState.warned,
            "pause": ControlState.paused,
            "kill": ControlState.killed,
            "escalate": ControlState.escalated,
        }.get(action)
        if transition is None:
            return
        # never downgrade a terminal/paused state via automation
        order = [ControlState.running, ControlState.warned, ControlState.paused,
                 ControlState.escalated, ControlState.killed]
        if order.index(transition) <= order.index(run.state) and run.state != ControlState.running:
            return
        run.state = transition
        run.last_reason = signals[0].reason if signals else title

        explanation = await explain_incident(title, signals, run.goal or "")
        inc = Incident(run_id=run.run_id, agent_id=run.agent_id, action=action,
                       severity=max((s.severity for s in signals), default=Severity.warn),
                       title=title, explanation=explanation, signals=signals)
        self.store.save_incident(inc)
        self.store.audit(run.run_id, "guardian", f"action_{action}", title)
        self.notify("incident", inc.model_dump())
        self.notify("run", run.model_dump())
        await self._slack(inc)

    async def _slack(self, inc: Incident) -> None:
        if not settings.slack_webhook:
            return
        try:
            async with httpx.AsyncClient(timeout=5) as c:
                await c.post(settings.slack_webhook, json={
                    "text": f":rotating_light: Guardian {inc.action.upper()} — "
                            f"agent `{inc.agent_id}` run `{inc.run_id[:8]}`\n"
                            f"*{inc.title}*\n{inc.explanation}"})
        except Exception:
            pass  # alerting failure must not affect enforcement
