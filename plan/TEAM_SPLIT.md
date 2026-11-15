# Two-person split (due 5:00 PM)

Companion to [PLAN.md](PLAN.md). About 5 hours from a noon start. The split follows the code boundary so the two of you rarely touch the same file:

- **Person A: World and Command Center.** Simulator, scenario, baseline, metrics, server, dashboard, demo script. Owns "the thing judges see".
- **Person B: Orchestrator.** Fleet Agent, Safety Agent, explanations, "why?". Owns "the thing judges score".

Nav Agent goes to A because the sim needs routes to move tractors. B only calls it.

## File ownership

| Person A | Person B | Shared (change only together) |
|---|---|---|
| `orchestrator/sim.py` | `orchestrator/fleet_agent.py` | `orchestrator/models.py` |
| `orchestrator/nav_agent.py` | `orchestrator/safety_agent.py` | `scenarios/farm.json` |
| `orchestrator/baseline.py` | `orchestrator/explain.py` | `README.md` (last hour) |
| `orchestrator/metrics.py`, `eventlog.py` | `tests/test_invariants.py`, `tests/test_scenarios.py` | `requirements.txt`, `compose.yaml` |
| `server.py`, `dashboard/index.html`, `run_demo.py` | `orchestrator/llm_agent.py`, `tests/test_llm_agent.py` | `example.env` (`.env` is local, git-ignored) |
| | `orchestrator/maintenance_agent.py`, `tests/test_maintenance.py` | |

**Change after Block 1: Claude makes the decisions.** B adds a Claude-driven coordinator (`LLMFleetAgent`, see [LLM_AGENT.md](LLM_AGENT.md)). Claude proposes each plan and the Safety Agent must approve it; on any failure it falls back to the greedy `FleetAgent`, so the result is never worse than greedy and the demo never depends on the network. Agents run the whole scenario with no human input; the dashboard buttons stay as an optional operator override. The greedy planner stays the default for tests (`COORDINATOR=greedy`); `COORDINATOR=llm` switches to Claude.

