"""Pydantic schemas for Guardian."""
from __future__ import annotations

import time
import uuid
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


class EventType(str, Enum):
    run_start = "run_start"
    llm_call = "llm_call"
    tool_call = "tool_call"
    resource = "resource"       # infra usage: db_query, compute_second, storage_gb...
    output = "output"
    error = "error"
    run_end = "run_end"


class Severity(int, Enum):
    ok = 0
    info = 1
    warn = 2
    high = 3
    critical = 4


class ControlState(str, Enum):
    running = "running"
    warned = "warned"
    paused = "paused"
    killed = "killed"
    escalated = "escalated"  # paused, pending human decision
    finished = "finished"


class AgentEvent(BaseModel):
    """One step of a watched agent. This is the entire integration surface."""
    event_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    run_id: str
    agent_id: str = "default"
    swarm_id: str = "default"
    # --- realized-layer trace context (sub-cluster governance) ---
    task_id: Optional[str] = None       # end-to-end workflow instance; propagated
    parent_run_id: Optional[str] = None # the run that invoked this run (edge)
    goal: Optional[str] = None          # send once on run_start
    type: EventType = EventType.tool_call
    name: Optional[str] = None          # tool name / model name / resource kind
    content: Optional[str] = None       # args, prompt summary, output text
    qty: float = 1.0                    # units for resource events (queries, seconds, GB)
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0               # if 0, Guardian prices it from the cost catalog
    ts: float = Field(default_factory=time.time)
    meta: dict[str, Any] = Field(default_factory=dict)


class Signal(BaseModel):
    """A detector's finding for one event."""
    detector: str
    severity: Severity
    reason: str
    evidence: dict[str, Any] = Field(default_factory=dict)


class Verdict(BaseModel):
    """Judge output (L2)."""
    drift_score: float = 0.0            # 0 = on goal, 1 = fully off-goal
    verdict: str = "ok"                 # ok | drifting | off_goal | unsafe
    reasoning: str = ""
    recommended_action: str = "observe" # observe|warn|pause|kill|escalate
    provider: str = "mock"


class Incident(BaseModel):
    incident_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    run_id: str
    agent_id: str
    action: str                          # warn|pause|kill|escalate
    severity: Severity
    title: str
    explanation: str
    signals: list[Signal] = Field(default_factory=list)
    ts: float = Field(default_factory=time.time)


class ControlResponse(BaseModel):
    run_id: str
    state: ControlState
    reason: str = ""
    incident_id: Optional[str] = None


class RunStatus(BaseModel):
    run_id: str
    agent_id: str
    swarm_id: str = "default"
    cluster: str = ""                   # declared cluster this agent belongs to
    task_id: str = ""                   # realized-layer workflow this run is part of
    parent_run_id: str = ""             # who invoked this run
    goal: Optional[str] = None
    state: ControlState = ControlState.running
    steps: int = 0
    tokens: int = 0
    cost_usd: float = 0.0
    cost_by_cat: dict[str, float] = Field(default_factory=dict)  # llm/api/db/compute/storage/other
    outputs_total: int = 0
    outputs_novel: int = 0
    started: float = Field(default_factory=time.time)
    last_seen: float = Field(default_factory=time.time)
    suspicion: float = 0.0              # decaying accumulated signal score
    last_reason: str = ""


class AgentProfile(BaseModel):
    """Registry entry — 'hire' an agent like an employee."""
    agent_id: str
    swarm_id: str = "default"
    cluster: str = ""                   # declared sub-cluster (ingestion/extraction/...)
    owner: str = ""
    purpose: str = ""
    budget_usd: float = 0.0             # informational; enforcement lives in policies.yaml
    registered_ts: float = Field(default_factory=time.time)


class TaskStatus(BaseModel):
    """Realized-layer rollup: one end-to-end workflow instance stitched from
    the event stream — which runs/clusters participated, in what shape, at what
    cost, and any topology governance events."""
    task_id: str
    swarm_id: str = "default"
    goal: str = ""
    state: ControlState = ControlState.running
    runs: list[str] = Field(default_factory=list)              # run_ids (spans)
    edges: list[dict] = Field(default_factory=list)            # {from,to,from_cluster,to_cluster,denied}
    clusters: list[str] = Field(default_factory=list)          # realized cluster set, entry order
    cost_usd: float = 0.0
    cost_by_cluster: dict[str, float] = Field(default_factory=dict)
    violations: list[dict] = Field(default_factory=list)       # topology governance events
    frozen_run: str = ""                                       # run held for a human decision
    started: float = Field(default_factory=time.time)
    last_seen: float = Field(default_factory=time.time)


class BillingRow(BaseModel):
    """One imported actual-cost line (e.g. from an AWS CUR export)."""
    swarm_id: str
    category: str                        # llm|api|db|compute|storage|other
    cost_usd: float
    source: str = "aws_cur"
    period: str = ""
