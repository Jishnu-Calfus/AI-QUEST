"""L1 deterministic detectors. Fast (<50ms), explainable, no LLM.

Each detector inspects the incoming event + per-run rolling state and returns Signals.
Severity mapping: info=1 (log), warn=2 (adds suspicion), high=3 (strong suspicion),
critical=4 (immediate ladder action, e.g. policy/budget breach).
"""
from __future__ import annotations

import re
import time
from collections import deque
from dataclasses import dataclass, field

from .config import Policy
from .models import AgentEvent, EventType, Severity, Signal

PII_PATTERNS = [
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "SSN"),
    (re.compile(r"\b(?:\d[ -]*?){13,16}\b"), "credit-card-like number"),
    (re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), "email address"),
    (re.compile(r"(?i)\b(password|api[_-]?key|secret[_-]?key)\s*[:=]\s*\S+"), "credential"),
]

_norm_re = re.compile(r"[^a-z0-9 ]+")


def _norm_tokens(text: str) -> set[str]:
    return set(_norm_re.sub(" ", (text or "").lower()).split())


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


@dataclass
class RunState:
    """Rolling per-run state used by detectors."""
    goal: str = ""
    steps: int = 0
    tokens: int = 0
    cost_usd: float = 0.0
    outputs_total: int = 0
    outputs_novel: int = 0      # outputs that added new information (waste analytics)
    started: float = field(default_factory=time.time)
    recent_actions: deque = field(default_factory=lambda: deque(maxlen=32))   # (name, tokset)
    recent_outputs: deque = field(default_factory=lambda: deque(maxlen=32))   # toksets
    call_times: deque = field(default_factory=lambda: deque(maxlen=256))


