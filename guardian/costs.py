"""Full-stack cost pricing & categorization.

Two-lane model:
  Lane 1 (live, metered): every event gets priced in real time — either the agent
  reports cost_usd directly (exact, e.g. LLM invoices) or Guardian prices it from
  the unit-cost catalog (estimate, e.g. db queries, compute seconds).
  Lane 2 (true-up): actual cloud billing (AWS CUR export) is imported and
  reconciled against the metered estimates per swarm/category.
"""
from __future__ import annotations

import os

import yaml

from .models import AgentEvent, EventType

CATEGORIES = ["llm", "api", "db", "compute", "storage", "other"]

DEFAULT_CATALOG: dict[str, float] = {
    # resource kind -> $ per unit  (edit cost_catalog.yaml to match your infra)
    "db_query": 0.0004,        # per query (derived from RDS bill / monthly queries)
    "db_connection_hour": 0.02,
    "compute_second": 0.00011, # $0.40/hr container share
    "gpu_second": 0.0014,
    "storage_gb": 0.023,       # per GB-month
    "network_gb": 0.09,
    "web_search": 0.005,       # per call (external API)
    "embedding_1k": 0.0001,
    "vector_query": 0.0002,
    "ocr_page": 0.0015,
}

_CATEGORY_BY_KIND = {
    "db_query": "db", "db_connection_hour": "db", "vector_query": "db",
    "compute_second": "compute", "gpu_second": "compute",
    "storage_gb": "storage", "network_gb": "storage",
    "web_search": "api", "embedding_1k": "api", "ocr_page": "api",
}


class CostCatalog:
    def __init__(self, path: str | None = None):
        self.path = path or os.environ.get("GUARDIAN_COST_CATALOG", "cost_catalog.yaml")
        self.unit_costs = dict(DEFAULT_CATALOG)
        self.reload()

    def reload(self) -> None:
        if os.path.exists(self.path):
            with open(self.path) as f:
                raw = yaml.safe_load(f) or {}
            self.unit_costs.update(raw.get("unit_costs", {}))

    def price(self, ev: AgentEvent) -> float:
        """Return the event's cost; use catalog when the agent didn't report one."""
        if ev.cost_usd and ev.cost_usd > 0:
            return ev.cost_usd
        unit = self.unit_costs.get(ev.name or "")
        if unit is not None:
            return unit * max(ev.qty, 0.0)
        return 0.0

    @staticmethod
    def categorize(ev: AgentEvent) -> str:
        if ev.type == EventType.llm_call:
            return "llm"
        if ev.type == EventType.resource:
            return _CATEGORY_BY_KIND.get(ev.name or "", "other")
        if ev.type == EventType.tool_call:
            return _CATEGORY_BY_KIND.get(ev.name or "", "api")
        return "other"


catalog = CostCatalog()
