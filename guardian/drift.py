"""Agent drift & model drift detection — ADD-ON module (touches no existing code).

Guardian already persists every agent step (type, model/tool name, tokens, cost,
output text, timestamp) plus the L2 judge's goal-drift verdicts. This module reads
that same event stream (read-only) and asks a different question than the live
detectors do:

  Is an agent — or the model behind it — behaving DIFFERENTLY THAN IT USED TO?

That is drift, and it is distributional, not per-step. We split each entity's
history into an earlier BASELINE window and a RECENT window and measure the shift:

  * categorical features (action mix, tool/model mix)  -> Jensen–Shannon divergence
  * numeric features (tokens, cost, output length,     -> standardized mean shift
    output novelty, judged goal-drift)                    blended with % change

AGENT DRIFT  = behavioural change of one agent over time (action mix shifts, cost
               per step balloons, output novelty collapses, goal-drift rises).
MODEL DRIFT  = the LLM behind the calls changing character (tokens-out per call,
               cost per call, output length, out/in ratio drifting) — the classic
               "the provider silently changed the model under us" signal.

Wire it onto the existing app WITHOUT editing anything:  see guardian/app_drift.py
"""
from __future__ import annotations

import math
import os
import sqlite3
from collections import Counter
from statistics import mean, pstdev

from fastapi.responses import FileResponse

from .config import settings
from .detectors import _jaccard, _norm_tokens

MIN_EVENTS = 6          # need enough history to talk about "before vs after"
MIN_WINDOW = 3
_LLM = "llm_call"

# feature weights inside an entity's blended drift score
_AGENT_DIMS = {"action_mix": 1.0, "tool_mix": 0.9, "cost_per_step": 1.0,
               "tokens_per_step": 0.9, "output_novelty": 1.0, "goal_drift": 1.1}
_MODEL_DIMS = {"tokens_out": 1.1, "tokens_in": 0.8, "cost_per_call": 1.0,
               "out_in_ratio": 0.9, "output_len": 0.9}


# ----------------------------- math helpers -----------------------------

def _js_divergence(p: dict, q: dict) -> float:
    """Jensen–Shannon divergence (log2, so 0..1) between two count/prob dists."""
    keys = set(p) | set(q)
    if not keys:
        return 0.0

    def norm(d):
        s = sum(d.get(k, 0) for k in keys) or 1.0
        return {k: d.get(k, 0) / s for k in keys}

    P, Q = norm(p), norm(q)
    M = {k: (P[k] + Q[k]) / 2 for k in keys}

    def kl(a, b):
        return sum(a[k] * math.log2(a[k] / b[k])
                   for k in keys if a[k] > 0 and b[k] > 0)

    return max(0.0, min(1.0, 0.5 * kl(P, M) + 0.5 * kl(Q, M)))


def _num_drift(base: list[float], recent: list[float], bounded: bool = False) -> float:
    """Standardized mean shift blended with relative change -> 0..1.

    bounded=True for features already on a 0..1 scale (novelty, judged goal-drift):
    use the absolute mean difference so a small move off a zero baseline stays
    small — relative change is meaningless there and would explode."""
    if len(base) < 2 or len(recent) < 1:
        return 0.0
    mb, mr = mean(base), mean(recent)
    diff = abs(mr - mb)
    if bounded:
        return min(1.0, diff)                  # already 0..1; absolute shift is the drift
    sb = pstdev(base)
    rel = diff / (abs(mb) + 1e-9)
    if sb < 1e-9:                              # constant baseline: fall back to relative
        return min(1.0, rel)
    z = diff / sb
    return min(1.0, 0.5 * min(1.0, z / 3.0) + 0.5 * min(1.0, rel))


def _status(score: float) -> tuple[str, str]:
    if score >= 0.7:
        return "high", "#f4587a"
    if score >= 0.4:
        return "moderate", "#f5b23e"
    if score >= 0.15:
        return "low", "#5aa7ff"
    return "stable", "#2dd4a7"


def _split(seq: list) -> tuple[list, list]:
    half = len(seq) // 2
    return seq[:half], seq[half:]


def _pct(base: float, recent: float):
    """Percent change, or None when the baseline is ~0 (undefined — show a delta)."""
    if abs(base) < 1e-9:
        return None
    return round((recent - base) / abs(base) * 100, 0)


