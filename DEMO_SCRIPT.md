# DEMO SCRIPT — minute by minute

Total: ~6 minutes. One person drives (terminal + browser), one person talks. Rehearse twice. Everything runs offline — no API keys, no wifi dependency.

## Pre-flight (before you're on stage)

```bash
docker compose up --build        # or: uvicorn guardian.main:app --port 8090
```
- Browser open on `http://localhost:8090`, zoomed so the back row can read it
- Two terminals ready in the `demo/` folder
- Fresh state: restart the server right before presenting (clears old runs)

---

## BEAT 1 — The hook (0:00–0:45) · no screen action

Say: *"$500M — one company's surprise AI bill this year. Gartner says 40% of AI agent projects will be canceled by 2027 — not because the AI is bad, but because nobody can prove what it costs or control what it does. We hire agents like employees and give them no payroll, no manager, no review, no audit file. We built that layer."*

Point at the empty dashboard: *"This is Guardian — a control plane for the AI workforce. Watch what happens when a swarm clocks in."*

## BEAT 2 — Swarm economics (0:45–2:30)

```bash
python swarm.py
```
Narrate as it fills in:
- *"Four agents just got **registered like employees** — owner, job description, budget. That's the roster."* (agents appear in Watched runs)
- Point at **Swarm economics** panel: *"Here's the number no token dashboard can show: **true cost** — LLM is only about half. The rest is database, compute, storage, APIs — priced live from a unit-cost catalog derived from our real bills."*
- Wait for the **WASTE badge** (~30s in): *"And here's the money moment: reviewer-agent — **half the spend, 7% of the output**. It re-verifies work that's already correct. Nobody knew. Now it has a name — and notice Guardian **paused it automatically**: activity without progress."*

## BEAT 3 — Billing true-up (2:30–3:15)

```bash
python import_billing.py
```
Say: *"Live numbers are estimates — so once a day we import the **actual cloud bill** and reconcile, category by category."* Point at the true-up line under the swarm: *"Metered versus actual. We never pretend estimates are measurements — that's what makes finance trust it."*

## BEAT 4 — Enforcement theater (3:15–4:45)

```bash
python run_demo.py looper violator wanderer
```
Narrate the incident feed as it fires:
- Violator **killed** (~5s): *"That agent just tried to call `delete_database`. Killed in milliseconds — by deterministic rules, no AI in the loop. This happens even if the model is jailbroken."*
- Looper **paused**: *"This one was retrying the same search, rephrased — similarity detection caught what exact-match can't."*
- Wanderer **escalated**: *"And this one drifted off its goal — an LLM judge caught it and froze it. It is blocked mid-run, right now, waiting for a human."*

**The click:** press **resume** (or **kill**) on the wanderer. *"That click is the whole product: a human back in charge of an autonomous system — and it just landed in the audit log with my name on it."*

## BEAT 5 — Debug + close (4:45–6:00)

Optional, if time: `curl localhost:8090/v1/runs/<id>/diagnose` (or show prepared output): *"One click root-cause: which step broke the run and why — in English, not a wall of JSON."*

Close, facing the judges: *"One event stream, seven capabilities: roster, payroll, performance review, timesheet, debugger, spending limits, and a manager — for the AI workforce. Everyone here is building agents. **We built the reason you're allowed to run them at scale — and the proof of what they're worth.** Nothing slips."*

---

## If something breaks

- Agents not appearing → server restarted after demo started; rerun `swarm.py` (it's idempotent)
- Port taken → `--port 8091` and change `GUARDIAN_URL`
- Judge questions → `curl localhost:8090/healthz` shows the active judge provider live
- Total safety net: `DEMO_TICK=0.5 python swarm.py` makes everything happen 2x faster

## Q&A hand-off

Every hard question and its answer is in **PITCH_PREP.md §6** — Bifrost, MAX_ITERATIONS, "no autonomy yet", accuracy, overhead, moat, "why not inside Aetherion". Assign one teammate to own that page.