**Change in Block 2: the Maintenance agent repairs things by itself.** `MaintenanceAgent` (B) watches every event: a broken tractor is repaired after `REPAIR_TRACTOR_MIN` (5 ticks), a blocked road or bridge reopens after `REPAIR_ROAD_MIN` (10 ticks), and a tractor that runs out of fuel gets a crew that also fills its tank. It works whether or not the tractor has work, and for breakdowns and blocks from the script or the dashboard. Repairs arrive as normal `repair` / `route_unblocked` events, so the coordinator replans and explains them; Claude's brief lists the repairs on the way. Repairs are part of the world, so every run gets them, the baseline included. Effects to know about:
- `sim.py` (A's) gained `process_event(ev)` (apply, observers, replan) and observer hooks; every event path, including the server's immediate apply, must use `process_event`.
- The comparison changed: with repairs the baseline also finishes all 11 jobs. The coordinator still wins on deadlines met (11 vs 7), violations (0 vs 1), travel and idle time, and finishes earlier. Greedy `WAIT_WEIGHT` went 0.2 -> 1.0 so idle tractors take work.
- Verified at 3:05: a full scripted day with Claude in the dashboard runs end to end, repairs included.

Run everything through Docker so nobody has to install packages (from `fleet-orchestrator/`): `docker compose up --build` starts the server and dashboard on http://localhost:8000 and reloads when code changes; `docker compose run --rm app pytest` runs the tests; `docker compose run --rm app python run_demo.py` runs the demo. Rebuild only after changing `requirements.txt`. The README should give these commands too.

Git: each person works on their own branch (`a/...`, `b/...`) and merges to `main` at every sync point. Changes to a shared file go in a small commit of their own, announced to the other person first.

## 12:00–12:30: write the contract together (do not skip)

This half hour is what lets you work in parallel for the next four hours. Write and commit these on `main` before splitting:

1. **`models.py`**: dataclasses for `Tractor`, `Field`, `Job`, `Event`, `Depot`, plus `WorldState` (all tractors, fields, jobs, graph, current tick, blocked edges).
2. **The dispatcher interface.** The sim talks to both dispatchers only through this:

   ```python
   @dataclass
   class Stop:
       kind: str            # "job" | "refuel"
       target: str          # job id or depot id

   @dataclass
   class Plan:
       queues: dict[str, list[Stop]]   # tractor id -> ordered stops
       explanation: str                # plain-language summary of what changed
       warnings: list[str]             # e.g. deadline risks, unassignable jobs + reason

   class Dispatcher(Protocol):
       def plan(self, state: WorldState, trigger: Event | None) -> Plan: ...
   ```

   The sim calls `plan()` at start, on every event, and whenever a tractor finishes its queue. It then executes the head of each queue. It never interrupts an in-progress job unless that tractor has broken down.
3. **Nav signature** (A implements; B relies on it):
   `nav.route(state, from_node, to_node) -> (path: list[str], distance: float) | None` (None means unreachable).
4. **`farm.json` skeleton**: 3 tractors, 8 fields, ~10 jobs, nodes/edges with one "bridge", one depot, and scripted events (a breakdown at around t=40, plus a blocked bridge and a priority change). Use rough numbers; A tunes them later.
5. A stub `FleetAgent.plan()` that returns empty queues, so A's sim runs from minute one.

## Block 1: 12:30–2:00, build the core in parallel

**A**
- `nav_agent.py`: Dijkstra over the graph, skipping blocked edges.
- `sim.py`: tick loop, movement along routes, fuel burn, work progress on `remaining`, breakdown and other events, calling `plan()`.
- `eventlog.py`: one JSONL line per event, assignment, plan change and job completion.
- `baseline.py` (FCFS) and `metrics.py`: jobs completed, travel time, idle time, deadlines met, violations.
- Stub `run_demo.py` that runs the baseline and writes `out/comparison.md`.

**B**
- Shared predicates in `safety_agent.py`: `compatible(tractor, job)`, `fuel_ok(tractor, stops, state)`, `route_ok(...)`, and `validate(plan, state) -> list[violation]`.
- `fleet_agent.py`: eligibility filter (using the predicates above), then feasibility/ETA, then greedy assignment by urgency and lowest marginal cost. Unassignable jobs go into `warnings` with a reason.
- `tests/test_invariants.py`: run the sim over random seeds and breakdown times, and call `validate()` after every tick. Use A's sim once it lands. Until then, test `plan()` on hand-built states.

**2:00 sync (15 min):** merge both branches. `python run_demo.py` should now run baseline and coordinator side by side, with numbers in `comparison.md`. Fix interface mismatches together.

## Block 2: 2:15–3:30, must-haves

**A**
- `server.py` (FastAPI + uvicorn): start the sim in a background thread from the app's `lifespan`, with a `threading.Lock` around state. Write endpoints as plain `def`, not `async def`, so FastAPI runs them in its threadpool and a blocking lock can't stall the event loop. Serve `dashboard/` with `StaticFiles`. Routes: `GET /state`, `POST /event`, `POST /jobs`, `GET /why/<job>`, `POST /speed`.
- `dashboard/index.html`: grid map with fields, tractors, routes and fuel bars; job table; explanation panel; event log; buttons for Break down, Add job/Submit job list, and speed. The sim must run to the end with no clicks; buttons are an override.
- ~~Choose the coordinator with `COORDINATOR=greedy|llm`~~: done by B (`server.py` starts on Claude when `COORDINATOR=llm`; the dashboard dropdown has `claude`; `run_demo.py` uses it). ~~Planning outside the state lock~~: done by A (the server plans in deferred mode, so the dashboard keeps updating during a Claude replan).
- ~~Add `anthropic` and pass the key through~~: done by B (`compose.yaml` loads `.env`; copy `example.env` to `.env`). ~~`httpx` in `requirements.txt`~~: done by A.

**B**
- ~~Breakdown recovery~~: done in Block 1 (the sim releases the queue keeping partial `remaining`; the coordinator replans and validates).
- ~~Scoring record per assignment~~: done in Block 1 (`Plan.why`, ready for `/why/<job>`).
- ~~Scenario test: only compatible tractor breaks down~~: done in Block 1.
- ~~`orchestrator/llm_agent.py`~~: done and merged, per [LLM_AGENT.md](LLM_AGENT.md). Claude gets a brief of the state plus the greedy plan, checks ideas with an `evaluate_queue` tool, and commits with `submit_plan`, which runs `validate()`. Fall back to greedy on any error, refusal, timeout or invalid plan. Tests use a fake client, so they stay offline.
- `explain.py` is smaller now: Claude's `explanation` is the main text, and `explain.py` is the template fallback ("T2 broke down at t=40 while spraying Field C (60% done). Its remaining work went to T1…") used when the greedy plan runs.
- ~~Maintenance agent~~: done and merged (see the note under File ownership).

**3:30 sync:** all must-haves should work end to end in the dashboard with `COORDINATOR=llm`. Let the run play out with no clicks and check that the breakdown at t=60 is handled and explained. Then click "Break down" once to check the override, and check that nothing is duplicated and violations stay at 0. If Claude is slow or unavailable, check that the greedy fallback takes over and the warning says so.

## Block 3: 3:30–4:15, stretch goals (take them in this order, stop at 4:15)

**B**
1. ~~Blocked route~~ and ~~fuel planning (refuel stops)~~: done in Block 1.
2. Record/replay cache for Claude plans (`out/llm_plans.json`), so the demo and hand-in runs are reproducible and work offline.
3. Priority change: reorder queued jobs only (check that Claude respects it; add a test).
4. 2-opt/insertion improvement on the greedy queues (this also improves the plan Claude starts from).

**A**
1. Dashboard buttons for Block route, Change priority and Why?; show Claude's explanation, and which coordinator made each plan (Claude or greedy fallback).
2. Tune `farm.json` so the comparison is honest but clear, now that repairs exist (both sides finish all 11 jobs). Consider counting idle time only while a compatible job is waiting: idle after a tractor's work is done is not waste.
3. Add a third column to `comparison.md`: Baseline | Greedy | Claude. Add a "travel per job completed" row.
4. ~~Claude API prose for "why?"~~: now covered by B's Claude coordinator.

## 4:15 code freeze. 4:15–4:45: hand-in

- **A:** run `python run_demo.py` on clean `main` (with `COORDINATOR=llm` and the replay cache) and commit `out/event_log.jsonl`, `out/comparison.md` and `out/llm_plans.json`.
- **B:** write the README: how to run (including `ANTHROPIC_API_KEY` and `COORDINATOR`), "own simulation (not AgOpenWeb)", design summary (Claude proposes, Safety Agent approves, greedy fallback, Maintenance agent repairs), scenario description, and the comparison table.
- **Both:** rehearse the 2–3 minute demo twice. Order: start the run and let the agents handle the scripted breakdown on their own, show Claude's explanation, ask "why?", then one operator override click, then the comparison table. Decide who presents and who clicks.

**4:45:** submit the code link and hand-in files. The last 15 minutes are buffer.

## If you fall behind

Cut from the bottom of this list first. Never cut the must-haves.

1. 2-opt improvement
2. Replay cache (then demo live with a network check before presenting)
3. Priority change
4. Claude coordinator: switch back to `COORDINATOR=greedy`, which already wins with 0 violations. Keep `explain.py` templates so explanations still appear.

If the 3:30 checkpoint fails, both of you drop stretch work, run with `COORDINATOR=greedy`, and fix the dashboard and explanations together.