def run_detectors(ev: AgentEvent, st: RunState, pol: Policy) -> list[Signal]:
    signals: list[Signal] = []
    now = ev.ts or time.time()

    # --- update rolling state ---
    st.steps += 1
    st.tokens += ev.tokens_in + ev.tokens_out
    st.cost_usd += ev.cost_usd
    st.call_times.append(now)
    action_key = f"{ev.type.value}:{ev.name or ''} {ev.content or ''}"
    action_toks = _norm_tokens(action_key)
    if ev.goal:
        st.goal = ev.goal

    # --- 1. Loop / repetition (similarity, not exact match) ---
    if ev.type in (EventType.tool_call, EventType.llm_call):
        window = list(st.recent_actions)[-pol.loop_window:]
        repeats = sum(
            1 for (name, toks) in window
            if name == (ev.name or "") and _jaccard(toks, action_toks) >= pol.loop_similarity
        )
        if repeats >= pol.loop_repeats:
            signals.append(Signal(
                detector="loop",
                severity=Severity.high,
                reason=f"Action repeated {repeats + 1}x with ≥{int(pol.loop_similarity * 100)}% similarity "
                       f"in last {pol.loop_window} steps: '{(ev.name or ev.type.value)}'",
                evidence={"repeats": repeats + 1, "action": (ev.name or "")[:80],
                          "content": (ev.content or "")[:160]},
            ))
        elif repeats == pol.loop_repeats - 1:
            signals.append(Signal(
                detector="loop",
                severity=Severity.warn,
                reason=f"Possible loop forming: '{ev.name or ev.type.value}' repeated {repeats + 1}x",
                evidence={"repeats": repeats + 1},
            ))
        st.recent_actions.append(((ev.name or ""), action_toks))

    # --- 2. Stall: outputs stopped adding new information ---
    if ev.type == EventType.output and ev.content:
        out_toks = _norm_tokens(ev.content)
        st.outputs_total += 1
        prior = list(st.recent_outputs)
        max_sim = max((_jaccard(out_toks, w) for w in prior), default=0.0)
        if max_sim < 0.9:
            st.outputs_novel += 1   # counts toward contribution in waste analytics
        window = prior[-pol.stall_window:]
        if len(window) >= pol.stall_window - 1 and window:
            avg_sim = sum(_jaccard(out_toks, w) for w in window) / len(window)
            if avg_sim >= 0.9:
                signals.append(Signal(
                    detector="stall",
                    severity=Severity.high,
                    reason=f"No new information in last {pol.stall_window} outputs "
                           f"(avg similarity {avg_sim:.0%}) — activity without progress",
                    evidence={"avg_similarity": round(avg_sim, 2)},
                ))
        st.recent_outputs.append(out_toks)

    # --- 3. Budget: cost / tokens / rate ---
    if st.cost_usd > pol.max_cost_usd:
        signals.append(Signal(
            detector="budget",
            severity=Severity.critical,
            reason=f"Cost budget breached: ${st.cost_usd:.2f} > ${pol.max_cost_usd:.2f} cap",
            evidence={"cost_usd": round(st.cost_usd, 4), "cap": pol.max_cost_usd},
        ))
    elif st.cost_usd > 0.8 * pol.max_cost_usd:
        signals.append(Signal(
            detector="budget",
            severity=Severity.warn,
            reason=f"At {st.cost_usd / pol.max_cost_usd:.0%} of ${pol.max_cost_usd:.2f} budget",
            evidence={"cost_usd": round(st.cost_usd, 4)},
        ))
    if st.tokens > pol.max_tokens:
        signals.append(Signal(
            detector="budget",
            severity=Severity.critical,
            reason=f"Token budget breached: {st.tokens:,} > {pol.max_tokens:,}",
            evidence={"tokens": st.tokens},
        ))
    recent_calls = [t for t in st.call_times if now - t <= 60]
    if len(recent_calls) > pol.max_calls_per_min:
        signals.append(Signal(
            detector="budget",
            severity=Severity.high,
            reason=f"Call rate {len(recent_calls)}/min exceeds {pol.max_calls_per_min}/min",
            evidence={"rate": len(recent_calls)},
        ))

    # --- 4. Policy: denied/unlisted tools, PII, custom patterns ---
    if ev.type == EventType.tool_call and ev.name:
        if ev.name in pol.denied_tools:
            signals.append(Signal(
                detector="policy",
                severity=Severity.critical,
                reason=f"Denied tool invoked: '{ev.name}'",
                evidence={"tool": ev.name, "args": (ev.content or "")[:160]},
            ))
        elif pol.allowed_tools is not None and ev.name not in pol.allowed_tools:
            signals.append(Signal(
                detector="policy",
                severity=Severity.critical,
                reason=f"Tool '{ev.name}' is not on the allow-list",
                evidence={"tool": ev.name},
            ))
    text = ev.content or ""
    if text:
        if pol.pii_block:
            for rx, label in PII_PATTERNS:
                m = rx.search(text)
                if m:
                    signals.append(Signal(
                        detector="policy",
                        severity=Severity.critical,
                        reason=f"PII/credential detected in agent traffic: {label}",
                        evidence={"kind": label, "sample": m.group(0)[:6] + "…"},
                    ))
                    break
        for pat in pol.denied_patterns:
            try:
                if re.search(pat, text, re.IGNORECASE):
                    signals.append(Signal(
                        detector="policy",
                        severity=Severity.critical,
                        reason=f"Content matched denied pattern: /{pat}/",
                        evidence={"pattern": pat},
                    ))
            except re.error:
                continue

    # --- 5. Runtime limits ---
    if st.steps > pol.max_steps:
        signals.append(Signal(
            detector="runtime",
            severity=Severity.critical,
            reason=f"Step limit exceeded: {st.steps} > {pol.max_steps}",
            evidence={"steps": st.steps},
        ))
    dur = now - st.started
    if dur > pol.max_duration_s:
        signals.append(Signal(
            detector="runtime",
            severity=Severity.critical,
            reason=f"Wall-time limit exceeded: {dur:.0f}s > {pol.max_duration_s:.0f}s",
            evidence={"duration_s": int(dur)},
        ))

    # --- errors are informative ---
    if ev.type == EventType.error:
        signals.append(Signal(
            detector="error",
            severity=Severity.warn,
            reason=f"Agent reported error: {(ev.content or '')[:120]}",
        ))

    return signals
