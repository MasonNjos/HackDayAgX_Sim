# Claude-driven Fleet Agent (LLM agents decide, humans optional)

## Context
Block 1 is done: the greedy `FleetAgent` beats the baseline (11/11 jobs, 0 violations). The user wants the coordination itself to be agentic, so Claude makes the assignment decisions instead of a fixed greedy rule, with no human input needed. Dashboard buttons stay as an optional override (decided). The sim already runs by itself and calls `dispatcher.plan(state, trigger)` at start, on every event and when a queue empties (`orchestrator/sim.py` `replan()`), so swapping in a Claude dispatcher is enough. No sim changes are needed.

Hard requirement: 0 constraint violations is worth 4 points, and the demo must never depend on the network. So Claude proposes and the existing Safety Agent decides. Any failure falls back to the greedy planner.

Timing risk: this competes with Block 2 must-haves (breakdown recovery is already handled; `explain.py`). Claude's explanation text also covers most of `explain.py`, which reduces that work.

## Design
New file `fleet-orchestrator/orchestrator/llm_agent.py` (B-owned), class `LLMFleetAgent` implementing the `Dispatcher` protocol, `name = "claude-coordinator"`.

**One `plan()` call = one short Claude tool loop (manual loop, max ~6 turns):**
1. Build a compact JSON brief from `state` + `trigger`:
   - tractors: implement, status, location, fuel / reserve, current_job, whether it must keep it (`safety.must_keep_current`);
   - open jobs: type, field, priority, deadline, remaining;
   - blocked edges; a travel-ticks table between relevant nodes (via `nav_agent.route` / `travel_ticks`);
   - **the greedy plan from `FleetAgent().plan()`** as a baseline to improve on. This keeps Claude fast and gives a known-good answer.
2. Tools (custom, `strict: true`, `additionalProperties: false`):
   - `evaluate_queue(tractor, stops)` returns `safety.simulate_queue` output: each stop's start/finish/fuel_after, lateness, and the failure reason if any. This lets Claude check ideas before committing.
   - `submit_plan(queues, explanation, reasons)`. The code runs `safety.validate()`. If there are violations, they are returned as an `is_error` tool result so Claude fixes them. If valid, the loop ends.
3. System prompt: the goal (finish all jobs, meet deadlines with priority weighting, minimise travel and idle), the hard rules, "start from the greedy plan; change it only where you can show a gain with evaluate_queue", and an explanation written for a farm operator.
4. Result: `Plan(queues, explanation=Claude's text, warnings=greedy warnings + safety.deadline_warnings, why=greedy why records merged with Claude's per-job reason)`.

**Guardrails (never worse than greedy):**
- Fall back to `FleetAgent().plan()` with a warning ("Claude unavailable: …, used greedy plan") on: no credentials, any `anthropic` error or timeout, a `refusal` stop reason, turn limit hit, no `submit_plan`, or a final plan that fails `validate()`.
- Per-call timeout (~30 s) via `client.with_options(timeout=...)`.
- Optional replay cache `out/llm_plans.json`, keyed by a hash of the brief. A re-run of `run_demo.py` gives identical plans offline (record once, then replay for the demo).

**API settings** (from the claude-api skill): `anthropic` Python SDK, model `claude-opus-5-5`, `output_config={"effort": "medium"}` (env-overridable), top-level `cache_control` so the system prompt and tools are cached across replans. Use `client.beta.messages.create` with `betas=["server-side-fallback-2026-07-01"]` and `fallbacks="default"` (on by default for Opus 5.5; tell the user). Use `tool_choice` auto, since forced tool choice is a 400 on Opus 5.5; the prompt names `submit_plan`, and the turn is re-prompted if Claude ends without calling it.

**Wiring (small changes in A's files; tell A):**
- `requirements.txt`: add `anthropic` (needs `docker compose build`).
- `compose.yaml`: pass `ANTHROPIC_API_KEY` through (`environment: - ANTHROPIC_API_KEY`).
- `run_demo.py` / later `server.py`: choose the dispatcher with env `COORDINATOR=llm|greedy` (default greedy, so tests and CI stay offline). Optionally add a third column to `comparison.md`: Baseline | Greedy | Claude.

Human override is unchanged: dashboard events go through `sim.inject()`, then `replan()`, then the Claude agent.

## Update: start-of-day route planning
At minute 0 (no event) Claude now plans the whole day from scratch instead of starting from the greedy plan:
- the brief has `mode: "full_day"` and only `greedy_score` (km, lateness), not the greedy queues;
- new tool `evaluate_plan(queues)` scores a whole plan (violations, total km, late jobs, `weighted_late`, unassigned, timelines);
- the brief also has `fcfs_baseline_target`: a what-if run of the FCFS baseline over the same day (no events) on a copy of the state: jobs done, km, km per job, idle minutes. Claude is told to beat it on travel;
- `_score` includes `idle_minutes` (each working tractor's wait from the end of its route to the end of the last route) and `last_finish_minute`;
- `submit_plan` also rejects a full-day plan with more `weighted_late` than greedy, or, at equal lateness, more km (+0.5) without finishing earlier, or a later finish (+5 min) without saving km. Idle is reported, not enforced: a tractor can always look less idle by driving more;
- the explanation gets a line `Route plan: X km, Y idle min (greedy …; FCFS baseline …); N deadline(s) at risk.`;
- its own limits: `LLM_MAX_TURNS_START` (14) and `LLM_BUDGET_START` (180 s). Replans after events are unchanged.

## Files
- New: `orchestrator/llm_agent.py`, `tests/test_llm_agent.py`
- Reuse: `orchestrator/fleet_agent.py` (`FleetAgent`), `orchestrator/safety_agent.py` (`simulate_queue`, `validate`, `deadline_warnings`, `must_keep_current`, `nearest_depot`), `orchestrator/nav_agent.py` (`route`, `travel_ticks`)
- Small edits (A's, announce): `requirements.txt`, `compose.yaml`, `run_demo.py`

## Verification
- `tests/test_llm_agent.py` with a fake client injected into `LLMFleetAgent(client=...)` (no network):
  - valid `submit_plan` → plan accepted, explanation is Claude's;
  - invalid plan, then a fixed one → second accepted after the `is_error` round;
  - client raises / refusal / never submits → greedy fallback with a warning;
  - final plan always passes `validate()`.
- `docker compose run --rm app pytest` stays green, offline.
- Live: `docker compose run --rm -e ANTHROPIC_API_KEY -e COORDINATOR=llm app python run_demo.py` → `comparison.md` shows the Claude coordinator with 0 violations, and jobs/deadlines at least as good as greedy; event log `plan` entries carry Claude's explanations.
