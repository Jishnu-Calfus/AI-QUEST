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
from .detectors import RunState, _jaccard, _norm_tokens, run_detectors
from .judge import diagnose_run, explain_incident, judge_run
from .models import (AgentEvent, ControlState, EventType, Incident, RunStatus,
                     Severity, Signal, Verdict)
from .store import Store

# node colour/type map used by the agent-level trace graph
_LOOP_SIM = 0.85          # jaccard threshold for "same action repeated"


def _action_key(e: AgentEvent) -> set:
    """Token set identifying an action — MUST match detectors.run_detectors so
    the analysis view agrees with what the loop detector actually fired on."""
    return _norm_tokens(f"{e.type.value}:{e.name or ''} {e.content or ''}")

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

    # ---------- agent-level tracing / analysis ----------

    async def agent_analysis(self, agent_id: str) -> dict:
        """Everything a developer needs to debug, govern and monitor ONE agent:
        aggregated cost/tokens, full trace, tool-call rollup, a trace/tool graph,
        governing policy, incidents, audit trail, and a root-cause diagnosis
        (with explicit loop analysis when the agent looped and was stopped)."""
        runs = sorted((r for r in self.runs.values() if r.agent_id == agent_id),
                      key=lambda r: r.started)
        profile = self.store.get_agent(agent_id)
        pol = settings.policy_for(agent_id)
        events = self.store.events_by_agent(agent_id)
        incidents = [i for i in self.store.incidents(500) if i["agent_id"] == agent_id]
        run_ids = {r.run_id for r in runs} or {e.run_id for e in events}
        audit = [a for a in self.store.audit_log(1500) if a["run_id"] in run_ids]

        # --- single pass over the trace: cost, tools, timeline, graph ---
        cost_by_cat = {c: 0.0 for c in CATEGORIES}
        tool_calls: dict[str, dict] = {}
        trace: list[dict] = []
        tokens = 0
        seen_by_name: dict[str, list[set]] = {}
        nodes: dict[str, dict] = {}
        order: dict[str, int] = {}
        edges: dict[tuple[str, str], int] = {}
        prev_node: str | None = None

        for seq, e in enumerate(events):
            cat = catalog.categorize(e)
            cost = e.cost_usd or 0.0
            tok = (e.tokens_in or 0) + (e.tokens_out or 0)
            tokens += tok
            if e.type not in (EventType.run_end, EventType.run_start):
                cost_by_cat[cat] = round(cost_by_cat.get(cat, 0.0) + cost, 6)

            is_repeat = False
            if e.type in (EventType.tool_call, EventType.llm_call):
                key = e.name or e.type.value
                tc = tool_calls.setdefault(key, {"name": key, "type": e.type.value,
                                                 "count": 0, "cost_usd": 0.0, "tokens": 0})
                tc["count"] += 1
                tc["cost_usd"] = round(tc["cost_usd"] + cost, 6)
                tc["tokens"] += tok
                toks = _action_key(e)
                is_repeat = any(_jaccard(p, toks) >= _LOOP_SIM
                                for p in seen_by_name.get(key, []))
                seen_by_name.setdefault(key, []).append(toks)

            trace.append({
                "seq": seq + 1, "ts": e.ts, "type": e.type.value,
                "name": e.name or "", "content": (e.content or "")[:400],
                "tokens_in": e.tokens_in, "tokens_out": e.tokens_out,
                "cost_usd": round(cost, 6), "category": cat,
                "is_repeat": is_repeat, "run_id": e.run_id,
            })

            # --- graph: distinct action nodes + sequential transitions ---
            nid = self._node_id(e)
            if nid is None:
                continue
            if nid not in nodes:
                order[nid] = len(order)
                nodes[nid] = {"id": nid, "label": nid, "type": e.type.value,
                              "count": 0, "cost": 0.0}
            nodes[nid]["count"] += 1
            nodes[nid]["cost"] = round(nodes[nid]["cost"] + cost, 6)
            if prev_node is not None:
                edges[(prev_node, nid)] = edges.get((prev_node, nid), 0) + 1
            prev_node = nid

        edge_list = [
            {"from": a, "to": b, "count": n,
             # a back-edge (target first appeared no later than source) or a
             # self-transition is a cycle → the visual signature of a loop
             "loop": order[b] <= order[a]}
            for (a, b), n in edges.items()
        ]
        graph = {"nodes": list(nodes.values()), "edges": edge_list}

        # --- rollups ---
        total_cost = round(sum(cost_by_cat.values()), 6)
        steps = sum(r.steps for r in runs) if runs else \
            len([e for e in events if e.type not in (EventType.run_end, EventType.run_start)])
        if runs:
            duration = round(max(r.last_seen for r in runs) - min(r.started for r in runs), 2)
            state = runs[-1].state.value
            goal = next((r.goal for r in reversed(runs) if r.goal), "")
            suspicion = round(max(r.suspicion for r in runs), 2)
            novel = sum(r.outputs_novel for r in runs)
            outputs_total = sum(r.outputs_total for r in runs)
        else:
            duration = round((events[-1].ts - events[0].ts), 2) if events else 0.0
            state = "unknown"
            goal = next((e.goal for e in events if e.goal), "") if events else ""
            suspicion = 0.0
            novel = outputs_total = 0

        waste = {"cost_share": 0.0, "contribution_share": 0.0, "waste_score": 0.0}
        for s in self.swarm_summary():
            for a in s["agents"]:
                if a["agent_id"] == agent_id:
                    waste = {k: a[k] for k in
                             ("cost_share", "contribution_share", "waste_score")}

        # --- root-cause diagnosis (loop-aware) ---
        loop = self._loop_diagnosis(events, incidents, runs[-1] if runs else None)
        narrative = await diagnose_run(goal, events, incidents)
        terminating_inc = any(i["action"] in ("pause", "kill", "escalate")
                              for i in incidents)
        if loop.get("detected"):
            root_cause = self._loop_root_cause(loop)
            fix = ("Add a progress check / cache so identical calls short-circuit, "
                   "or tighten policy (loop_repeats / stall_window). "
                   + (narrative.get("fix_suggestion") or ""))
        elif incidents and not terminating_inc and state in ("finished", "running", "warned"):
            # warnings fired but nothing ever escalated — say so honestly
            root_cause = (
                f"Completed within policy. {len(incidents)} warning-level signal(s) fired "
                f"(e.g. “{incidents[0]['title']}”) but none escalated to pause/kill, so the "
                f"run was allowed to finish. If the output was wrong, the fault is in agent "
                f"logic, not runtime behaviour.")
            fix = "Review the warned steps; tighten thresholds only if this pattern recurs."
        else:
            root_cause = narrative.get("root_cause", "")
            fix = narrative.get("fix_suggestion", "")

        return {
            "agent_id": agent_id,
            "profile": profile,
            "swarm_id": (runs[-1].swarm_id if runs else
                         (profile or {}).get("swarm_id", "default")),
            "goal": goal,
            "state": state,
            "summary": {
                "cost_usd": total_cost, "cost_by_cat": cost_by_cat,
                "tokens": tokens, "steps": steps, "duration_s": duration,
                "outputs_total": outputs_total, "outputs_novel": novel,
                "suspicion": suspicion, **waste,
            },
            "policy": {
                "max_cost_usd": pol.max_cost_usd, "max_tokens": pol.max_tokens,
                "max_steps": pol.max_steps, "max_duration_s": pol.max_duration_s,
                "loop_repeats": pol.loop_repeats, "loop_similarity": pol.loop_similarity,
                "stall_window": pol.stall_window, "denied_tools": pol.denied_tools,
                "allowed_tools": pol.allowed_tools, "denied_patterns": pol.denied_patterns,
                "pii_block": pol.pii_block, "warn_threshold": pol.warn_threshold,
                "pause_threshold": pol.pause_threshold, "kill_threshold": pol.kill_threshold,
                "budget_action": pol.budget_action, "policy_action": pol.policy_action,
                "drift_action": pol.drift_action,
            },
            "runs": [r.model_dump() for r in runs],
            "tool_calls": sorted(tool_calls.values(), key=lambda t: -t["cost_usd"]),
            "trace": trace,
            "graph": graph,
            "incidents": incidents,
            "audit": audit,
            "diagnosis": {"root_cause": root_cause, "fix_suggestion": fix,
                          "loop": loop, "provider": narrative.get("provider", "mock")},
        }

    @staticmethod
    def _node_id(e: AgentEvent) -> str | None:
        if e.type in (EventType.run_start, EventType.run_end):
            return None
        if e.type == EventType.output:
            return "output"
        if e.type == EventType.error:
            return "error"
        return e.name or e.type.value

    def _loop_diagnosis(self, events: list[AgentEvent], incidents: list[dict],
                        run: RunStatus | None) -> dict:
        """Deterministic loop root-cause: find the dominant repeated action,
        how many times / how similar, what it produced, and what stopped it."""
        actionable = [(i, e) for i, e in enumerate(events)
                      if e.type in (EventType.tool_call, EventType.llm_call)]
        clusters: list[dict] = []
        for idx, e in actionable:
            name = e.name or e.type.value
            toks = _action_key(e)
            for cl in clusters:
                if cl["name"] == name and _jaccard(cl["rep"], toks) >= _LOOP_SIM:
                    cl["idxs"].append(idx)
                    cl["sims"].append(_jaccard(cl["rep"], toks))
                    if e.content and len(cl["samples"]) < 4:
                        cl["samples"].append(e.content[:100])
                    break
            else:
                clusters.append({"name": name, "rep": toks, "idxs": [idx], "sims": [1.0],
                                 "samples": ([e.content[:100]] if e.content else [])})

        loop_inc = next((i for i in incidents if any(
            s.get("detector") in ("loop", "stall") for s in i.get("signals", []))), None)
        if not clusters:
            return {"detected": False}
        top = max(clusters, key=lambda c: len(c["idxs"]))
        repeats = len(top["idxs"])

        # What (if anything) stopped the run? Only a pause/kill/escalate counts as
        # "the loop got exited" — a bare warn that the agent survived does not.
        terminated_by = terminated_step = None
        if run and run.state.value in ("killed", "paused", "escalated"):
            terminated_by, terminated_step = run.state.value, run.steps
        elif loop_inc and loop_inc["action"] in ("pause", "kill", "escalate"):
            terminated_by = loop_inc["action"]

        # A loop is only a *root cause* when it repeated hard AND either stopped the
        # run or is still in flight. An agent that iterated a few times and finished
        # cleanly (e.g. one report section per step) is NOT a runaway loop.
        active = run is not None and run.state.value in ("running", "warned")
        if repeats < 3 or not (terminated_by is not None or active):
            return {"detected": False}

        first_step, last_step = top["idxs"][0] + 1, top["idxs"][-1] + 1
        avg_sim = round(sum(top["sims"]) / len(top["sims"]) * 100)
        wasted = round(sum(events[i].cost_usd or 0.0 for i in top["idxs"]), 6)
        return {
            "detected": True, "action": top["name"], "repeats": repeats,
            "similarity_pct": avg_sim, "first_step": first_step, "last_step": last_step,
            "novel_outputs": (run.outputs_novel if run else 0),
            "wasted_cost_usd": wasted, "terminated_by": terminated_by,
            "terminated_at_step": terminated_step, "samples": top["samples"],
            "incident_title": (loop_inc["title"] if loop_inc else None),
        }

    @staticmethod
    def _loop_root_cause(loop: dict) -> str:
        term = (f"Guardian {loop['terminated_by']} the run at step "
                f"{loop['terminated_at_step']}."
                if loop.get("terminated_by") else
                "The run has not yet been stopped.")
        return (
            f"Repetition loop: the agent invoked '{loop['action']}' {loop['repeats']}× "
            f"with ~{loop['similarity_pct']}% identical inputs across steps "
            f"{loop['first_step']}–{loop['last_step']}, producing only "
            f"{loop['novel_outputs']} novel output(s) — activity without progress. "
            f"{term} Root cause: the agent kept issuing near-identical calls instead of "
            f"advancing toward its goal (no new information gained between iterations)."
        )

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