# ----------------------------- analyzer -----------------------------

class DriftAnalyzer:
    """Reads the same SQLite the live server writes — strictly read-only."""

    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or settings.db_path

    def _read(self):
        events, judge = [], []
        if not os.path.exists(self.db_path):
            return events, judge
        try:
            con = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True,
                                  check_same_thread=False)
            try:
                events = con.execute(
                    "SELECT agent_id, run_id, type, name, content, tokens_in,"
                    " tokens_out, cost_usd, ts FROM events ORDER BY ts ASC").fetchall()
                judge = con.execute(
                    "SELECT run_id, detail, ts FROM audit WHERE actor LIKE 'judge:%'"
                    " ORDER BY ts ASC").fetchall()
            finally:
                con.close()
        except sqlite3.Error:
            pass
        return events, judge

    # ---- feature extraction ----

    @staticmethod
    def _novelty_flags(outputs: list[str]) -> list[float]:
        """1 if an output added new information vs prior outputs, else 0."""
        flags, seen = [], []
        for text in outputs:
            toks = _norm_tokens(text)
            sim = max((_jaccard(toks, s) for s in seen), default=0.0)
            flags.append(0.0 if sim >= 0.9 else 1.0)
            seen.append(toks)
        return flags

    def analyze(self) -> dict:
        events, judge = self._read()
        # goal-drift scores parsed from judge audit rows, keyed by run_id
        run_goaldrift: dict[str, list[tuple[float, float]]] = {}
        for run_id, detail, ts in judge:
            d = (detail or "")
            if "drift=" in d:
                try:
                    val = float(d.split("drift=")[1].split()[0])
                    run_goaldrift.setdefault(run_id, []).append((ts, val))
                except (ValueError, IndexError):
                    continue

        by_agent: dict[str, list] = {}
        by_model: dict[str, list] = {}
        run_agent: dict[str, str] = {}
        for row in events:
            agent_id, run_id, typ, name = row[0], row[1], row[2], row[3]
            run_agent[run_id] = agent_id
            by_agent.setdefault(agent_id, []).append(row)
            if typ == _LLM and name:
                by_model.setdefault(name, []).append(row)

        agents = [self._agent_drift(a, evs, run_goaldrift, run_agent)
                  for a, evs in by_agent.items()]
        models = [self._model_drift(m, evs) for m, evs in by_model.items()]
        agents = [a for a in agents if a]
        models = [m for m in models if m]
        agents.sort(key=lambda x: -x["score"])
        models.sort(key=lambda x: -x["score"])
        return {"agents": agents, "models": models,
                "summary": self._summary(agents, models)}

    # ---- per-agent ----

    def _agent_drift(self, agent_id, evs, run_goaldrift, run_agent):
        if len(evs) < MIN_EVENTS:
            return {"entity": agent_id, "kind": "agent", "events": len(evs),
                    "status": "insufficient", "color": "#8b94a7", "score": 0.0,
                    "top_signal": "not enough history", "features": []}
        base, recent = _split(evs)
        if len(base) < MIN_WINDOW or len(recent) < MIN_WINDOW:
            return {"entity": agent_id, "kind": "agent", "events": len(evs),
                    "status": "insufficient", "color": "#8b94a7", "score": 0.0,
                    "top_signal": "not enough history", "features": []}

        TYPE, NAME, CONTENT, TIN, TOUT, COST = 2, 3, 4, 5, 6, 7

        def type_dist(rows):
            return Counter(r[TYPE] for r in rows)

        def name_dist(rows):
            return Counter(r[NAME] or r[TYPE] for r in rows
                           if r[TYPE] in ("tool_call", _LLM))

        cost_b = [r[COST] or 0.0 for r in base]
        cost_r = [r[COST] or 0.0 for r in recent]
        tok_b = [(r[TIN] or 0) + (r[TOUT] or 0) for r in base]
        tok_r = [(r[TIN] or 0) + (r[TOUT] or 0) for r in recent]

        out_all = [r[CONTENT] or "" for r in evs if r[TYPE] == "output"]
        nov = self._novelty_flags(out_all)
        nb, nr = _split(nov)

        # judged goal drift for this agent's runs, in ts order
        gd = sorted((v for r in evs for v in run_goaldrift.get(r[1], [])),
                    key=lambda x: x[0])
        gd_vals = [v for _, v in gd]
        gb, gr = _split(gd_vals)

        dims = {
            "action_mix": _js_divergence(type_dist(base), type_dist(recent)),
            "tool_mix": _js_divergence(name_dist(base), name_dist(recent)),
            "cost_per_step": _num_drift(cost_b, cost_r),
            "tokens_per_step": _num_drift(tok_b, tok_r),
            "output_novelty": _num_drift(nb, nr, bounded=True) if len(nb) >= 2 else 0.0,
            "goal_drift": _num_drift(gb, gr, bounded=True) if len(gb) >= 2 else 0.0,
        }
        score = self._blend(dims, _AGENT_DIMS)
        status, color = _status(score)
        feats = [
            self._num_feat("action mix", "JS", None, None, dims["action_mix"], categorical=True,
                           base_dist=type_dist(base), recent_dist=type_dist(recent)),
            self._num_feat("tool/model mix", "JS", None, None, dims["tool_mix"], categorical=True,
                           base_dist=name_dist(base), recent_dist=name_dist(recent)),
            self._num_feat("cost / step", "$", mean(cost_b), mean(cost_r), dims["cost_per_step"]),
            self._num_feat("tokens / step", "tk", mean(tok_b), mean(tok_r), dims["tokens_per_step"]),
            self._num_feat("output novelty", "rate", mean(nb) if nb else 0,
                           mean(nr) if nr else 0, dims["output_novelty"]),
        ]
        if gd_vals:
            feats.append(self._num_feat("goal drift (judge)", "score",
                                        mean(gb) if gb else 0, mean(gr) if gr else 0,
                                        dims["goal_drift"]))
        top = max(dims, key=dims.get)
        return {"entity": agent_id, "kind": "agent", "events": len(evs),
                "base_n": len(base), "recent_n": len(recent),
                "score": round(score, 3), "status": status, "color": color,
                "dims": {k: round(v, 3) for k, v in dims.items()},
                "top_signal": self._signal(top, feats), "features": feats}

    # ---- per-model ----

    def _model_drift(self, model, evs):
        if len(evs) < MIN_EVENTS:
            return {"entity": model, "kind": "model", "events": len(evs),
                    "status": "insufficient", "color": "#8b94a7", "score": 0.0,
                    "top_signal": "not enough calls", "features": []}
        base, recent = _split(evs)
        if len(base) < MIN_WINDOW or len(recent) < MIN_WINDOW:
            return {"entity": model, "kind": "model", "events": len(evs),
                    "status": "insufficient", "color": "#8b94a7", "score": 0.0,
                    "top_signal": "not enough calls", "features": []}
        CONTENT, TIN, TOUT, COST = 4, 5, 6, 7

        def col(rows, i):
            return [rows[k][i] or 0 for k in range(len(rows))]

        tin_b, tin_r = col(base, TIN), col(recent, TIN)
        tout_b, tout_r = col(base, TOUT), col(recent, TOUT)
        cost_b, cost_r = col(base, COST), col(recent, COST)
        ratio_b = [o / (i + 1e-9) for o, i in zip(tout_b, tin_b)]
        ratio_r = [o / (i + 1e-9) for o, i in zip(tout_r, tin_r)]
        len_b = [len(r[CONTENT] or "") for r in base]
        len_r = [len(r[CONTENT] or "") for r in recent]

        dims = {
            "tokens_out": _num_drift(tout_b, tout_r),
            "tokens_in": _num_drift(tin_b, tin_r),
            "cost_per_call": _num_drift(cost_b, cost_r),
            "out_in_ratio": _num_drift(ratio_b, ratio_r),
            "output_len": _num_drift(len_b, len_r),
        }
        score = self._blend(dims, _MODEL_DIMS)
        status, color = _status(score)
        feats = [
            self._num_feat("tokens out / call", "tk", mean(tout_b), mean(tout_r), dims["tokens_out"]),
            self._num_feat("tokens in / call", "tk", mean(tin_b), mean(tin_r), dims["tokens_in"]),
            self._num_feat("cost / call", "$", mean(cost_b), mean(cost_r), dims["cost_per_call"]),
            self._num_feat("out/in ratio", "x", mean(ratio_b), mean(ratio_r), dims["out_in_ratio"]),
            self._num_feat("output length", "ch", mean(len_b), mean(len_r), dims["output_len"]),
        ]
        top = max(dims, key=dims.get)
        return {"entity": model, "kind": "model", "events": len(evs),
                "base_n": len(base), "recent_n": len(recent),
                "score": round(score, 3), "status": status, "color": color,
                "dims": {k: round(v, 3) for k, v in dims.items()},
                "top_signal": self._signal(top, feats), "features": feats}

    # ---- shaping helpers ----

    @staticmethod
    def _blend(dims: dict, weights: dict) -> float:
        vals = [dims[k] * weights.get(k, 1.0) for k in dims]
        wmax = max((dims[k] for k in dims), default=0.0)
        wmean = sum(vals) / (sum(weights.get(k, 1.0) for k in dims) or 1.0)
        return max(0.0, min(1.0, 0.6 * wmax + 0.4 * wmean))

    @staticmethod
    def _num_feat(label, unit, base, recent, drift, categorical=False,
                  base_dist=None, recent_dist=None):
        f = {"feature": label, "unit": unit, "drift": round(drift, 3),
             "categorical": categorical}
        if categorical:
            tot_b = sum(base_dist.values()) or 1
            tot_r = sum(recent_dist.values()) or 1
            keys = sorted(set(base_dist) | set(recent_dist))
            f["base_dist"] = {k: round(base_dist.get(k, 0) / tot_b, 2) for k in keys}
            f["recent_dist"] = {k: round(recent_dist.get(k, 0) / tot_r, 2) for k in keys}
        else:
            f["base"] = round(base, 4)
            f["recent"] = round(recent, 4)
            f["pct"] = _pct(base, recent)
            f["delta"] = round(recent - base, 4)
        return f

    @staticmethod
    def _signal(top_dim: str, feats: list) -> str:
        label = {"action_mix": "action mix", "tool_mix": "tool/model mix",
                 "cost_per_step": "cost / step", "tokens_per_step": "tokens / step",
                 "output_novelty": "output novelty", "goal_drift": "goal drift (judge)",
                 "tokens_out": "tokens out / call", "tokens_in": "tokens in / call",
                 "cost_per_call": "cost / call", "out_in_ratio": "out/in ratio",
                 "output_len": "output length"}.get(top_dim, top_dim)
        for f in feats:
            if f["feature"] == label:
                if f.get("categorical"):
                    return f"{label} distribution shifted"
                if f.get("pct") is None:            # zero baseline: show absolute delta
                    return f"{label} {f['base']}→{f['recent']} (Δ{f['delta']:+})"
                sign = "+" if f["pct"] >= 0 else ""
                return f"{label} {f['base']}→{f['recent']} ({sign}{f['pct']:.0f}%)"
        return label

    @staticmethod
    def _summary(agents, models):
        def drifting(rows):
            return sum(1 for r in rows if r["score"] >= 0.4)
        allrows = agents + models
        return {
            "agents_monitored": len(agents), "models_monitored": len(models),
            "agents_drifting": drifting(agents), "models_drifting": drifting(models),
            "max_drift": round(max((r["score"] for r in allrows), default=0.0), 3),
            "top": sorted(allrows, key=lambda r: -r["score"])[:1][0]["entity"]
                   if allrows else None,
        }


# ----------------------------- app wiring (no existing file touched) -----------------------------

def register(app) -> None:
    """Attach drift routes to an EXISTING FastAPI app. Called from app_drift.py."""
    analyzer = DriftAnalyzer()

    @app.get("/v1/drift")
    async def drift_all():
        return analyzer.analyze()

    @app.get("/v1/drift/agents")
    async def drift_agents():
        return analyzer.analyze()["agents"]

    @app.get("/v1/drift/models")
    async def drift_models():
        return analyzer.analyze()["models"]

    @app.get("/v1/drift/summary")
    async def drift_summary():
        return analyzer.analyze()["summary"]

    @app.get("/drift")
    async def drift_page():
        return FileResponse(os.path.join(os.path.dirname(__file__), "drift.html"))
