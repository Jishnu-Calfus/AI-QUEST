"""Policy loading.

policies.yaml defines guardrails resolved across FOUR levels — most specific wins:

    default  ->  swarm  ->  cluster  ->  agent

Scalar limits (budgets, thresholds) use most-specific-wins. Restrictive
security fields are MONOTONIC — they can only get stricter going down the
levels, never looser: denied_tools / denied_patterns are unioned, pii_block and
fail_closed OR-together, and allowed_tools intersect (tighten-only). A cluster
must never be able to unblock what its swarm denied. That asymmetry is what makes
clusters real separation-of-duties boundaries instead of mere defaults.

Topology policies (denied cluster edges, required predecessors, fan-out caps,
task budgets) live at the swarm level and govern the SHAPE of a task, not a
single event — see engine.py.
"""
from __future__ import annotations

import os
import tempfile
from typing import Any

import yaml
from pydantic import BaseModel, Field

DEFAULT_POLICY: dict[str, Any] = {
    "max_cost_usd": 1.0,
    "max_tokens": 200_000,
    "max_steps": 60,
    "max_duration_s": 600,
    "max_calls_per_min": 120,
    "loop_window": 8,            # sliding window of recent actions
    "loop_similarity": 0.85,     # jaccard similarity to count as repeat
    "loop_repeats": 3,           # repeats within window => loop signal
    "stall_window": 6,           # steps with no new info => stall signal
    "denied_tools": [],
    "allowed_tools": None,       # None = all allowed
    "pii_block": True,
    "denied_patterns": [],       # extra regexes
    "judge_every_n_steps": 10,   # periodic drift check
    "warn_threshold": 3.0,       # suspicion score ladder
    "pause_threshold": 5.0,
    "kill_threshold": 8.0,
    "budget_action": "kill",     # action on hard budget breach
    "policy_action": "kill",     # action on policy violation
    "drift_action": "escalate",  # action on judged goal drift
    "fail_closed": False,
}

# fields that can only tighten going down the resolution chain
_MONOTONIC = {"denied_tools", "denied_patterns", "pii_block", "fail_closed",
              "allowed_tools"}
# keys in a swarm/cluster block that are structure, not scalar policy
_STRUCTURAL = {"clusters", "topology", "cluster", "swarm_id"}


class Policy(BaseModel):
    model_config = {"extra": "allow"}
    max_cost_usd: float = 1.0
    max_tokens: int = 200_000
    max_steps: int = 60
    max_duration_s: float = 600
    max_calls_per_min: int = 120
    loop_window: int = 8
    loop_similarity: float = 0.85
    loop_repeats: int = 3
    stall_window: int = 6
    denied_tools: list[str] = Field(default_factory=list)
    allowed_tools: list[str] | None = None
    pii_block: bool = True
    denied_patterns: list[str] = Field(default_factory=list)
    judge_every_n_steps: int = 10
    warn_threshold: float = 3.0
    pause_threshold: float = 5.0
    kill_threshold: float = 8.0
    budget_action: str = "kill"
    policy_action: str = "kill"
    drift_action: str = "escalate"
    fail_closed: bool = False


class TopologyPolicy(BaseModel):
    """Governs the SHAPE of a task across clusters (swarm-scoped)."""
    denied_edges: list[tuple[str, str]] = Field(default_factory=list)    # (from,to)
    required_predecessors: dict[str, str] = Field(default_factory=dict)  # cluster->required
    max_clusters_per_task: int = 0     # 0 = unlimited
    max_runs_per_task: int = 0         # 0 = unlimited
    task_budget_usd: float = 0.0       # 0 = unlimited
    shared_clusters: list[str] = Field(default_factory=list)  # exempt from shadow rule
    on_denied_edge: str = "escalate"
    on_missing_predecessor: str = "escalate"
    on_fanout_breach: str = "pause"
    on_task_budget: str = "pause"
    on_shadow_utilization: str = "escalate"

    def is_edge_denied(self, frm: str, to: str) -> bool:
        return any(frm == f and to == t for f, t in self.denied_edges)


