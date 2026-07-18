"""Guardian SDK — the entire integration is ≤5 lines:

    from guardian_sdk import Guardian
    g = Guardian(agent_id="invoice-bot", goal="Summarize Q2 invoices")
    g.event("tool_call", name="search", content="q2 invoices", cost_usd=0.002)
    ...
    g.end()

Semantics:
  * fail-open by default: if Guardian is unreachable, your agent keeps running.
  * cooperative control: every .event() returns the control state; when Guardian
    pauses the run, .event() BLOCKS (polling) until a human resumes or kills;
    when killed, it raises GuardianKilled — catch it or let the run die cleanly.
  * .checkpoint() = explicit control check without reporting an event.
Works with any framework: call .event() wherever your agent does something.
"""
from __future__ import annotations

import os
import time
import uuid

import httpx


class GuardianKilled(RuntimeError):
    """Raised when Guardian (or a human) killed this run."""


class Guardian:
    def __init__(self, agent_id: str = "default", goal: str = "",
                 swarm_id: str = "default",
                 task_id: str | None = None, parent_run_id: str | None = None,
                 base_url: str | None = None, api_key: str | None = None,
                 run_id: str | None = None, fail_open: bool = True,
                 poll_interval: float = 1.5, timeout: float = 3.0):
        self.base = (base_url or os.environ.get("GUARDIAN_URL", "http://localhost:8090")).rstrip("/")
        self.key = api_key or os.environ.get("GUARDIAN_API_KEY", "guardian-dev-key")
        self.agent_id = agent_id
        self.swarm_id = swarm_id
        self.goal = goal
        self.run_id = run_id or uuid.uuid4().hex[:12]
        # realized-layer trace context. task_id identifies the whole end-to-end
        # workflow; it is minted once at the entry point and propagated unchanged
        # to every descendant. parent_run_id links this run to its invoker.
        self.task_id = task_id or os.environ.get("GUARDIAN_TASK_ID") or self.run_id
        self.parent_run_id = parent_run_id or os.environ.get("GUARDIAN_PARENT_RUN") or ""
        self.fail_open = fail_open
        self.poll = poll_interval
        # trust_env=False: ignore system proxy vars — Guardian is a local/known
        # control plane; env proxies (SOCKS etc.) must never break enforcement.
        self._c = httpx.Client(timeout=timeout, trust_env=False)
        self._started = False

    # ---------------- context propagation ----------------

    def context(self) -> dict:
        """Handoff token to pass to any agent this one invokes. The callee builds
        its Guardian from it — that single hop is the ENTIRE author effort; the
        realized cross-agent graph is then derived by Guardian. Ride it on
        whatever transport you already use (HTTP headers, kwargs, message meta)."""
        return {"task_id": self.task_id, "parent_run_id": self.run_id}

    def child(self, agent_id: str, goal: str = "", **kw) -> "Guardian":
        """Convenience: construct a child client already wired to this run/task."""
        return Guardian(agent_id=agent_id, goal=goal, swarm_id=self.swarm_id,
                        base_url=self.base, api_key=self.key, fail_open=self.fail_open,
                        poll_interval=self.poll, **self.context(), **kw)

    # ---------------- core ----------------

    def event(self, type: str = "tool_call", name: str = "", content: str = "",
              tokens_in: int = 0, tokens_out: int = 0, cost_usd: float = 0.0,
              qty: float = 1.0, **meta) -> str:
        """Report one step. Returns control state; blocks on pause; raises on kill."""
        payload = {
            "run_id": self.run_id, "agent_id": self.agent_id,
            "swarm_id": self.swarm_id, "task_id": self.task_id,
            "parent_run_id": self.parent_run_id, "type": type,
            "name": name, "content": content, "tokens_in": tokens_in,
            "tokens_out": tokens_out, "cost_usd": cost_usd, "qty": qty,
            "meta": meta, "ts": time.time(),
        }
        if not self._started:
            payload["goal"] = self.goal
            self._started = True
        state = self._post_event(payload)
        return self._enforce(state)

    def checkpoint(self) -> str:
        """Control check without reporting an event (use inside long inner loops)."""
        return self._enforce(self._control())

    def llm(self, model: str, prompt: str, tokens_in: int, tokens_out: int,
            cost_usd: float) -> str:
        return self.event("llm_call", name=model, content=prompt[:300],
                          tokens_in=tokens_in, tokens_out=tokens_out, cost_usd=cost_usd)

    def tool(self, name: str, args: str = "", cost_usd: float = 0.0) -> str:
        return self.event("tool_call", name=name, content=args, cost_usd=cost_usd)

    def output(self, text: str) -> str:
        return self.event("output", content=text)

    def resource(self, kind: str, qty: float = 1.0, note: str = "") -> str:
        """Report infra usage: db_query, compute_second, storage_gb, gpu_second...
        Guardian prices it from the cost catalog — full-stack cost, not just tokens."""
        return self.event("resource", name=kind, qty=qty, content=note)

    def end(self) -> None:
        try:
            self._post_event({"run_id": self.run_id, "agent_id": self.agent_id,
                              "swarm_id": self.swarm_id, "task_id": self.task_id,
                              "parent_run_id": self.parent_run_id,
                              "type": "run_end", "ts": time.time()})
        except Exception:
            pass

    # -------------- internals --------------

    def _post_event(self, payload: dict) -> str:
        try:
            r = self._c.post(f"{self.base}/v1/events", json=payload,
                             headers={"X-Guardian-Key": self.key})
            r.raise_for_status()
            return r.json().get("state", "running")
        except GuardianKilled:
            raise
        except Exception:
            if self.fail_open:
                return "running"          # guardian down -> agent unaffected
            raise

    def _control(self) -> str:
        try:
            r = self._c.get(f"{self.base}/v1/runs/{self.run_id}/control")
            return r.json().get("state", "running")
        except Exception:
            return "running" if self.fail_open else "paused"

    def _enforce(self, state: str) -> str:
        if state == "killed":
            raise GuardianKilled(f"run {self.run_id} killed by Guardian")
        while state in ("paused", "escalated"):
            time.sleep(self.poll)          # blocked at checkpoint until human decides
            state = self._control()
            if state == "killed":
                raise GuardianKilled(f"run {self.run_id} killed by Guardian")
        return state

    # -------------- context manager --------------

    def __enter__(self) -> "Guardian":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is None:
            self.end()
        self._c.close()
