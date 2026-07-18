"""Policy loading. policies.yaml defines per-agent guardrails; 'default' applies to unknown agents."""
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


class Settings:
    def __init__(self, path: str | None = None):
        self.path = path or os.environ.get("GUARDIAN_POLICIES", "policies.yaml")
        self.api_key = os.environ.get("GUARDIAN_API_KEY", "guardian-dev-key")
        self.slack_webhook = os.environ.get("SLACK_WEBHOOK_URL", "")
        # default DB lives in tmp: works on any mount (SQLite needs local file locking)
        self.db_path = os.environ.get(
            "GUARDIAN_DB", os.path.join(tempfile.gettempdir(), "guardian.db"))
        self.policies: dict[str, Policy] = {}
        self.reload()

    def reload(self) -> None:
        raw: dict[str, Any] = {}
        if os.path.exists(self.path):
            with open(self.path) as f:
                raw = yaml.safe_load(f) or {}
        agents = raw.get("agents", {})
        base = {**DEFAULT_POLICY, **raw.get("default", {})}
        self.policies = {"default": Policy(**base)}
        for agent_id, overrides in agents.items():
            self.policies[agent_id] = Policy(**{**base, **(overrides or {})})

    def policy_for(self, agent_id: str) -> Policy:
        return self.policies.get(agent_id, self.policies["default"])


settings = Settings()