class Settings:
    def __init__(self, path: str | None = None):
        self.path = path or os.environ.get("GUARDIAN_POLICIES", "policies.yaml")
        self.api_key = os.environ.get("GUARDIAN_API_KEY", "guardian-dev-key")
        self.slack_webhook = os.environ.get("SLACK_WEBHOOK_URL", "")
        # default DB lives in tmp: works on any mount (SQLite needs local file locking)
        self.db_path = os.environ.get(
            "GUARDIAN_DB", os.path.join(tempfile.gettempdir(), "guardian.db"))
        self.reload()

    # ---------- loading ----------

    def reload(self) -> None:
        raw: dict[str, Any] = {}
        if os.path.exists(self.path):
            with open(self.path) as f:
                raw = yaml.safe_load(f) or {}

        self.base: dict[str, Any] = {**DEFAULT_POLICY, **(raw.get("default") or {})}

        # per-agent overrides + declared cluster assignment
        self.agent_overrides: dict[str, dict] = {}
        self.agent_cluster: dict[str, str] = {}
        for agent_id, ov in (raw.get("agents") or {}).items():
            ov = ov or {}
            self.agent_overrides[agent_id] = ov
            if ov.get("cluster"):
                self.agent_cluster[agent_id] = ov["cluster"]

        # swarm scalar overrides, cluster scalar overrides, topology
        self.swarm_overrides: dict[str, dict] = {}
        self.cluster_overrides: dict[tuple[str, str], dict] = {}
        self.topology: dict[str, TopologyPolicy] = {}
        for swarm_id, sw in (raw.get("swarms") or {}).items():
            sw = sw or {}
            self.swarm_overrides[swarm_id] = {
                k: v for k, v in sw.items() if k not in _STRUCTURAL}
            for cluster, cl in (sw.get("clusters") or {}).items():
                self.cluster_overrides[(swarm_id, cluster)] = {
                    k: v for k, v in (cl or {}).items() if k not in _STRUCTURAL}
            if sw.get("topology"):
                self.topology[swarm_id] = self._parse_topology(sw["topology"])

        self._cache: dict[tuple[str, str, str], Policy] = {}

    @staticmethod
    def _parse_topology(t: dict) -> TopologyPolicy:
        edges = [(e["from"], e["to"]) for e in (t.get("denied_edges") or [])]
        reqs = {r["cluster"]: r["requires"]
                for r in (t.get("required_predecessors") or [])}
        return TopologyPolicy(
            denied_edges=edges, required_predecessors=reqs,
            max_clusters_per_task=t.get("max_clusters_per_task", 0),
            max_runs_per_task=t.get("max_runs_per_task", 0),
            task_budget_usd=t.get("task_budget_usd", 0.0),
            shared_clusters=t.get("shared_clusters", []),
            on_denied_edge=t.get("on_denied_edge", "escalate"),
            on_missing_predecessor=t.get("on_missing_predecessor", "escalate"),
            on_fanout_breach=t.get("on_fanout_breach", "pause"),
            on_task_budget=t.get("on_task_budget", "pause"),
            on_shadow_utilization=t.get("on_shadow_utilization", "escalate"),
        )

    # ---------- resolution ----------

    def policy_for(self, agent_id: str, swarm_id: str = "default",
                   cluster: str = "") -> Policy:
        key = (agent_id, swarm_id, cluster)
        hit = self._cache.get(key)
        if hit is not None:
            return hit

        out = dict(self.base)
        denied_tools = set(self.base.get("denied_tools") or [])
        denied_pat = set(self.base.get("denied_patterns") or [])
        pii = bool(self.base.get("pii_block", True))
        fail_closed = bool(self.base.get("fail_closed", False))
        allowed_sets: list[set] = ([set(self.base["allowed_tools"])]
                                   if self.base.get("allowed_tools") is not None else [])

        layers: list[dict] = []
        if swarm_id in self.swarm_overrides:
            layers.append(self.swarm_overrides[swarm_id])
        if cluster and (swarm_id, cluster) in self.cluster_overrides:
            layers.append(self.cluster_overrides[(swarm_id, cluster)])
        if agent_id in self.agent_overrides:
            layers.append(self.agent_overrides[agent_id])

        for layer in layers:
            for k, v in layer.items():
                if k in _STRUCTURAL:
                    continue
                if k == "denied_tools":
                    denied_tools |= set(v or [])
                elif k == "denied_patterns":
                    denied_pat |= set(v or [])
                elif k == "pii_block":
                    pii = pii or bool(v)
                elif k == "fail_closed":
                    fail_closed = fail_closed or bool(v)
                elif k == "allowed_tools":
                    if v is not None:
                        allowed_sets.append(set(v))
                else:                      # scalar: most-specific-wins
                    out[k] = v

        out["denied_tools"] = sorted(denied_tools)
        out["denied_patterns"] = sorted(denied_pat)
        out["pii_block"] = pii
        out["fail_closed"] = fail_closed
        out["allowed_tools"] = (sorted(set.intersection(*allowed_sets))
                                if allowed_sets else None)
        pol = Policy(**out)
        self._cache[key] = pol
        return pol

    def topology_for(self, swarm_id: str) -> TopologyPolicy:
        return self.topology.get(swarm_id, TopologyPolicy())

    def declared_cluster(self, agent_id: str) -> str:
        """YAML-declared cluster (registry assignment overrides this in the engine)."""
        return self.agent_cluster.get(agent_id, "")

    def clusters_for(self, swarm_id: str) -> list[str]:
        """Declared cluster names for a swarm (used to spot untouched/shadow)."""
        return sorted({c for (s, c) in self.cluster_overrides if s == swarm_id})


settings = Settings()
