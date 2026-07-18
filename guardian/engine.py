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

from .config import Policy, TopologyPolicy, settings
from .costs import CATEGORIES, catalog
from .detectors import RunState, _jaccard, _norm_tokens, run_detectors
from .judge import diagnose_run, explain_incident, judge_run
from .models import (AgentEvent, ControlState, EventType, Incident, RunStatus,
                     Severity, Signal, TaskStatus, Verdict)
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
        self.tasks: dict[str, TaskStatus] = {}          # realized-layer rollups
        self.agent_cluster: dict[str, str] = {}         # declared assignment cache
        self.notify = notify or (lambda kind, payload: None)  # SSE broadcaster
        self._load_registry_clusters()

    # ---------- declared cluster assignment ----------

    def _load_registry_clusters(self) -> None:
        for a in self.store.list_agents():
            if a.get("cluster"):
                self.agent_cluster[a["agent_id"]] = a["cluster"]

    def set_cluster(self, agent_id: str, cluster: str) -> None:
        if cluster:
            self.agent_cluster[agent_id] = cluster

    def _cluster_of(self, agent_id: str) -> str:
        """Registry assignment wins; else YAML-declared; else unassigned."""
        return self.agent_cluster.get(agent_id) or settings.declared_cluster(agent_id)

    # ---------- public API ----------

    async def handle_event(self, ev: AgentEvent) -> ControlState:
        cluster = self._cluster_of(ev.agent_id)
        task_id = ev.task_id or ev.run_id          # no context => singleton task
        is_new_run = ev.run_id not in self.runs
        run = self.runs.get(ev.run_id)
        if run is None:
            run = RunStatus(run_id=ev.run_id, agent_id=ev.agent_id,
                            swarm_id=ev.swarm_id, cluster=cluster, task_id=task_id,
                            parent_run_id=ev.parent_run_id or "", goal=ev.goal)
            self.runs[ev.run_id] = run
            self.states[ev.run_id] = RunState(goal=ev.goal or "")
            self.store.audit(ev.run_id, "guardian", "run_registered",
                             f"agent={ev.agent_id} swarm={ev.swarm_id} "
                             f"cluster={cluster or '-'} task={task_id}")
        st = self.states[ev.run_id]
        pol = settings.policy_for(ev.agent_id, ev.swarm_id, cluster)

        # terminal states short-circuit
        if run.state in (ControlState.killed, ControlState.finished):
            return run.state

        # --- price the event (lane 1: live metering) ---
        ev.cost_usd = catalog.price(ev)
        cat = catalog.categorize(ev)

        if not self.store.save_event(ev):          # duplicate event_id
            return run.state

        # --- realized-layer: stitch the task graph + topology governance ---
        await self._update_task(ev, run, cluster, task_id, is_new_run)
        if run.state in (ControlState.killed,):     # topology may have killed it
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
                              "swarm_id": ev.swarm_id, "cluster": run.cluster,
                              "task_id": run.task_id, "type": ev.type.value,
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

    # ---------- realized-layer: tasks + topology governance ----------

    async def _update_task(self, ev: AgentEvent, run: RunStatus, cluster: str,
                           task_id: str, is_new_run: bool) -> None:
        """Stitch the event into its task graph and run deterministic topology
        governance (denied edges, required predecessors, fan-out, task budget,
        shadow utilization) at the moment the shape is observed."""
        task = self.tasks.get(task_id)
        if task is None:
            task = TaskStatus(task_id=task_id, swarm_id=ev.swarm_id, goal=ev.goal or "")
            self.tasks[task_id] = task
        if ev.goal and not task.goal:
            task.goal = ev.goal
        task.last_seen = time.time()

        # full-stack cost, per task and per cluster (the CFO answer)
        if ev.type.value not in ("run_end", "run_start"):
            task.cost_usd = round(task.cost_usd + ev.cost_usd, 6)
            ck = cluster or "(unassigned)"
            task.cost_by_cluster[ck] = round(
                task.cost_by_cluster.get(ck, 0.0) + ev.cost_usd, 6)

        top = settings.topology_for(ev.swarm_id)

        if is_new_run:
            task.runs.append(ev.run_id)
            parent = self.runs.get(ev.parent_run_id) if ev.parent_run_id else None
            pc = parent.cluster if parent else ""
            edge = None
            if ev.parent_run_id:
                edge = {"from": ev.parent_run_id, "to": ev.run_id,
                        "from_cluster": pc or "(unassigned)",
                        "to_cluster": cluster or "(unassigned)", "denied": False}
            # topology checks run BEFORE recording this cluster's entry, so the
            # required-predecessor test sees only clusters that came earlier
            await self._topology_check(ev, run, cluster, pc, task, top, edge)
            if edge:
                task.edges.append(edge)
            if cluster and cluster not in task.clusters:
                task.clusters.append(cluster)

        # task budget spans every run in the task; fire once
        if (top.task_budget_usd and task.cost_usd > top.task_budget_usd
                and not any(v["kind"] == "task_budget" for v in task.violations)):
            await self._act_task_budget(ev, run, task, top)

        self.notify("task", self._task_public(task))

    async def _topology_check(self, ev: AgentEvent, run: RunStatus, cluster: str,
                              parent_cluster: str, task: TaskStatus,
                              top: TopologyPolicy, edge: Optional[dict]) -> None:
        # 1. denied cluster -> cluster edge (freeze before the child acts)
        if parent_cluster and cluster and top.is_edge_denied(parent_cluster, cluster):
            if edge:
                edge["denied"] = True
            sig = Signal(detector="topology", severity=Severity.critical,
                         reason=f"Denied cluster edge traversed: {parent_cluster} → {cluster}",
                         evidence={"from_cluster": parent_cluster, "to_cluster": cluster,
                                   "parent_run": ev.parent_run_id})
            await self._act_topology(run, task, sig, top.on_denied_edge, "denied_edge",
                                     f"Denied boundary crossed: {parent_cluster} → {cluster}")

        # 2. required predecessor missing (e.g. nothing enters payments un-validated)
        req = top.required_predecessors.get(cluster)
        if req and req not in task.clusters:
            sig = Signal(detector="topology", severity=Severity.critical,
                         reason=f"Cluster '{cluster}' entered without required predecessor '{req}'",
                         evidence={"cluster": cluster, "requires": req,
                                   "clusters_so_far": list(task.clusters)})
            await self._act_topology(run, task, sig, top.on_missing_predecessor,
                                     "missing_predecessor",
                                     f"Missing predecessor: {req} before {cluster}")

        # 3. shadow utilization: unassigned agent participating in a governed task
        declared = settings.clusters_for(ev.swarm_id)
        if not cluster and declared and ev.parent_run_id:
            sig = Signal(detector="topology", severity=Severity.high,
                         reason=f"Shadow utilization: '{ev.agent_id}' participated in a task "
                                f"but is assigned to no cluster in swarm '{ev.swarm_id}'",
                         evidence={"agent": ev.agent_id, "swarm": ev.swarm_id,
                                   "parent_run": ev.parent_run_id})
            await self._act_topology(run, task, sig, top.on_shadow_utilization, "shadow",
                                     f"Shadow utilization: {ev.agent_id}")

        # 4. fan-out limits (runaway orchestrator)
        n_runs = len(task.runs)
        n_clusters = len(task.clusters) + (1 if cluster and cluster not in task.clusters else 0)
        if top.max_runs_per_task and n_runs > top.max_runs_per_task:
            sig = Signal(detector="topology", severity=Severity.high,
                         reason=f"Task fan-out: {n_runs} runs exceeds cap {top.max_runs_per_task}",
                         evidence={"runs": n_runs, "cap": top.max_runs_per_task})
            await self._act_topology(run, task, sig, top.on_fanout_breach, "fanout_runs",
                                     "Runaway fan-out (runs per task)")
        if top.max_clusters_per_task and n_clusters > top.max_clusters_per_task:
            sig = Signal(detector="topology", severity=Severity.high,
                         reason=f"Task touched {n_clusters} clusters, cap is {top.max_clusters_per_task}",
                         evidence={"clusters": n_clusters, "cap": top.max_clusters_per_task})
            await self._act_topology(run, task, sig, top.on_fanout_breach, "fanout_clusters",
                                     "Runaway fan-out (clusters per task)")

    async def _act_topology(self, run: RunStatus, task: TaskStatus, sig: Signal,
                            action: str, kind: str, title: str) -> None:
        if any(v["kind"] == kind and v["run_id"] == run.run_id for v in task.violations):
            return  # already recorded this violation on this run
        task.violations.append({
            "kind": kind, "title": title, "action": action, "run_id": run.run_id,
            "agent_id": run.agent_id, "cluster": run.cluster or "(unassigned)",
            "reason": sig.reason, "evidence": sig.evidence, "ts": time.time()})
        st_map = {"warn": ControlState.warned, "pause": ControlState.paused,
                  "kill": ControlState.killed, "escalate": ControlState.escalated}
        task.state = st_map.get(action, task.state)
        if action in ("pause", "escalate"):
            task.frozen_run = run.run_id
        self.store.audit(run.run_id, "guardian", f"topology_{kind}",
                         f"{action}: {title}")
        # enforce on the child run through the existing incident/ladder path
        await self._open_incident(run, [sig], action, title=title)

    async def _act_task_budget(self, ev: AgentEvent, run: RunStatus,
                               task: TaskStatus, top: TopologyPolicy) -> None:
        action = top.on_task_budget
        sig = Signal(detector="topology", severity=Severity.critical,
                     reason=f"Task budget breached: ${task.cost_usd:.2f} > "
                            f"${top.task_budget_usd:.2f} across {len(task.runs)} runs",
                     evidence={"task_cost": round(task.cost_usd, 4),
                               "cap": top.task_budget_usd, "runs": len(task.runs)})
        task.violations.append({
            "kind": "task_budget", "title": "Task budget breach", "action": action,
            "run_id": run.run_id, "agent_id": run.agent_id,
            "cluster": run.cluster or "(unassigned)", "reason": sig.reason,
            "evidence": sig.evidence, "ts": time.time()})
        st_map = {"pause": ControlState.paused, "kill": ControlState.killed,
                  "escalate": ControlState.escalated}
        task.state = st_map.get(action, task.state)
        if action in ("pause", "escalate"):
            task.frozen_run = run.run_id
        # act on ALL active runs in the task
        for rid in list(task.runs):
            r = self.runs.get(rid)
            if not r or r.state not in (ControlState.running, ControlState.warned):
                continue
            if rid == run.run_id:
                await self._open_incident(r, [sig], action, title="Task budget breach")
            else:
                self._force_state(r, action, "task budget breach (sibling run)")

    def _force_state(self, run: RunStatus, action: str, reason: str) -> None:
        st_map = {"pause": ControlState.paused, "kill": ControlState.killed,
                  "escalate": ControlState.escalated}
        ns = st_map.get(action)
        if ns and run.state in (ControlState.running, ControlState.warned):
            run.state = ns
            run.last_reason = reason
            self.store.audit(run.run_id, "guardian", f"task_action_{action}", reason)
            self.notify("run", run.model_dump())

    def _task_public(self, task: TaskStatus) -> dict:
        d = task.model_dump()
        d["run_count"] = len(task.runs)
        d["cluster_count"] = len(task.clusters)
        d["violation_count"] = len(task.violations)
        # a task under a governance hold keeps that state; otherwise it's
        # finished once every one of its runs has finished/been killed
        if task.state not in (ControlState.paused, ControlState.escalated,
                              ControlState.killed) and task.runs:
            done = all(self.runs[r].state in (ControlState.finished, ControlState.killed)
                       for r in task.runs if r in self.runs)
            d["state"] = (ControlState.finished.value if done
                          else ControlState.running.value)
        return d

    def tasks_summary(self) -> list[dict]:
        return sorted((self._task_public(t) for t in self.tasks.values()),
                      key=lambda t: -t["last_seen"])

    def task_detail(self, task_id: str) -> Optional[dict]:
        task = self.tasks.get(task_id)
        if task is None:
            return None
        nodes = []
        for rid in task.runs:
            r = self.runs.get(rid)
            if r is None:
                continue
            nodes.append({"run_id": rid, "agent_id": r.agent_id,
                          "cluster": r.cluster or "(unassigned)",
                          "state": r.state.value, "cost_usd": r.cost_usd,
                          "parent_run_id": r.parent_run_id, "goal": r.goal or ""})
        declared = settings.clusters_for(task.swarm_id)
        realized = set(task.clusters)
        d = self._task_public(task)
        d["nodes"] = nodes
        d["declared_clusters"] = declared
        d["untouched_clusters"] = [c for c in declared if c not in realized]
        return d

    @staticmethod
    def _norm_cluster(c: str) -> str:
        return c or "(unassigned)"

    def _cluster_keys(self) -> set:
        keys = set()
        for r in self.runs.values():
            keys.add((r.swarm_id, self._norm_cluster(r.cluster)))
        for a in self.store.list_agents():
            keys.add((a["swarm_id"], self._norm_cluster(a.get("cluster", ""))))
        return keys

    def _cluster_rollup(self, swarm_id: str, cluster: str) -> dict:
        """One cluster: its member agents (declared + active), fully-loaded cost,
        category split, and cluster-scoped waste (cost share − contribution share
        AMONG cluster members). Declared-but-idle agents are included."""
        members: dict[str, dict] = {}
        cost_by_cat = {c: 0.0 for c in CATEGORIES}
        tokens = steps = 0

        def m(aid: str) -> dict:
            return members.setdefault(aid, {
                "agent_id": aid, "cost_usd": 0.0, "runs": 0, "outputs_novel": 0,
                "outputs_total": 0, "state": "idle", "owner": "", "purpose": "",
                "budget_usd": 0.0})

        for r in self.runs.values():
            if r.swarm_id != swarm_id or self._norm_cluster(r.cluster) != cluster:
                continue
            x = m(r.agent_id)
            x["cost_usd"] = round(x["cost_usd"] + r.cost_usd, 6)
            x["runs"] += 1
            x["outputs_novel"] += r.outputs_novel
            x["outputs_total"] += r.outputs_total
            x["state"] = r.state.value
            for c, v in r.cost_by_cat.items():
                cost_by_cat[c] = round(cost_by_cat.get(c, 0.0) + v, 6)
            tokens += r.tokens
            steps += r.steps
        for a in self.store.list_agents():
            if a["swarm_id"] == swarm_id and self._norm_cluster(a.get("cluster", "")) == cluster:
                x = m(a["agent_id"])
                x["owner"], x["purpose"] = a.get("owner", ""), a.get("purpose", "")
                x["budget_usd"] = a.get("budget_usd", 0.0)

        mem = list(members.values())
        total = sum(x["cost_usd"] for x in mem) or 1e-9
        novel = sum(x["outputs_novel"] for x in mem) or 1e-9
        for x in mem:
            cs, ct = x["cost_usd"] / total, x["outputs_novel"] / novel
            x["cost_share"] = round(cs, 3)
            x["contribution_share"] = round(ct, 3)
            x["waste_score"] = round(max(0.0, cs - ct), 3)
        mem.sort(key=lambda x: -x["cost_usd"])
        return {
            "swarm_id": swarm_id, "cluster": cluster,
            "cost_usd": round(sum(x["cost_usd"] for x in mem), 6),
            "cost_by_cat": cost_by_cat, "tokens": tokens, "steps": steps,
            "runs": sum(x["runs"] for x in mem), "agent_count": len(mem),
            "outputs_novel": sum(x["outputs_novel"] for x in mem),
            "outputs_total": sum(x["outputs_total"] for x in mem),
            "members": mem, "agents": [x["agent_id"] for x in mem],
            "top_waster": (mem[0]["agent_id"] if mem and mem[0]["waste_score"] > 0.15
                           else None),
        }

    def clusters_summary(self) -> list[dict]:
        """Per (swarm, cluster) rollup — the sub-cluster economics roster."""
        out = [self._cluster_rollup(s, c) for (s, c) in self._cluster_keys()]
        return sorted(out, key=lambda x: -x["cost_usd"])

    def _cluster_graph(self, swarm_id: str, focus: str) -> dict:
        """Cluster-topology graph: observed cluster→cluster invocations across all
        tasks in the swarm, plus declared clusters as nodes. Denied edges in red."""
        edges: dict[tuple[str, str], dict] = {}
        nodes: set[str] = set(settings.clusters_for(swarm_id)) | {focus}
        for t in self.tasks.values():
            if t.swarm_id != swarm_id:
                continue
            for e in t.edges:
                fc, tc = e.get("from_cluster", ""), e.get("to_cluster", "")
                nodes.add(fc)
                nodes.add(tc)
                cur = edges.setdefault((fc, tc), {"count": 0, "denied": False})
                cur["count"] += 1
                cur["denied"] = cur["denied"] or bool(e.get("denied"))
        return {"focus": focus,
                "nodes": [{"id": n, "focus": n == focus} for n in sorted(nodes)],
                "edges": [{"from": a, "to": b, "count": v["count"], "denied": v["denied"]}
                          for (a, b), v in edges.items()]}

    def cluster_analysis(self, swarm_id: str, cluster: str) -> Optional[dict]:
        """Per-cluster analysis (the analog of agent_analysis): member roster,
        cost/category, cluster-scoped waste, governing policy, topology role, the
        cluster-interaction graph, participating tasks, incidents and a diagnosis."""
        roll = self._cluster_rollup(swarm_id, cluster)
        if not roll["members"] and cluster not in settings.clusters_for(swarm_id):
            return None
        member_ids = set(roll["agents"])

        pol = settings.policy_for("", swarm_id, cluster)
        top = settings.topology_for(swarm_id)
        topology = {
            "denied_edges": [{"from": f, "to": t} for (f, t) in top.denied_edges
                             if f == cluster or t == cluster],
            "requires": top.required_predecessors.get(cluster),
            "required_by": [c for c, req in top.required_predecessors.items()
                            if req == cluster],
        }

        # participating tasks + violations involving this cluster
        tasks, violations = [], []
        for t in self.tasks.values():
            if t.swarm_id != swarm_id or cluster not in t.clusters:
                continue
            tasks.append({"task_id": t.task_id, "state": self._task_public(t)["state"],
                          "cost_here": round(t.cost_by_cluster.get(cluster, 0.0), 6),
                          "cost_usd": t.cost_usd,
                          "violation_count": len(t.violations)})
            for v in t.violations:
                ev = v.get("evidence", {})
                if (v.get("cluster") == cluster or ev.get("from_cluster") == cluster
                        or ev.get("to_cluster") == cluster or ev.get("requires") == cluster):
                    violations.append({**v, "task_id": t.task_id})
        tasks.sort(key=lambda x: -x["cost_here"])

        incidents = [i for i in self.store.incidents(500) if i["agent_id"] in member_ids]
        graph = self._cluster_graph(swarm_id, cluster)

        # diagnosis
        if violations:
            kinds = sorted({v["kind"] for v in violations})
            diag = (f"Cluster '{cluster}' is implicated in {len(violations)} topology "
                    f"governance event(s): {', '.join(kinds)}. Review the denied edges / "
                    f"required predecessors below and the frozen tasks.")
        elif roll["top_waster"]:
            w = roll["members"][0]
            diag = (f"Top waster: '{w['agent_id']}' holds {int(w['cost_share']*100)}% of "
                    f"cluster spend but {int(w['contribution_share']*100)}% of its novel "
                    f"output. Candidate for consolidation or a tighter budget.")
        else:
            diag = (f"Healthy: {roll['agent_count']} agent(s), ${roll['cost_usd']:.4f} across "
                    f"{len(tasks)} task(s); no topology violations.")

        return {
            "swarm_id": swarm_id, "cluster": cluster,
            "summary": {k: roll[k] for k in ("cost_usd", "cost_by_cat", "tokens",
                                             "steps", "runs", "agent_count",
                                             "outputs_novel", "outputs_total")},
            "members": roll["members"], "top_waster": roll["top_waster"],
            "policy": {
                "max_cost_usd": pol.max_cost_usd, "max_tokens": pol.max_tokens,
                "max_steps": pol.max_steps, "allowed_tools": pol.allowed_tools,
                "denied_tools": pol.denied_tools, "pii_block": pol.pii_block,
                "budget_action": pol.budget_action, "policy_action": pol.policy_action,
            },
            "topology": topology, "graph": graph, "tasks": tasks,
            "task_count": len(tasks), "violations": violations,
            "incidents": incidents, "diagnosis": diag,
        }

    async def human_task_action(self, task_id: str, action: str,
                                who: str = "operator") -> Optional[dict]:
        """Human decision on a whole task (the topology-violation card)."""
        task = self.tasks.get(task_id)
        if task is None:
            return None
        if action == "resume":
            rid = task.frozen_run
            if rid and rid in self.runs:
                await self.human_action(rid, "resume", who)
            task.frozen_run = ""
            task.state = ControlState.running
        elif action in ("pause", "kill"):
            for rid in list(task.runs):
                r = self.runs.get(rid)
                if r and r.state in (ControlState.running, ControlState.warned,
                                     ControlState.paused, ControlState.escalated):
                    await self.human_action(rid, action, who)
            task.state = (ControlState.killed if action == "kill"
                          else ControlState.paused)
        self.store.audit(task_id, who, f"human_task_{action}")
        self.notify("task", self._task_public(task))
        return self._task_public(task)

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
        # resolve the ACTUAL 4-level policy (default->swarm->cluster->agent)
        _swarm = (runs[-1].swarm_id if runs else (profile or {}).get("swarm_id", "default"))
        pol = settings.policy_for(agent_id, _swarm, self._cluster_of(agent_id))
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
