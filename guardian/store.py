"""SQLite audit store. Every event, signal, verdict and action is persisted.
Restart-safe; swap for Postgres later without changing callers."""
from __future__ import annotations

import json
import sqlite3
import threading
import time

from .models import AgentEvent, AgentProfile, BillingRow, Incident

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  event_id TEXT PRIMARY KEY, run_id TEXT, agent_id TEXT, type TEXT, name TEXT,
  content TEXT, tokens_in INT, tokens_out INT, cost_usd REAL, ts REAL, meta TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id, ts);
CREATE TABLE IF NOT EXISTS incidents (
  incident_id TEXT PRIMARY KEY, run_id TEXT, agent_id TEXT, action TEXT,
  severity INT, title TEXT, explanation TEXT, signals TEXT, ts REAL
);
CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, run_id TEXT, actor TEXT,
  action TEXT, detail TEXT
);
CREATE TABLE IF NOT EXISTS registry (
  agent_id TEXT PRIMARY KEY, swarm_id TEXT, owner TEXT, purpose TEXT,
  budget_usd REAL, registered_ts REAL
);
CREATE TABLE IF NOT EXISTS billing (
  id INTEGER PRIMARY KEY AUTOINCREMENT, swarm_id TEXT, category TEXT,
  cost_usd REAL, source TEXT, period TEXT, imported_ts REAL
);
"""


class Store:
    def __init__(self, path: str = "guardian.db"):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        self._lock = threading.Lock()

    def save_event(self, ev: AgentEvent) -> bool:
        """Returns False on duplicate event_id (idempotent ingest)."""
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (ev.event_id, ev.run_id, ev.agent_id, ev.type.value, ev.name,
                     ev.content, ev.tokens_in, ev.tokens_out, ev.cost_usd, ev.ts,
                     json.dumps(ev.meta)),
                )
                self._conn.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    def save_incident(self, inc: Incident) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO incidents VALUES (?,?,?,?,?,?,?,?,?)",
                (inc.incident_id, inc.run_id, inc.agent_id, inc.action,
                 int(inc.severity), inc.title, inc.explanation,
                 json.dumps([s.model_dump() for s in inc.signals]), inc.ts),
            )
            self._conn.commit()

    def audit(self, run_id: str, actor: str, action: str, detail: str = "") -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO audit (ts, run_id, actor, action, detail) VALUES (?,?,?,?,?)",
                (time.time(), run_id, actor, action, detail),
            )
            self._conn.commit()

    def recent_events(self, run_id: str, limit: int = 20) -> list[AgentEvent]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT event_id, run_id, agent_id, type, name, content, tokens_in,"
                " tokens_out, cost_usd, ts, meta FROM events WHERE run_id=?"
                " ORDER BY ts DESC LIMIT ?", (run_id, limit),
            ).fetchall()
        rows.reverse()
        return [AgentEvent(event_id=r[0], run_id=r[1], agent_id=r[2], type=r[3],
                           name=r[4], content=r[5], tokens_in=r[6], tokens_out=r[7],
                           cost_usd=r[8], ts=r[9], meta=json.loads(r[10] or "{}"))
                for r in rows]

    def events_by_agent(self, agent_id: str, limit: int = 5000) -> list[AgentEvent]:
        """All events for an agent across every run, oldest first — the raw
        material for agent-level tracing and debugging."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT event_id, run_id, agent_id, type, name, content, tokens_in,"
                " tokens_out, cost_usd, ts, meta FROM events WHERE agent_id=?"
                " ORDER BY ts ASC LIMIT ?", (agent_id, limit),
            ).fetchall()
        return [AgentEvent(event_id=r[0], run_id=r[1], agent_id=r[2], type=r[3],
                           name=r[4], content=r[5], tokens_in=r[6], tokens_out=r[7],
                           cost_usd=r[8], ts=r[9], meta=json.loads(r[10] or "{}"))
                for r in rows]

    def incidents(self, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT incident_id, run_id, agent_id, action, severity, title,"
                " explanation, signals, ts FROM incidents ORDER BY ts DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(incident_id=r[0], run_id=r[1], agent_id=r[2], action=r[3],
                     severity=r[4], title=r[5], explanation=r[6],
                     signals=json.loads(r[7] or "[]"), ts=r[8]) for r in rows]

    # ---- registry ("hire" agents like employees) ----

    def register_agent(self, p: AgentProfile) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO registry VALUES (?,?,?,?,?,?)",
                (p.agent_id, p.swarm_id, p.owner, p.purpose, p.budget_usd,
                 p.registered_ts))
            self._conn.commit()

    def list_agents(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT agent_id, swarm_id, owner, purpose, budget_usd,"
                " registered_ts FROM registry ORDER BY swarm_id, agent_id").fetchall()
        return [dict(agent_id=r[0], swarm_id=r[1], owner=r[2], purpose=r[3],
                     budget_usd=r[4], registered_ts=r[5]) for r in rows]

    def get_agent(self, agent_id: str) -> dict | None:
        with self._lock:
            r = self._conn.execute(
                "SELECT agent_id, swarm_id, owner, purpose, budget_usd,"
                " registered_ts FROM registry WHERE agent_id=?", (agent_id,)).fetchone()
        if not r:
            return None
        return dict(agent_id=r[0], swarm_id=r[1], owner=r[2], purpose=r[3],
                    budget_usd=r[4], registered_ts=r[5])

    # ---- billing true-up (lane 2) ----

    def import_billing(self, rows: list[BillingRow]) -> int:
        with self._lock:
            for b in rows:
                self._conn.execute(
                    "INSERT INTO billing (swarm_id, category, cost_usd, source,"
                    " period, imported_ts) VALUES (?,?,?,?,?,?)",
                    (b.swarm_id, b.category, b.cost_usd, b.source, b.period,
                     time.time()))
            self._conn.commit()
        return len(rows)

    def billing_by_swarm(self) -> dict[str, dict[str, float]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT swarm_id, category, SUM(cost_usd) FROM billing"
                " GROUP BY swarm_id, category").fetchall()
        out: dict[str, dict[str, float]] = {}
        for swarm, cat, total in rows:
            out.setdefault(swarm, {})[cat] = round(total, 6)
        return out

    def audit_log(self, limit: int = 100) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts, run_id, actor, action, detail FROM audit"
                " ORDER BY ts DESC LIMIT ?", (limit,),
            ).fetchall()
        return [dict(ts=r[0], run_id=r[1], actor=r[2], action=r[3], detail=r[4])
                for r in rows]
